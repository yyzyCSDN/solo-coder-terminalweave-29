from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import (ValidationError, ConflictError,
                                       NotFoundError, CapacityError, StateTransitionError)
from .fleet import FleetRegistry, EquipmentState
from .maintenance import MaintenanceManager
from .incidents import IncidentBook
from .fuel import FuelEfficiencyTracker
from .dispatch import Position
from .availability import (AvailabilityRow, DispatchTask, EquipmentEvaluation,
                           OperatingProfile, RejectCode, RejectReason, TaskState,
                           evaluate_equipment)

class DispatchRejected(CapacityError):
    """Raised when a task cannot be assigned; every concrete reason is attached."""

    def __init__(self, task_id: str, equipment_id: str | None,
                 reasons: tuple[RejectReason, ...]):
        target = f'equipment {equipment_id} ' if equipment_id else 'all candidates '
        detail = '; '.join(f'[{r.code.value}] {r.message}' for r in reasons)
        super().__init__(f'dispatch of task {task_id} to {target}rejected: {detail}')
        self.task_id = task_id
        self.equipment_id = equipment_id
        self.reasons = reasons

@dataclass(frozen=True)
class Assignment:
    task_id: str
    equipment_id: str
    assigned_at: datetime
    evaluation: EquipmentEvaluation

@dataclass(frozen=True)
class Revocation:
    task_id: str
    equipment_id: str
    at: datetime
    reason_code: RejectCode
    message: str
    detail: dict = field(default_factory=dict)

    @classmethod
    def from_reason(cls, task_id: str, equipment_id: str, at: datetime,
                    reason: RejectReason) -> 'Revocation':
        return cls(task_id, equipment_id, at, reason.code, reason.message, dict(reason.detail))

