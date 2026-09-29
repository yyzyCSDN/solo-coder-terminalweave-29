"""Real-time, unified dispatch availability.

The equipment ledger keeps status (:mod:`fleet`), faults (:mod:`incidents`),
maintenance (:mod:`maintenance`), battery/fuel margin (:mod:`charging`,
:mod:`fuel`) and position in separate places. Dispatchers previously had to
piece those together themselves, and the basic :class:`EquipmentDispatcher`
only looked at a single ``available`` flag.

:class:`AvailabilityBoard` folds every signal into one live view and explains
*why* a unit cannot take a particular task. Task assignments carry their own
state, so an equipment status change only dislodges tasks that have not
started; a unit already working keeps its assignment.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from math import hypot

from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import (
    DispatchRejectedError,
    StateTransitionError,
    ValidationError,
)
from terminalweave.equipment.fleet import Equipment, EquipmentState, FleetRegistry
from terminalweave.equipment.fuel import FuelEfficiencyTracker
from terminalweave.equipment.incidents import IncidentBook
from terminalweave.equipment.maintenance import MaintenanceManager


class ReasonCode(str, Enum):
    """Machine-readable rejection reasons; one equipment may have several."""

    WRONG_KIND = 'wrong_kind'
    INSUFFICIENT_CAPACITY = 'insufficient_capacity'
    NOT_AVAILABLE = 'not_available'
    ALREADY_ASSIGNED = 'already_assigned'
    ACTIVE_FAULT = 'active_fault'
    MAINTENANCE_OVERDUE = 'maintenance_overdue'
    MAINTENANCE_DUE = 'maintenance_due'
    LOW_BATTERY = 'low_battery'
    LOW_FUEL = 'low_fuel'
    LOCATION_UNKNOWN = 'location_unknown'
    OUT_OF_RANGE = 'out_of_range'
    DEADLINE_UNREACHABLE = 'deadline_unreachable'


@dataclass(frozen=True)
class BlockReason:
    code: ReasonCode
    detail: str


class TaskState(str, Enum):
    PENDING = 'pending'        # in the pool, no unit committed
    ASSIGNED = 'assigned'      # dispatched to a unit, work not started
    IN_PROGRESS = 'in_progress'
    DONE = 'done'
    CANCELLED = 'cancelled'


ACTIVE_TASK_STATES = frozenset({TaskState.ASSIGNED, TaskState.IN_PROGRESS})


@dataclass(frozen=True)
class Position:
    x: float
    y: float

    def distance_to(self, other: 'Position') -> float:
        return hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True)
class DispatchTask:
    """A move request. Energy demand is expressed in the unit's own gauge:

    battery percent points for electric units, litres for fuel units."""

    job_id: str
    required_kind: str
    load_tonnes: float
    pickup: Position
    dropoff: Position
    deadline: datetime
    priority: int = 0
    battery_need_pct: float = 0.0
    fuel_need_liters: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, 'deadline', ensure_utc(self.deadline))
        if not self.job_id.strip():
            raise ValidationError('job id required')
        if self.load_tonnes <= 0:
            raise ValidationError('load must be positive')
        if self.battery_need_pct < 0 or self.fuel_need_liters < 0:
            raise ValidationError('energy demand cannot be negative')

    @property
    def loaded_distance(self) -> float:
        return self.pickup.distance_to(self.dropoff)


@dataclass(frozen=True)
class DispatchResult:
    job_id: str
    equipment_id: str
    deadhead_distance: float
    loaded_distance: float
    eta: datetime
    score: float


@dataclass(frozen=True)
class AvailabilityViewItem:
    equipment: Equipment
    position: Position | None
    available_for_task: bool
    headroom: float | None
    reasons: tuple[BlockReason, ...]


@dataclass(frozen=True)
class AvailabilityView:
    at: datetime
    task: DispatchTask | None
    items: tuple[AvailabilityViewItem, ...]

    @property
    def dispatchable(self) -> tuple[AvailabilityViewItem, ...]:
        return tuple(i for i in self.items if i.available_for_task)


@dataclass(frozen=True)
class StateChangeEffect:
    equipment_id: str
    new_state: EquipmentState
    released_job_ids: tuple[str, ...]
    unaffected_job_ids: tuple[str, ...]


class Assignment:
    """A live commitment of one unit to one job."""

    def __init__(self, job_id: str, state: TaskState = TaskState.ASSIGNED,
                 eta: datetime | None = None):
        self.job_id = job_id
        self.state = state
        self.eta = ensure_utc(eta) if eta is not None else None


@dataclass
class FuelLevel:
    """Latest tank reading used for fuel margin checks."""

    equipment_id: str
    liters: float
    at: datetime

    def __post_init__(self):
        self.at = ensure_utc(self.at)
        if self.liters < 0:
            raise ValidationError('fuel cannot be negative')


class AvailabilityBoard:
    """Live availability computed on demand from the ledger subsystems.

    Optional inputs are omitted with ``None``: a signal the board has no
    source for is not evaluated (fail-open for unknown data), except the
    fleet status and positions for a task with a deadline, which are always
    required.
    """

    def __init__(self,
                 fleet: FleetRegistry,
                 positions: dict[str, Position] | None = None,
                 incidents: IncidentBook | None = None,
                 maintenance: MaintenanceManager | None = None,
                 fuel_tracker: FuelEfficiencyTracker | None = None,
                 fuel_levels: dict[str, FuelLevel] | None = None,
                 *,
                 battery_reserve_pct: float = 20.0,
                 fuel_reserve_liters: float = 0.0,
                 block_when_maintenance_due: bool = True,
                 severe_incident_threshold: int = 4,
                 max_deadhead: float | None = None,
                 travel_speed: float = 20.0):
        if not 0 <= battery_reserve_pct <= 100:
            raise ValidationError('battery reserve must be 0..100')
        if fuel_reserve_liters < 0 or travel_speed <= 0:
            raise ValidationError('invalid reserve or travel speed')
        if severe_incident_threshold not in range(1, 6):
            raise ValidationError('severity threshold must be 1..5')
        self.fleet = fleet
        self.positions = dict(positions or {})
        self.incidents = incidents
        self.maintenance = maintenance
        self.fuel_tracker = fuel_tracker
        self.fuel_levels = dict(fuel_levels or {})
        self.battery_reserve_pct = battery_reserve_pct
        self.fuel_reserve_liters = fuel_reserve_liters
        self.block_when_maintenance_due = block_when_maintenance_due
        self.severe_incident_threshold = severe_incident_threshold
        self.max_deadhead = max_deadhead
        self.travel_speed = travel_speed
        self._tasks: dict[str, DispatchTask] = {}
        self._task_state: dict[str, TaskState] = {}
        self._assignments: dict[str, Assignment] = {}

    # -- positions -------------------------------------------------------

    def update_position(self, equipment_id: str, position: Position) -> None:
        if equipment_id not in self.fleet.items:
            raise ValidationError(f'unknown equipment {equipment_id}')
        self.positions[equipment_id] = position

    def update_fuel_level(self, level: FuelLevel) -> None:
        if level.equipment_id not in self.fleet.items:
            raise ValidationError(f'unknown equipment {level.equipment_id}')
        self.fuel_levels[level.equipment_id] = level

    # -- task book -------------------------------------------------------

    def register_task(self, task: DispatchTask) -> None:
        if task.job_id in self._tasks:
            raise ValidationError(f'duplicate job {task.job_id}')
        self._tasks[task.job_id] = task
        self._task_state[task.job_id] = TaskState.PENDING

    def task_state(self, job_id: str) -> TaskState:
        return self._task_state[job_id]

    def assigned_equipment(self, job_id: str) -> str | None:
        for equipment_id, assignment in self._assignments.items():
            if assignment.job_id == job_id and assignment.state in ACTIVE_TASK_STATES:
                return equipment_id
        return None

    # -- evaluation ------------------------------------------------------

    def _eta(self, position: Position, task: DispatchTask, now: datetime) -> datetime:
        deadhead = position.distance_to(task.pickup)
        return now + timedelta(hours=deadhead / self.travel_speed) \
            if deadhead else now

    def evaluate(self, equipment: Equipment, task: DispatchTask | None,
                 now: datetime) -> tuple[bool, float | None, list[BlockReason]]:
        """Return (dispatchable, energy headroom, reasons) for one unit."""
        reasons: list[BlockReason] = []
        headroom: float | None = None

        if task is not None:
            if equipment.kind != task.required_kind:
                reasons.append(BlockReason(
                    ReasonCode.WRONG_KIND,
                    f"requires {task.required_kind}, unit is {equipment.kind}"))
            if equipment.capacity_tonnes < task.load_tonnes:
                reasons.append(BlockReason(
                    ReasonCode.INSUFFICIENT_CAPACITY,
                    f"capacity {equipment.capacity_tonnes}t below load {task.load_tonnes}t"))

        busy_job = self._active_job(equipment.equipment_id)
        if busy_job is not None:
            reasons.append(BlockReason(
                ReasonCode.ALREADY_ASSIGNED,
                f"committed to job {busy_job} ({self._assignments[equipment.equipment_id].state.value})"))
        elif equipment.state != EquipmentState.AVAILABLE:
            reasons.append(BlockReason(
                ReasonCode.NOT_AVAILABLE,
                f"state is {equipment.state.value}"))

        if self.incidents is not None:
            active = self.incidents.active_for(equipment.equipment_id)
            severe = [i for i in active if i.severity >= self.severe_incident_threshold]
            if severe:
                top = max(severe, key=lambda i: i.severity)
                reasons.append(BlockReason(
                    ReasonCode.ACTIVE_FAULT,
                    f"unresolved {top.category} fault, severity {top.severity}"))

        reasons.extend(self._maintenance_reasons(equipment.equipment_id))

        position = self.positions.get(equipment.equipment_id)
        if task is not None:
            if position is None:
                reasons.append(BlockReason(
                    ReasonCode.LOCATION_UNKNOWN,
                    'current position not reported'))
            else:
                deadhead = position.distance_to(task.pickup)
                if self.max_deadhead is not None and deadhead > self.max_deadhead:
                    reasons.append(BlockReason(
                        ReasonCode.OUT_OF_RANGE,
                        f"deadhead {deadhead:.1f} exceeds limit {self.max_deadhead:.1f}"))
                eta = self._eta(position, task, now)
                if eta > task.deadline:
                    reasons.append(BlockReason(
                        ReasonCode.DEADLINE_UNREACHABLE,
                        f"ETA {eta.isoformat()} after deadline {task.deadline.isoformat()}"))
        elif equipment.location is None:
            reasons.append(BlockReason(
                ReasonCode.LOCATION_UNKNOWN, 'current location not reported'))

        fuel_need = task.fuel_need_liters if task is not None else 0.0
        if (task is not None and fuel_need == 0.0 and self.fuel_tracker is not None
                and position is not None):
            rate = self.fuel_tracker.liters_per_hour(equipment.equipment_id)
            if rate is not None:
                trip_hours = ((position.distance_to(task.pickup)
                               + task.loaded_distance) / self.travel_speed)
                fuel_need = rate * trip_hours
        energy_reason, headroom = self._energy_reason(equipment, task, fuel_need)
        if energy_reason is not None:
            reasons.append(energy_reason)

        return (not reasons), headroom, reasons

    def _active_job(self, equipment_id: str) -> str | None:
        assignment = self._assignments.get(equipment_id)
        if assignment is None or assignment.state not in ACTIVE_TASK_STATES:
            return None
        return assignment.job_id

    def _maintenance_reasons(self, equipment_id: str) -> list[BlockReason]:
        mgr = self.maintenance
        if mgr is None or equipment_id not in mgr.tracked_equipment():
            return []
        out: list[BlockReason] = []
        for order in mgr.active_orders(equipment_id):
            if mgr.is_overdue(equipment_id, order.plan_id):
                out.append(BlockReason(
                    ReasonCode.MAINTENANCE_OVERDUE,
                    f"plan {order.plan_id} past service meter {order.due_at_meter}"))
            elif self.block_when_maintenance_due:
                out.append(BlockReason(
                    ReasonCode.MAINTENANCE_DUE,
                    f"plan {order.plan_id} due at meter {order.due_at_meter}"))
        return out

    def _energy_reason(self, equipment: Equipment,
                       task: DispatchTask | None, fuel_need_liters: float
                       ) -> tuple[BlockReason | None, float | None]:
        need_pct = task.battery_need_pct if task is not None else 0.0
        battery = equipment.battery_percent
        if battery is not None:
            floor = self.battery_reserve_pct + need_pct
            headroom = battery - self.battery_reserve_pct
            if battery < floor:
                return (BlockReason(
                    ReasonCode.LOW_BATTERY,
                    f"battery {battery:.0f}% below {need_pct:.0f}% demand + "
                    f"{self.battery_reserve_pct:.0f}% reserve"), headroom)
            return None, headroom

        level = self.fuel_levels.get(equipment.equipment_id)
        if level is not None:
            headroom = level.liters - self.fuel_reserve_liters
            if level.liters < fuel_need_liters + self.fuel_reserve_liters:
                return (BlockReason(
                    ReasonCode.LOW_FUEL,
                    f"fuel {level.liters:.0f}L below {fuel_need_liters:.0f}L demand + "
                    f"{self.fuel_reserve_liters:.0f}L reserve"), headroom)
            return None, headroom
        return None, None

    def view(self, now: datetime, task: DispatchTask | None = None
             ) -> AvailabilityView:
        """Real-time snapshot. Pass a task to score against its requirements."""
        now = ensure_utc(now)
        items = []
        for equipment_id in sorted(self.fleet.items):
            equipment = self.fleet.items[equipment_id]
            ok, headroom, reasons = self.evaluate(equipment, task, now)
            items.append(AvailabilityViewItem(
                equipment, self.positions.get(equipment_id), ok, headroom,
                tuple(reasons)))
        return AvailabilityView(now, task, tuple(items))

    # -- dispatch --------------------------------------------------------

    def dispatch(self, task: DispatchTask, now: datetime
                 ) -> DispatchResult:
        """Pick the best unit for *task* or reject with specific reasons."""
        now = ensure_utc(now)
        if task.job_id in self._tasks:
            if self._task_state[task.job_id] != TaskState.PENDING:
                raise ValidationError(
                    f"job {task.job_id} is {self._task_state[task.job_id].value}")
        else:
            self.register_task(task)
        scored = []
        rejected: dict[str, list[BlockReason]] = {}
        for item in self.view(now, task).items:
            if not item.available_for_task:
                rejected[item.equipment.equipment_id] = list(item.reasons)
                continue
            position = item.position  # never None: LOCATION_UNKNOWN would block
            deadhead = position.distance_to(task.pickup)
            eta = self._eta(position, task, now)
            headroom = item.headroom if item.headroom is not None else 0.0
            score = (deadhead
                     + task.loaded_distance * 0.15
                     - task.priority * 0.1
                     - headroom * 0.001)
            scored.append((score, item.equipment.equipment_id, deadhead, eta))

        if not scored:
            raise DispatchRejectedError(
                self._rejection_message(task, rejected), rejected)

        score, equipment_id, deadhead, eta = min(scored, key=lambda s: s[0])
        self._assignments[equipment_id] = Assignment(
            task.job_id, TaskState.ASSIGNED, eta)
        self._task_state[task.job_id] = TaskState.ASSIGNED
        self.fleet.transition(equipment_id, EquipmentState.WORKING)
        return DispatchResult(task.job_id, equipment_id, deadhead,
                              task.loaded_distance, eta, score)

    def _rejection_message(self, task: DispatchTask,
                           rejected: dict[str, list[BlockReason]]) -> str:
        lines = [f"dispatch rejected for job {task.job_id}:"]
        for equipment_id in sorted(rejected):
            codes = ', '.join(f"{r.code.value} ({r.detail})"
                              for r in rejected[equipment_id])
            lines.append(f"  {equipment_id}: {codes}")
        return '\n'.join(lines)

    # -- assignment lifecycle -------------------------------------------

    def start_task(self, job_id: str, now: datetime) -> None:
        now = ensure_utc(now)
        equipment_id, assignment = self._job_assignment(job_id)
        if assignment.state != TaskState.ASSIGNED:
            raise StateTransitionError(
                f'job {job_id} is {assignment.state.value}, not assigned')
        assignment.state = TaskState.IN_PROGRESS
        self._task_state[job_id] = TaskState.IN_PROGRESS

    def complete_task(self, job_id: str, now: datetime) -> None:
        now = ensure_utc(now)
        equipment_id, assignment = self._job_assignment(job_id)
        if assignment.state not in {TaskState.ASSIGNED, TaskState.IN_PROGRESS}:
            raise StateTransitionError(
                f'job {job_id} is {assignment.state.value}')
        self._assignments.pop(equipment_id, None)
        self._task_state[job_id] = TaskState.DONE
        equipment = self.fleet.items[equipment_id]
        if equipment.state == EquipmentState.WORKING:
            self.fleet.transition(equipment_id, EquipmentState.AVAILABLE)

    def cancel_task(self, job_id: str, now: datetime) -> None:
        """Cancel a task that has not started; working tasks must finish first."""
        now = ensure_utc(now)
        equipment_id, assignment = self._job_assignment(job_id)
        if assignment.state == TaskState.IN_PROGRESS:
            raise StateTransitionError(
                f'job {job_id} already started; complete instead of cancel')
        if assignment.state != TaskState.ASSIGNED:
            raise StateTransitionError(
                f'job {job_id} is {assignment.state.value}')
        self._assignments.pop(equipment_id, None)
        self._task_state[job_id] = TaskState.CANCELLED
        equipment = self.fleet.items[equipment_id]
        if equipment.state == EquipmentState.WORKING:
            self.fleet.transition(equipment_id, EquipmentState.AVAILABLE)

    def _job_assignment(self, job_id: str) -> tuple[str, Assignment]:
        for equipment_id, assignment in self._assignments.items():
            if assignment.job_id == job_id:
                return equipment_id, assignment
        raise ValidationError(f'no active assignment for job {job_id}')

    # -- equipment status changes ---------------------------------------

    def change_equipment_state(self, equipment_id: str, new_state: EquipmentState,
                               now: datetime) -> StateChangeEffect:
        """Move a unit to CHARGING / MAINTENANCE / OUT_OF_SERVICE.

        Only the not-yet-started (assigned) commitment is released, sending
        the job back to the pending pool; work that has started keeps running
        and the unit returns to it afterwards.
        """
        now = ensure_utc(now)
        assignment = self._assignments.get(equipment_id)
        released: list[str] = []
        unaffected: list[str] = []
        if assignment is not None:
            if assignment.state == TaskState.ASSIGNED:
                released.append(assignment.job_id)
                self._assignments.pop(equipment_id, None)
                self._task_state[assignment.job_id] = TaskState.PENDING
            elif assignment.state == TaskState.IN_PROGRESS:
                unaffected.append(assignment.job_id)
        self.fleet.transition(equipment_id, new_state)
        return StateChangeEffect(equipment_id, new_state,
                                 tuple(released), tuple(unaffected))

    def return_to_service(self, equipment_id: str, now: datetime) -> StateChangeEffect:
        """Clear CHARGING / MAINTENANCE / OUT_OF_SERVICE and become available.

        A unit still finishing a started task returns to WORKING instead of
        AVAILABLE so that task remains attached."""
        now = ensure_utc(now)
        assignment = self._assignments.get(equipment_id)
        still_working = (assignment is not None
                         and assignment.state == TaskState.IN_PROGRESS)
        target = EquipmentState.WORKING if still_working else EquipmentState.AVAILABLE
        self.fleet.transition(equipment_id, target)
        unaffected = (assignment.job_id,) if still_working else ()
        return StateChangeEffect(equipment_id, target, (), unaffected)
