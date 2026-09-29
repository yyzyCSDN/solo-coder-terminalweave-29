from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from math import hypot
from terminalweave.core.clock import ensure_utc, iso_z
from terminalweave.core.errors import ValidationError
from .fleet import Equipment, EquipmentState
from .maintenance import MaintenanceStatus
from .incidents import EquipmentIncident
from .dispatch import Position

class RejectCode(str, Enum):
    UNKNOWN_EQUIPMENT = 'unknown_equipment'
    KIND_MISMATCH = 'kind_mismatch'
    INSUFFICIENT_CAPACITY = 'insufficient_capacity'
    NOT_AVAILABLE_STATE = 'not_available_state'
    OUT_OF_SERVICE_FAULT = 'out_of_service_fault'
    INCIDENT_OPEN = 'incident_open'
    MAINTENANCE_OVERDUE = 'maintenance_overdue'
    MAINTENANCE_ACTIVE = 'maintenance_active'
    LOCATION_UNKNOWN = 'location_unknown'
    ENERGY_UNKNOWN = 'energy_unknown'
    ENERGY_INSUFFICIENT = 'energy_insufficient'
    DEADLINE_UNREACHABLE = 'deadline_unreachable'
    EQUIPMENT_BUSY = 'equipment_busy'

class AvailabilityLevel(str, Enum):
    DISPATCHABLE = 'dispatchable'
    CONSTRAINED = 'constrained'
    BLOCKED = 'blocked'

class EnergyMetric(str, Enum):
    BATTERY = 'battery'
    FUEL = 'fuel'

class TaskState(str, Enum):
    PENDING = 'pending'
    ASSIGNED = 'assigned'
    IN_PROGRESS = 'in_progress'
    DONE = 'done'
    CANCELLED = 'cancelled'

@dataclass(frozen=True)
class OperatingProfile:
    equipment_id: str
    avg_speed_kmh: float
    battery_kwh: float | None = None
    consumption_kwh_per_km: float | None = None
    reserve_pct: float = 20.0
    fuel_lph: float | None = None
    tank_liters: float | None = None

    def __post_init__(self) -> None:
        if self.avg_speed_kmh <= 0:
            raise ValidationError('average speed must be positive')
        if not 0 <= self.reserve_pct < 100:
            raise ValidationError('reserve percent must be 0..100')
        electric = self.battery_kwh is not None or self.consumption_kwh_per_km is not None
        if electric and (self.battery_kwh is None or self.battery_kwh <= 0
                         or self.consumption_kwh_per_km is None
                         or self.consumption_kwh_per_km <= 0):
            raise ValidationError('invalid electric operating profile')
        burns_fuel = self.fuel_lph is not None or self.tank_liters is not None
        if burns_fuel and (self.fuel_lph is None or self.fuel_lph <= 0
                           or self.tank_liters is None or self.tank_liters <= 0):
            raise ValidationError('invalid fuel operating profile')

@dataclass(frozen=True)
class DispatchTask:
    task_id: str
    required_kind: str
    load_tonnes: float
    pickup: Position
    dropoff: Position
    deadline: datetime | None = None
    priority: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.required_kind.strip():
            raise ValidationError('task id and kind required')
        if self.load_tonnes <= 0:
            raise ValidationError('load must be positive')
        if self.deadline is not None:
            object.__setattr__(self, 'deadline', ensure_utc(self.deadline))
        if self.created_at is not None:
            object.__setattr__(self, 'created_at', ensure_utc(self.created_at))

    @property
    def loaded_km(self) -> float:
        return hypot(self.dropoff.x - self.pickup.x, self.dropoff.y - self.pickup.y)

@dataclass(frozen=True)
class EnergyStatus:
    metric: EnergyMetric | None
    percent: float | None
    autonomy_minutes: float | None
    known: bool
    detail: str = ''

    @classmethod
    def unknown(cls, detail: str) -> 'EnergyStatus':
        return cls(None, None, None, False, detail)

@dataclass(frozen=True)
class RejectReason:
    code: RejectCode
    message: str
    detail: dict = field(default_factory=dict)

@dataclass(frozen=True)
class EquipmentEvaluation:
    equipment_id: str
    level: AvailabilityLevel
    blockers: tuple[RejectReason, ...]
    warnings: tuple[str, ...]
    deadhead_km: float | None = None
    deadhead_minutes: float | None = None
    energy: EnergyStatus | None = None
    energy_headroom_pct: float | None = None
    score: tuple | None = None

@dataclass(frozen=True)
class AvailabilityRow:
    equipment_id: str
    kind: str
    state: EquipmentState
    location: str | None
    level: AvailabilityLevel
    blockers: tuple[RejectReason, ...]
    warnings: tuple[str, ...]
    energy_percent: float | None
    autonomy_minutes: float | None

def _distance(a: Position, b: Position) -> float:
    return hypot(a.x - b.x, a.y - b.y)

