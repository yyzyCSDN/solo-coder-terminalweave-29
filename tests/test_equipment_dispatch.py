from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import (CapacityError, ConflictError,
                                       NotFoundError, StateTransitionError)
from terminalweave.equipment.availability import (AvailabilityLevel, DispatchTask,
                                                  OperatingProfile, RejectCode)
from terminalweave.equipment.dispatch_board import DispatchBoard, DispatchRejected
from terminalweave.equipment.dispatch import Position
from terminalweave.equipment.fleet import Equipment, EquipmentState, FleetRegistry
from terminalweave.equipment.fuel import FuelEfficiencyTracker, FuelReading
from terminalweave.equipment.incidents import EquipmentIncident, IncidentBook
from terminalweave.equipment.maintenance import (EquipmentMeter, MaintenanceManager,
                                                 MaintenancePlan)

UTC = timezone.utc
NOW = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def _board():
    fleet = FleetRegistry()
    ev = Equipment('EV-1', 'terminal_tractor', capacity_tonnes=40,
                   state=EquipmentState.AVAILABLE, location='yard-A', battery_percent=90)
    dt = Equipment('DT-1', 'terminal_tractor', capacity_tonnes=50,
                   state=EquipmentState.AVAILABLE, location='yard-B')
    small = Equipment('EV-2', 'terminal_tractor', capacity_tonnes=10,
                      state=EquipmentState.AVAILABLE, location='yard-C', battery_percent=80)
    fleet.add(ev)
    fleet.add(dt)
    fleet.add(small)

    maintenance = MaintenanceManager()
    maintenance.register_plan(MaintenancePlan('P100', interval_hours=100))
    maintenance.register_meter(EquipmentMeter('EV-1', 50, NOW))
    maintenance.register_meter(EquipmentMeter('DT-1', 50, NOW))
    maintenance.register_meter(EquipmentMeter('EV-2', 50, NOW))

    incidents = IncidentBook()
    fuel = FuelEfficiencyTracker()
    fuel.ingest(FuelReading('DT-1', NOW, liters=200, engine_hours=10))

    board = DispatchBoard(fleet, maintenance=maintenance, incidents=incidents, fuel=fuel)
    board.register_profile(OperatingProfile('EV-1', avg_speed_kmh=20, battery_kwh=200,
                                            consumption_kwh_per_km=1.0))
    board.register_profile(OperatingProfile('EV-2', avg_speed_kmh=20, battery_kwh=200,
                                            consumption_kwh_per_km=1.0))
    board.register_profile(OperatingProfile('DT-1', avg_speed_kmh=20, fuel_lph=10,
                                            tank_liters=400))
    board.report_position('EV-1', Position(0, 0))
    board.report_position('DT-1', Position(3, 0))
    board.report_position('EV-2', Position(0, 0))
    return board, fleet, maintenance, incidents, fuel


def _task(task_id='T1', deadline=None, load_tonnes=30, pickup=None):
    return DispatchTask(task_id, 'terminal_tractor', load_tonnes,
                        pickup or Position(0, 0), Position(10, 0),
                        deadline=deadline, priority=1, created_at=NOW)


def test_availability_view_marks_dispatchable_and_surfaces_energy():
    board, *_ = _board()
    rows = {r.equipment_id: r for r in board.availability_view(NOW)}
    assert rows['EV-1'].level == AvailabilityLevel.DISPATCHABLE
    assert rows['EV-1'].energy_percent == pytest.approx(90)
    # diesel unit percent computed from tank size
    assert rows['DT-1'].energy_percent == pytest.approx(50)
    assert rows['DT-1'].autonomy_minutes == pytest.approx(1200)
    assert rows['EV-1'].blockers == ()


def test_manual_dispatch_success_and_completion():
    board, fleet, *_ = _board()
    board.add_task(_task(deadline=NOW + timedelta(hours=1)))
    assignment = board.assign('T1', 'EV-1', NOW)
    assert assignment.equipment_id == 'EV-1'
    assert assignment.evaluation.deadhead_km == 0
    board.start_task('T1', NOW + timedelta(minutes=1))
    assert fleet.get('EV-1').state == EquipmentState.WORKING
    board.complete_task('T1', NOW + timedelta(hours=1), Position(10, 0))
    assert fleet.get('EV-1').state == EquipmentState.AVAILABLE
    assert board.task_state('T1').value == 'done'