class DispatchBoard:
    """Real-time dispatchable-equipment view and task lifecycle.

    Fleet state changes are observed and only invalidate *not-yet-started*
    assignments; in-progress and finished tasks keep running and become
    revocations only if the dispatcher re-checks a pending assignment.
    """

    def __init__(self, fleet: FleetRegistry, *,
                 maintenance: MaintenanceManager | None = None,
                 incidents: IncidentBook | None = None,
                 fuel: FuelEfficiencyTracker | None = None,
                 low_energy_pct: float = 20.0,
                 incident_block_severity: int = 4):
        self.fleet = fleet
        self.maintenance = maintenance
        self.incidents = incidents
        self.fuel = fuel
        self.low_energy_pct = low_energy_pct
        self.incident_block_severity = incident_block_severity
        self._profiles: dict[str, OperatingProfile] = {}
        self._positions: dict[str, Position] = {}
        self._tasks: dict[str, DispatchTask] = {}
        self._task_state: dict[str, TaskState] = {}
        self._assignments: dict[str, Assignment] = {}
        self._revocations: dict[str, Revocation] = {}
        self._dirty: set[str] = set()
        fleet.on_transition(self._fleet_changed)

    # ---- registration -------------------------------------------------
    def register_profile(self, profile: OperatingProfile) -> None:
        if profile.equipment_id not in self.fleet.items:
            raise NotFoundError(f'unknown equipment {profile.equipment_id}')
        self._profiles[profile.equipment_id] = profile

    def report_position(self, equipment_id: str, position: Position) -> None:
        if equipment_id not in self.fleet.items:
            raise NotFoundError(f'unknown equipment {equipment_id}')
        self._positions[equipment_id] = position

    # ---- task lifecycle ------------------------------------------------
    def add_task(self, task: DispatchTask) -> DispatchTask:
        if task.task_id in self._tasks:
            raise ValidationError('duplicate task id')
        self._tasks[task.task_id] = task
        self._task_state[task.task_id] = TaskState.PENDING
        return task

    def _require_task(self, task_id: str) -> DispatchTask:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise NotFoundError(f'unknown task {task_id}')

    def task_state(self, task_id: str) -> TaskState:
        self._require_task(task_id)
        return self._task_state[task_id]

    def assignment(self, task_id: str) -> Assignment | None:
        self._require_task(task_id)
        return self._assignments.get(task_id)

    def last_revocation(self, task_id: str) -> Revocation | None:
        self._require_task(task_id)
        return self._revocations.get(task_id)

    def revocations(self) -> tuple[Revocation, ...]:
        return tuple(sorted(self._revocations.values(),
                            key=lambda r: (r.at, r.task_id)))

    def cancel_task(self, task_id: str) -> None:
        self._require_task(task_id)
        if self._task_state[task_id] not in {TaskState.PENDING, TaskState.ASSIGNED}:
            raise StateTransitionError(f'cannot cancel task in state {self._task_state[task_id]}')
        self._assignments.pop(task_id, None)
        self._task_state[task_id] = TaskState.CANCELLED

    def assign(self, task_id: str, equipment_id: str, now: datetime) -> Assignment:
        now = ensure_utc(now)
        self.refresh(now)
        task = self._require_task(task_id)
        if self._task_state[task_id] != TaskState.PENDING:
            raise ConflictError(f'task {task_id} is {self._task_state[task_id].value}')
        if equipment_id not in self.fleet.items:
            raise DispatchRejected(task_id, equipment_id, (RejectReason(
                RejectCode.UNKNOWN_EQUIPMENT, f'unknown equipment {equipment_id}',
                {'equipment_id': equipment_id}),))
        evaluation = self._evaluate(equipment_id, task, now)
        if evaluation.blockers:
            raise DispatchRejected(task_id, equipment_id, evaluation.blockers)
        assignment = Assignment(task_id, equipment_id, now, evaluation)
        self._assignments[task_id] = assignment
        self._task_state[task_id] = TaskState.ASSIGNED
        return assignment

    def auto_assign(self, task_id: str, now: datetime) -> Assignment:
        now = ensure_utc(now)
        self.refresh(now)
        task = self._require_task(task_id)
        if self._task_state[task_id] != TaskState.PENDING:
            raise ConflictError(f'task {task_id} is {self._task_state[task_id].value}')
        evaluations = self.evaluate_for_task(task, now)
        feasible = [e for e in evaluations if not e.blockers]
        if not feasible:
            reasons = tuple(r for e in evaluations for r in e.blockers)
            if not reasons:
                reasons = (RejectReason(RejectCode.UNKNOWN_EQUIPMENT,
                                        'no equipment registered', {}),)
            raise DispatchRejected(task_id, None, reasons)
        chosen = min(feasible, key=lambda e: e.score)
        assignment = Assignment(task_id, chosen.equipment_id, now, chosen)
        self._assignments[task_id] = assignment
        self._task_state[task_id] = TaskState.ASSIGNED
        return assignment

    def start_task(self, task_id: str, now: datetime) -> Assignment:
        now = ensure_utc(now)
        self.refresh(now)
        task = self._require_task(task_id)
        if self._task_state[task_id] == TaskState.PENDING:
            raise StateTransitionError('task has no assignment; assign it first')
        if self._task_state[task_id] != TaskState.ASSIGNED:
            raise StateTransitionError(f'task {task_id} is {self._task_state[task_id].value}')
        assignment = self._assignments[task_id]
        recheck = self._evaluate(assignment.equipment_id, task, now)
        if recheck.blockers:
            raise DispatchRejected(task_id, assignment.equipment_id, recheck.blockers)
        self.fleet.transition(assignment.equipment_id, EquipmentState.WORKING)
        self._task_state[task_id] = TaskState.IN_PROGRESS
        return assignment

    def complete_task(self, task_id: str, now: datetime,
                      new_position: Position | None = None) -> None:
        now = ensure_utc(now)
        task = self._require_task(task_id)
        if self._task_state[task_id] != TaskState.IN_PROGRESS:
            raise StateTransitionError('task is not in progress')
        assignment = self._assignments[task_id]
        if new_position is not None:
            self._positions[assignment.equipment_id] = new_position
        self.fleet.transition(assignment.equipment_id, EquipmentState.AVAILABLE)
        self._task_state[task_id] = TaskState.DONE

    # ---- state changes only touch not-started assignments -------------
    def _fleet_changed(self, equipment_id: str, new_state: EquipmentState) -> None:
        self._dirty.add(equipment_id)

    def pending_changes(self) -> frozenset[str]:
        return frozenset(self._dirty)

    def refresh(self, now: datetime) -> tuple[Revocation, ...]:
        now = ensure_utc(now)
        revoked: list[Revocation] = []
        if self._dirty:
            dirty = self._dirty
            for task_id, assignment in list(self._assignments.items()):
                if assignment.equipment_id not in dirty:
                    continue
                if self._task_state[task_id] != TaskState.ASSIGNED:
                    continue  # in-progress/done work is never interrupted
                task = self._tasks[task_id]
                evaluation = self._evaluate(assignment.equipment_id, task, now)
                if evaluation.blockers:
                    reason = evaluation.blockers[0]
                    revocation = Revocation.from_reason(task_id, assignment.equipment_id, now, reason)
                    self._revocations[task_id] = revocation
                    self._assignments.pop(task_id, None)
                    self._task_state[task_id] = TaskState.PENDING
                    revoked.append(revocation)
            self._dirty.clear()
        return tuple(sorted(revoked, key=lambda r: r.task_id))

    # ---- real-time view ------------------------------------------------
    def availability_view(self, now: datetime,
                          kind: str | None = None) -> tuple[AvailabilityRow, ...]:
        now = ensure_utc(now)
        rows = []
        for equipment in self.fleet.items.values():
            if kind is not None and equipment.kind != kind:
                continue
            evaluation = self._evaluate(equipment.equipment_id, None, now)
            rows.append(AvailabilityRow(
                equipment.equipment_id, equipment.kind, equipment.state,
                equipment.location, evaluation.level, evaluation.blockers,
                evaluation.warnings,
                evaluation.energy.percent if evaluation.energy else None,
                evaluation.energy.autonomy_minutes if evaluation.energy else None))
        return tuple(sorted(rows, key=lambda r: r.equipment_id))

    def evaluate_for_task(self, task: DispatchTask,
                          now: datetime) -> tuple[EquipmentEvaluation, ...]:
        now = ensure_utc(now)
        return tuple(self._evaluate(item.equipment_id, task, now)
                     for item in self.fleet.items.values())

    def _evaluate(self, equipment_id: str, task: DispatchTask | None,
                  now: datetime) -> EquipmentEvaluation:
        equipment = self.fleet.items[equipment_id]
        occupied_by: str | None = None
        for other_id, other in self._assignments.items():
            if other.equipment_id == equipment_id and \
                    self._task_state[other_id] in {TaskState.ASSIGNED, TaskState.IN_PROGRESS}:
                occupied_by = other_id
                break
        maintenance = None
        if self.maintenance is not None:
            maintenance = self.maintenance.maintenance_status(equipment_id)
        incidents: tuple = ()
        if self.incidents is not None:
            incidents = self.incidents.open_incidents(equipment_id)
        fuel_reading = None
        if self.fuel is not None and equipment.battery_percent is None:
            fuel_reading = self.fuel.latest_reading(equipment_id)
        return evaluate_equipment(
            equipment, now,
            position=self._positions.get(equipment_id),
            profile=self._profiles.get(equipment_id),
            maintenance=maintenance,
            incidents=incidents,
            fuel_reading=fuel_reading,
            task=task,
            occupied_by=occupied_by,
            low_energy_pct=self.low_energy_pct,
            incident_block_severity=self.incident_block_severity)