def energy_status(equipment: Equipment, profile: OperatingProfile | None,
                  fuel_reading, now: datetime) -> EnergyStatus:
    now = ensure_utc(now)
    if equipment.battery_percent is not None:
        if profile is None or profile.battery_kwh is None \
                or profile.consumption_kwh_per_km is None:
            return EnergyStatus.unknown('battery powered unit lacks electric operating profile')
        range_km = (equipment.battery_percent / 100.0) * profile.battery_kwh \
            / profile.consumption_kwh_per_km
        minutes = range_km / profile.avg_speed_kmh * 60.0
        return EnergyStatus(EnergyMetric.BATTERY, equipment.battery_percent, minutes, True)
    if profile is not None and profile.fuel_lph is None and profile.battery_kwh is not None:
        return EnergyStatus.unknown('battery reading missing')
    if fuel_reading is None:
        return EnergyStatus.unknown('no fuel reading recorded')
    if profile is None or profile.tank_liters is None or profile.fuel_lph is None:
        return EnergyStatus.unknown('diesel unit lacks fuel operating profile')
    if fuel_reading.liters > profile.tank_liters + 1e-9:
        return EnergyStatus.unknown('fuel reading exceeds tank capacity')
    percent = fuel_reading.liters / profile.tank_liters * 100.0
    minutes = fuel_reading.liters / profile.fuel_lph * 60.0
    return EnergyStatus(EnergyMetric.FUEL, percent, minutes, True)

def evaluate_equipment(equipment: Equipment, now: datetime, *,
                       position: Position | None,
                       profile: OperatingProfile | None = None,
                       maintenance: MaintenanceStatus | None = None,
                       incidents: tuple[EquipmentIncident, ...] = (),
                       fuel_reading=None,
                       task: DispatchTask | None = None,
                       occupied_by: str | None = None,
                       low_energy_pct: float = 20.0,
                       incident_block_severity: int = 4) -> EquipmentEvaluation:
    """Aggregate every availability signal for one unit.

    With a task the result is fail-closed: missing position or energy data
    becomes a hard blocker. Without a task those become warnings so that the
    real-time view still surfaces the unit.
    """
    now = ensure_utc(now)
    if not 0 <= low_energy_pct <= 100:
        raise ValidationError('low energy percent must be 0..100')
    eid = equipment.equipment_id
    blockers: list[RejectReason] = []
    warnings: list[str] = []

    if task is not None and occupied_by is not None and occupied_by != task.task_id:
        blockers.append(RejectReason(RejectCode.EQUIPMENT_BUSY,
                                     f'equipment is assigned to task {occupied_by}',
                                     {'equipment_id': eid, 'task_id': occupied_by}))
    elif task is None and occupied_by is not None:
        warnings.append(f'committed to task {occupied_by}')

    if task is not None and equipment.kind != task.required_kind:
        blockers.append(RejectReason(RejectCode.KIND_MISMATCH,
                                     f"kind '{equipment.kind}' does not match required '{task.required_kind}'",
                                     {'equipment_id': eid, 'kind': equipment.kind,
                                      'required_kind': task.required_kind}))
    if task is not None and equipment.capacity_tonnes < task.load_tonnes:
        blockers.append(RejectReason(RejectCode.INSUFFICIENT_CAPACITY,
                                     f'capacity {equipment.capacity_tonnes}t below required {task.load_tonnes}t',
                                     {'equipment_id': eid, 'capacity_tonnes': equipment.capacity_tonnes,
                                      'required_tonnes': task.load_tonnes}))

    if equipment.state == EquipmentState.OUT_OF_SERVICE:
        blockers.append(RejectReason(RejectCode.OUT_OF_SERVICE_FAULT,
                                     'equipment is out of service due to fault',
                                     {'equipment_id': eid}))
    elif equipment.state == EquipmentState.MAINTENANCE:
        blockers.append(RejectReason(RejectCode.MAINTENANCE_ACTIVE,
                                     'equipment is under maintenance',
                                     {'equipment_id': eid}))
    elif equipment.state != EquipmentState.AVAILABLE:
        blockers.append(RejectReason(RejectCode.NOT_AVAILABLE_STATE,
                                     f"equipment state is '{equipment.state.value}'",
                                     {'equipment_id': eid, 'state': equipment.state.value}))

    open_incidents = tuple(i for i in incidents if i.resolved_at is None)
    severe = tuple(i for i in open_incidents if i.severity >= incident_block_severity)
    if severe:
        worst = max(severe, key=lambda i: i.severity)
        blockers.append(RejectReason(RejectCode.INCIDENT_OPEN,
                                     f'unresolved severity {worst.severity} incident {worst.incident_id}',
                                     {'equipment_id': eid, 'incident_id': worst.incident_id,
                                      'severity': worst.severity}))
    elif open_incidents:
        worst = max(open_incidents, key=lambda i: i.severity)
        warnings.append(f'open severity {worst.severity} incident {worst.incident_id}')

    if maintenance is not None:
        if maintenance.overdue:
            blockers.append(RejectReason(RejectCode.MAINTENANCE_OVERDUE,
                                         f'maintenance overdue for plans {",".join(maintenance.overdue)}',
                                         {'equipment_id': eid, 'plans': list(maintenance.overdue)}))
        elif maintenance.active_work_order and equipment.state != EquipmentState.MAINTENANCE:
            blockers.append(RejectReason(RejectCode.MAINTENANCE_ACTIVE,
                                         'maintenance work order is in progress',
                                         {'equipment_id': eid}))
        elif maintenance.due:
            warnings.append(f'maintenance due for plans {",".join(maintenance.due)}')

    energy = energy_status(equipment, profile, fuel_reading, now)
    deadhead_km = deadhead_minutes = None
    headroom_pct = None
    if task is not None:
        if position is None:
            blockers.append(RejectReason(RejectCode.LOCATION_UNKNOWN,
                                         'equipment position is unknown',
                                         {'equipment_id': eid}))
        if not energy.known:
            blockers.append(RejectReason(RejectCode.ENERGY_UNKNOWN,
                                         f'energy status unavailable: {energy.detail}',
                                         {'equipment_id': eid}))

        if position is not None and profile is not None:
            deadhead_km = _distance(position, task.pickup)
            deadhead_minutes = deadhead_km / profile.avg_speed_kmh * 60.0
            if task.deadline is not None:
                earliest = now + timedelta(minutes=deadhead_minutes)
                if earliest > task.deadline:
                    blockers.append(RejectReason(RejectCode.DEADLINE_UNREACHABLE,
                                                 f'cannot reach pickup by {iso_z(task.deadline)}; '
                                                 f'earliest {iso_z(earliest)}',
                                                 {'equipment_id': eid, 'deadhead_minutes': deadhead_minutes,
                                                  'deadline': iso_z(task.deadline),
                                                  'earliest_arrival': iso_z(earliest)}))

        if energy.known and profile is not None:
            route_km = task.loaded_km + (deadhead_km or 0.0)
            shortage = _energy_shortfall(energy, profile, route_km)
            headroom_pct = shortage['headroom_pct']
            if 'message' in shortage:
                detail = {k: v for k, v in shortage.items()
                          if k not in {'message', 'headroom_pct'}}
                blockers.append(RejectReason(RejectCode.ENERGY_INSUFFICIENT,
                                             shortage['message'],
                                             {'equipment_id': eid, **detail}))
    else:
        if position is None:
            warnings.append('position unknown')
        if not energy.known:
            warnings.append(f'energy unknown: {energy.detail}')

    if energy.known and energy.percent is not None and energy.percent < low_energy_pct:
        warnings.append(f'{energy.metric.value} below {low_energy_pct:.0f}% reserve threshold')

    if blockers:
        level = AvailabilityLevel.BLOCKED
    elif warnings:
        level = AvailabilityLevel.CONSTRAINED
    else:
        level = AvailabilityLevel.DISPATCHABLE
    score = None
    if task is not None and not blockers:
        score = (deadhead_minutes if deadhead_minutes is not None else float('inf'),
                 -(headroom_pct if headroom_pct is not None else 0.0), eid)
    return EquipmentEvaluation(eid, level, tuple(blockers), tuple(warnings),
                               deadhead_km, deadhead_minutes, energy, headroom_pct, score)