def test_rejection_carries_specific_reasons():
    board, *_ = _board()
    board.add_task(_task('HEAVY', load_tonnes=60))
    with pytest.raises(DispatchRejected) as exc:
        board.assign('HEAVY', 'EV-2', NOW)
    codes = {r.code for r in exc.value.reasons}
    assert RejectCode.INSUFFICIENT_CAPACITY in codes
    # auto-assign aggregating every candidate reason also fails closed
    with pytest.raises(DispatchRejected) as exc:
        board.auto_assign('HEAVY', NOW)
    assert exc.value.reasons and all(r.message for r in exc.value.reasons)


def test_fault_state_change_revokes_unstarted_assignment_but_spares_started():
    board, fleet, *_ = _board()
    board.add_task(_task('T1'))
    board.add_task(_task('T2'))
    board.assign('T1', 'EV-1', NOW)
    board.assign('T2', 'DT-1', NOW)
    board.start_task('T1', NOW)
    # fault reported after T1 started: it must keep running
    fleet.transition('EV-1', EquipmentState.OUT_OF_SERVICE)
    # T2's unit breaks down before start: revoked with a concrete reason
    fleet.transition('DT-1', EquipmentState.MAINTENANCE)
    revocations = board.refresh(NOW + timedelta(minutes=2))
    revoked_ids = {r.task_id for r in revocations}
    assert 'T2' in revoked_ids and 'T1' not in revoked_ids
    assert board.task_state('T2').value == 'pending'
    assert board.task_state('T1').value == 'in_progress'
    reason = board.last_revocation('T2')
    assert reason.reason_code in {RejectCode.MAINTENANCE_ACTIVE}
    assert reason.message
    # re-dispatch to the broken unit is refused with specific reasons
    with pytest.raises(DispatchRejected) as exc:
        board.assign('T2', 'DT-1', NOW)
    assert RejectCode.MAINTENANCE_ACTIVE in {r.code for r in exc.value.reasons}
    board.complete_task('T1', NOW + timedelta(hours=1))


def test_deadline_unreachable_is_a_blocker_but_charged_unit_recovers():
    board, fleet, *_ = _board()
    far_task = DispatchTask('FAR', 'terminal_tractor', 30, Position(100, 0),
                            Position(110, 0), deadline=NOW + timedelta(minutes=10),
                            created_at=NOW)
    board.add_task(far_task)
    with pytest.raises(DispatchRejected) as exc:
        board.assign('FAR', 'EV-1', NOW)
    assert RejectCode.DEADLINE_UNREACHABLE in {r.code for r in exc.value.reasons}
    # relax the deadline -> same unit qualifies
    relaxed = DispatchTask('FAR2', 'terminal_tractor', 30, Position(0.5, 0),
                           Position(1, 0), deadline=NOW + timedelta(hours=2), created_at=NOW)
    board.add_task(relaxed)
    assignment = board.auto_assign('FAR2', NOW)
    assert assignment.equipment_id == 'EV-1'


def test_energy_insufficient_blocks_and_fuel_units_are_checked():
    board, *_ = _board()
    # 90% of 200 kWh = 180 kWh usable total; route 10 km at 1 kWh/km, reserve 20% = 40 kWh: fine
    # push route beyond range
    long_task = DispatchTask('LONG', 'terminal_tractor', 30, Position(0, 0),
                             Position(150, 0), deadline=NOW + timedelta(hours=10),
                             created_at=NOW)
    board.add_task(long_task)
    with pytest.raises(DispatchRejected) as exc:
        board.assign('LONG', 'EV-1', NOW)
    assert RejectCode.ENERGY_INSUFFICIENT in {r.code for r in exc.value.reasons}

    # diesel: 200 L, reserve 80 L, 10 L/h at 20 km/h -> 120 L covers 240 km
    fuel_task = DispatchTask('FUEL', 'terminal_tractor', 30, Position(3, 0),
                             Position(500, 0), deadline=NOW + timedelta(hours=30),
                             created_at=NOW)
    board.add_task(fuel_task)
    with pytest.raises(DispatchRejected) as exc:
        board.assign('FUEL', 'DT-1', NOW)
    assert RejectCode.ENERGY_INSUFFICIENT in {r.code for r in exc.value.reasons}