def _energy_shortfall(energy: EnergyStatus, profile: OperatingProfile,
                      route_km: float) -> dict:
    if energy.metric == EnergyMetric.BATTERY:
        required_pct = route_km * profile.consumption_kwh_per_km / profile.battery_kwh * 100.0
        headroom_pct = energy.percent - profile.reserve_pct - required_pct
        out: dict = {'headroom_pct': headroom_pct, 'required_pct': required_pct,
                     'reserve_pct': profile.reserve_pct, 'available_pct': energy.percent}
        if energy.percent < profile.reserve_pct:
            out['message'] = (f'battery {energy.percent:.1f}% below reserve '
                              f'{profile.reserve_pct:.1f}%')
        elif required_pct > energy.percent - profile.reserve_pct:
            out['message'] = (f'job needs {required_pct:.1f}% battery but only '
                              f'{energy.percent - profile.reserve_pct:.1f}% is usable above reserve')
        return out
    route_hours = route_km / profile.avg_speed_kmh
    required_liters = profile.fuel_lph * route_hours
    reserve_liters = profile.tank_liters * profile.reserve_pct / 100.0
    headroom_liters = energy.percent / 100.0 * profile.tank_liters - reserve_liters - required_liters
    headroom_pct = headroom_liters / profile.tank_liters * 100.0
    out = {'headroom_pct': headroom_pct, 'required_liters': required_liters,
           'reserve_liters': reserve_liters}
    current_liters = energy.percent / 100.0 * profile.tank_liters
    if current_liters < reserve_liters:
        out['message'] = (f'fuel {current_liters:.1f}L below reserve {reserve_liters:.1f}L')
    elif required_liters > current_liters - reserve_liters:
        out['message'] = (f'job needs {required_liters:.1f}L but only '
                          f'{current_liters - reserve_liters:.1f}L is usable above reserve')
    return out