def test_missing_position_or_energy_fails_closed():
    board, fleet, *_ = _board()
    unknown = Equipment('EV-9', 'terminal_tractor', 40, battery_percent=90)
    fleet.add(unknown)
    board.register_profile(OperatingProfile('EV-9', avg_speed_kmh=20, battery_kwh=200,
                                            consumption_kwh_per_km=1.0))
    board.add_task(_task('T9'))
    with pytest.raises(DispatchRejected) as exc:
        board.assign('T9', 'EV-9', NOW)
    codes = {r.code for r in exc.value.reasons}
    assert RejectCode.LOCATION_UNKNOWN in codes

    # no fuel reading for a diesel unit -> energy unknown blocker
    diesel = Equipment('DT-9', 'terminal_tractor', 40)
    fleet.add(diesel)
    board.register_profile(OperatingProfile('DT-9', avg_speed_kmh=20, fuel_lph=10,
                                            tank_liters=400))
    board.report_position('DT-9', Position(0, 0))
    board.add_task(_task('T10'))
    with pytest.raises(DispatchRejected) as exc:
        board.assign('T10', 'DT-9', NOW)
    assert RejectCode.ENERGY_UNKNOWN in {r.code for r in exc.value.reasons}


def test_open_severe_incident_blocks_resolving_recovers():
    board, fleet, maintenance, incidents, _ = _board()
    incidents.add(EquipmentIncident('I1', 'EV-1', NOW, severity=4, category='brake'))
    board.add_task(_task())
    with pytest.raises(DispatchRejected) as exc:
        board.assign('T1', 'EV-1', NOW)
    assert RejectCode.INCIDENT_OPEN in {r.code for r in exc.value.reasons}
    incidents.resolve('I1', NOW + timedelta(hours=1))
    assignment = board.assign('T1', 'EV-1', NOW + timedelta(hours=1))
    assert assignment.equipment_id == 'EV-1'
    # minor incidents only constrain
    incidents.add(EquipmentIncident('I2', 'DT-1', NOW, severity=2, category='mirror'))
    rows = {r.equipment_id: r for r in board.availability_view(NOW)}
    assert rows['DT-1'].level == AvailabilityLevel.CONSTRAINED


def test_overdue_maintenance_blocks_due_is_warning():
    board, fleet, maintenance, *_ = _board()
    meter = maintenance._meters['DT-1']
    meter.hours = 201  # beyond 100 interval
    status = maintenance.maintenance_status('DT-1')
    assert status.overdue == ('P100',)
    board.add_task(_task('T1'))
    with pytest.raises(DispatchRejected) as exc:
        board.assign('T1', 'DT-1', NOW)
    assert RejectCode.MAINTENANCE_OVERDUE in {r.code for r in exc.value.reasons}

    meter.hours = 100  # exactly due, inside grace
    rows = {r.equipment_id: r for r in board.availability_view(NOW)}
    assert rows['DT-1'].level == AvailabilityLevel.CONSTRAINED


def test_equipment_busy_with_other_task_and_unknown_unit_rejection():
    board, *_ = _board()
    board.add_task(_task('T1'))
    board.add_task(_task('T2'))
    board.assign('T1', 'EV-1', NOW)
    with pytest.raises(DispatchRejected) as exc:
        board.assign('T2', 'EV-1', NOW)
    assert RejectCode.EQUIPMENT_BUSY in {r.code for r in exc.value.reasons}
    with pytest.raises(DispatchRejected) as exc:
        board.assign('T2', 'NOPE', NOW)
    assert RejectCode.UNKNOWN_EQUIPMENT in {r.code for r in exc.value.reasons}


def test_auto_assign_picks_nearest_dispatchable():
    board, *_ = _board()
    # DT-1 sits at (3,0), EV-1 at (0,0); pickup at (2,0) -> DT-1 closer
    board.add_task(_task('T1', pickup=Position(2, 0)))
    assignment = board.auto_assign('T1', NOW)
    assert assignment.equipment_id == 'DT-1'
    assert assignment.evaluation.deadhead_km == pytest.approx(1.0)


def test_cancel_and_invalid_lifecycle_transitions():
    board, *_ = _board()
    board.add_task(_task())
    with pytest.raises(StateTransitionError):
        board.start_task('T1', NOW)  # never assigned
    board.assign('T1', 'EV-1', NOW)
    board.cancel_task('T1')
    assert board.task_state('T1').value == 'cancelled'
    with pytest.raises(ConflictError):
        board.assign('T1', 'EV-1', NOW)
    with pytest.raises(NotFoundError):
        board.task_state('missing')


def test_dispatch_rejected_is_capacity_error():
    board, *_ = _board()
    board.add_task(_task(load_tonnes=999))
    with pytest.raises(CapacityError):
        board.auto_assign('T1', NOW)
