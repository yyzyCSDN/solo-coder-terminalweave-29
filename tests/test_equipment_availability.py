from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import DispatchRejectedError, StateTransitionError
from terminalweave.equipment.availability import (
    AvailabilityBoard,
    DispatchTask,
    FuelLevel,
    Position,
    ReasonCode,
    TaskState,
)
from terminalweave.equipment.fleet import Equipment, EquipmentState, FleetRegistry
from terminalweave.equipment.fuel import FuelEfficiencyTracker, FuelReading
from terminalweave.equipment.incidents import EquipmentIncident, IncidentBook
from terminalweave.equipment.maintenance import (
    EquipmentMeter,
    MaintenanceManager,
    MaintenancePlan,
)

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def make_fleet():
    fleet = FleetRegistry()
    fleet.add(Equipment('E1', 'yard_truck', 40, location='A', battery_percent=90))
    fleet.add(Equipment('E2', 'yard_truck', 40, location='B', battery_percent=95))
    fleet.add(Equipment('E3', 'yard_truck', 40, location='D', battery_percent=85))
    fleet.add(Equipment('F1', 'forklift', 5, location='C', battery_percent=50))
    return fleet


def make_board(**kwargs):
    fleet = make_fleet()
    positions = {
        'E1': Position(0, 0),
        'E2': Position(10, 0),
        'E3': Position(9, 0),
        'F1': Position(1, 1),
    }
    board = AvailabilityBoard(fleet, positions=positions, **kwargs)
    return board, fleet


def task(job='J1', kind='yard_truck', load=20, pickup=Position(8, 0),
         dropoff=Position(9, 0), deadline=None, **kwargs):
    return DispatchTask(
        job, kind, load, pickup, dropoff,
        deadline or T0 + timedelta(hours=2), **kwargs)


def test_unified_view_blends_all_signals_and_picks_nearest():
    board, fleet = make_board()
    result = board.dispatch(task(), T0)
    # pickup (8,0): E3 at (9,0) is nearest at distance 1
    assert result.equipment_id == 'E3'
    assert fleet.items['E3'].state == EquipmentState.WORKING
    assert board.task_state('J1') == TaskState.ASSIGNED

    view = board.view(T0, task(job='J2'))
    e3 = next(i for i in view.items if i.equipment.equipment_id == 'E3')
    assert not e3.available_for_task
    assert {r.code for r in e3.reasons} == {ReasonCode.ALREADY_ASSIGNED}
    # the other trucks remain dispatchable
    assert {i.equipment.equipment_id for i in view.dispatchable} == {'E1', 'E2'}


def test_dispatch_rejected_lists_specific_reason_per_equipment():
    board, fleet = make_board()
    # forklift: wrong kind; E1/E2: low battery once reserve + demand applied
    heavy = task(job='BIG', load=20, battery_need_pct=80)
    with pytest.raises(DispatchRejectedError) as exc:
        board.dispatch(heavy, T0)
    err = exc.value
    codes = {eid: {r.code for r in reasons}
             for eid, reasons in err.reasons_by_equipment.items()}
    assert codes['E1'] == {ReasonCode.LOW_BATTERY}
    assert codes['E2'] == {ReasonCode.LOW_BATTERY}
    assert codes['E3'] == {ReasonCode.LOW_BATTERY}
    assert ReasonCode.WRONG_KIND in codes['F1']
    assert 'low_battery' in str(err) and 'wrong_kind' in str(err)


def test_capacity_shortfall_has_its_own_reason():
    board, fleet = make_board()
    over = task(job='OVER', load=45)
    with pytest.raises(DispatchRejectedError) as exc:
        board.dispatch(over, T0)
    e1 = exc.value.reasons_by_equipment['E1']
    assert ReasonCode.INSUFFICIENT_CAPACITY in {r.code for r in e1}
    assert any('45' in r.detail for r in e1)


def test_unresolved_severe_fault_blocks_until_resolved():
    book = IncidentBook()
    book.add(EquipmentIncident('I1', 'E3', T0 - timedelta(hours=1), 5, 'hydraulic'))
    board, fleet = make_board(incidents=book)

    view = board.view(T0, task())
    e3 = next(i for i in view.items if i.equipment.equipment_id == 'E3')
    assert not e3.available_for_task
    assert {r.code for r in e3.reasons} == {ReasonCode.ACTIVE_FAULT}
    # selection steers around the faulty nearest unit to E2 (distance 2)
    result = board.dispatch(task(job='J1'), T0)
    assert result.equipment_id == 'E2'

    # resolving the fault clears the block and E3 becomes the best pick again
    book.resolve('I1', T0)
    view = board.view(T0 + timedelta(minutes=1), task(job='J2'))
    e3 = next(i for i in view.items if i.equipment.equipment_id == 'E3')
    assert e3.available_for_task


def test_minor_incident_does_not_block():
    book = IncidentBook()
    book.add(EquipmentIncident('I1', 'E2', T0, 2, 'cosmetic'))
    board, fleet = make_board(incidents=book)
    result = board.dispatch(task(), T0)
    # nearest unit is E3 regardless; E2's low-severity incident keeps it eligible
    assert result.equipment_id == 'E3'
    view = board.view(T0, task(job='J2'))
    e2 = next(i for i in view.items if i.equipment.equipment_id == 'E2')
    assert e2.available_for_task


def test_maintenance_overdue_and_due_block_with_distinct_reasons():
    mgr = MaintenanceManager()
    mgr.register_plan(MaintenancePlan('P100', interval_hours=100, grace_hours=5))
    mgr.register_meter(EquipmentMeter('E1', 102, T0))   # due, within grace
    mgr.register_meter(EquipmentMeter('E2', 110, T0))   # overdue
    mgr.register_meter(EquipmentMeter('E3', 130, T0))   # overdue
    mgr.open_due_orders(T0)

    board, fleet = make_board(maintenance=mgr)
    with pytest.raises(DispatchRejectedError) as exc:
        board.dispatch(task(), T0)
    codes = {eid: {r.code for r in reasons}
             for eid, reasons in exc.value.reasons_by_equipment.items()}
    assert codes['E1'] == {ReasonCode.MAINTENANCE_DUE}
    assert codes['E2'] == {ReasonCode.MAINTENANCE_OVERDUE}
    assert codes['E3'] == {ReasonCode.MAINTENANCE_OVERDUE}


def test_maintenance_due_can_be_warn_only_overdue_still_blocks():
    mgr = MaintenanceManager()
    mgr.register_plan(MaintenancePlan('P100', interval_hours=100))
    mgr.register_meter(EquipmentMeter('E1', 100, T0))
    mgr.register_meter(EquipmentMeter('E2', 130, T0))
    mgr.register_meter(EquipmentMeter('E3', 130, T0))
    mgr.open_due_orders(T0)

    board, fleet = make_board(maintenance=mgr, block_when_maintenance_due=False)
    # the two nearer trucks are overdue and refused; the due-only E1 takes it
    result = board.dispatch(task(), T0)
    assert result.equipment_id == 'E1'
    for eid in ('E2', 'E3'):
        item = next(i for i in board.view(T0, task(job='J2')).items
                    if i.equipment.equipment_id == eid)
        assert ReasonCode.MAINTENANCE_OVERDUE in {r.code for r in item.reasons}


def test_deadline_unreachable_explains_eta():
    # pickup (8,0): E2 is 2 away (ETA 6 min), E3 is 1 away (ETA 3 min),
    # E1 is 8 away (ETA 24 min)
    board, fleet = make_board()
    tight = task(deadline=T0 + timedelta(minutes=2))  # every truck misses it
    with pytest.raises(DispatchRejectedError) as exc:
        board.dispatch(tight, T0)
    for eid in ('E1', 'E2', 'E3'):
        assert ReasonCode.DEADLINE_UNREACHABLE in {
            r.code for r in exc.value.reasons_by_equipment[eid]}


def test_deadline_steers_choice_to_closer_unit():
    board, fleet = make_board()
    # E3 at distance 1 (ETA 3 min) is the only unit inside a 5-minute window
    tight = task(deadline=T0 + timedelta(minutes=5))
    result = board.dispatch(tight, T0)
    assert result.equipment_id == 'E3'


def test_missing_position_blocks_task_dispatch():
    board, fleet = make_board()
    # only the nearest truck has no position report; it must be ruled out
    del board.positions['E3']
    result = board.dispatch(task(), T0)
    assert result.equipment_id == 'E2'
    e3 = next(i for i in board.view(T0, task(job='J2')).items
              if i.equipment.equipment_id == 'E3')
    assert ReasonCode.LOCATION_UNKNOWN in {r.code for r in e3.reasons}


def test_max_deadhead_filter():
    board, fleet = make_board(max_deadhead=1.5)
    # pickup (8,0): E3 distance 1 qualifies, E2 distance 2 and E1 distance 8 do not
    result = board.dispatch(task(), T0)
    assert result.equipment_id == 'E3'
    view = board.view(T0, task(job='J2'))
    for eid in ('E1', 'E2'):
        item = next(i for i in view.items if i.equipment.equipment_id == eid)
        assert ReasonCode.OUT_OF_RANGE in {r.code for r in item.reasons}


def test_fuel_margin_uses_estimated_burn_rate():
    fleet = FleetRegistry()
    # diesel unit has no battery_percent
    fleet.add(Equipment('D1', 'yard_truck', 40, location='A'))
    tracker = FuelEfficiencyTracker()
    # 10 L/h burn: 100 L -> 90 L over one engine hour
    tracker.ingest(FuelReading('D1', T0 - timedelta(hours=2), 100, 10))
    tracker.ingest(FuelReading('D1', T0 - timedelta(hours=1), 90, 11))
    board = AvailabilityBoard(
        fleet,
        positions={'D1': Position(0, 0)},
        fuel_tracker=tracker,
        fuel_levels={'D1': FuelLevel('D1', 5, T0)},
        fuel_reserve_liters=2)
    # pickup 80 units away + 1 loaded at speed 20 => ~4 engine hours => ~40 L
    with pytest.raises(DispatchRejectedError) as exc:
        board.dispatch(task(), T0)
    assert ReasonCode.LOW_FUEL in {
        r.code for r in exc.value.reasons_by_equipment['D1']}

    # with a full tank the same job dispatches
    board.update_fuel_level(FuelLevel('D1', 200, T0))
    result = board.dispatch(task(job='J2'), T0)
    assert result.equipment_id == 'D1'


def test_state_change_releases_only_unstarted_job():
    board, fleet = make_board()
    # queue Q nearest E3; force S onto E2 with a pickup right next to it
    queued = board.dispatch(task(job='Q'), T0)
    started = board.dispatch(
        task(job='S', pickup=Position(10, 0), dropoff=Position(11, 0)), T0)
    assert queued.equipment_id == 'E3'
    assert started.equipment_id == 'E2'
    board.start_task('S', T0 + timedelta(minutes=1))

    effect_q = board.change_equipment_state(
        'E3', EquipmentState.CHARGING, T0 + timedelta(minutes=2))
    effect_s = board.change_equipment_state(
        'E2', EquipmentState.MAINTENANCE, T0 + timedelta(minutes=3))

    assert effect_q.released_job_ids == ('Q',)
    assert effect_q.unaffected_job_ids == ()
    assert effect_s.released_job_ids == ()
    assert effect_s.unaffected_job_ids == ('S',)

    # queued job is back in the pool and re-dispatchable to another unit
    assert board.task_state('Q') == TaskState.PENDING
    assert board.assigned_equipment('Q') is None
    again = board.dispatch(task(job='Q'), T0 + timedelta(minutes=4))
    assert again.equipment_id in {'E1', 'E2', 'E3'} and again.equipment_id != 'E3'

    # started job is still attached and survives return-to-service
    assert board.task_state('S') == TaskState.IN_PROGRESS
    effect = board.return_to_service('E2', T0 + timedelta(minutes=5))
    assert effect.unaffected_job_ids == ('S',)
    assert fleet.items['E2'].state == EquipmentState.WORKING
    board.complete_task('S', T0 + timedelta(minutes=6))
    assert board.task_state('S') == TaskState.DONE


def test_cancel_started_task_is_refused():
    board, fleet = make_board()
    board.dispatch(task(job='J1'), T0)
    board.start_task('J1', T0 + timedelta(minutes=1))
    with pytest.raises(StateTransitionError):
        board.cancel_task('J1', T0 + timedelta(minutes=2))
    board.complete_task('J1', T0 + timedelta(minutes=3))
    assert fleet.items['E3'].state == EquipmentState.AVAILABLE


def test_completing_task_frees_unit_for_next_job():
    board, fleet = make_board()
    # tie up every truck so the second job must wait for a completion
    board.dispatch(task(job='J1'), T0)
    board.dispatch(
        task(job='J2', pickup=Position(10, 0), dropoff=Position(11, 0)), T0)
    board.dispatch(
        task(job='J3', pickup=Position(0, 0), dropoff=Position(-1, 0)), T0)
    board.start_task('J1', T0 + timedelta(minutes=1))
    with pytest.raises(DispatchRejectedError):
        board.dispatch(task(job='J4'), T0 + timedelta(minutes=2))
    board.complete_task('J1', T0 + timedelta(minutes=3))
    result = board.dispatch(task(job='J4'), T0 + timedelta(minutes=4))
    assert result.equipment_id == 'E3'


def test_view_without_task_reports_general_dispatchability():
    book = IncidentBook()
    book.add(EquipmentIncident('I1', 'E2', T0, 5, 'engine'))
    board, fleet = make_board(incidents=book)
    fleet.transition('F1', EquipmentState.CHARGING)
    view = board.view(T0)
    by_id = {i.equipment.equipment_id: i for i in view.items}
    assert by_id['E1'].available_for_task
    assert not by_id['E2'].available_for_task
    assert not by_id['F1'].available_for_task
    assert {r.code for r in by_id['E2'].reasons} == {ReasonCode.ACTIVE_FAULT}
    assert {r.code for r in by_id['F1'].reasons} == {ReasonCode.NOT_AVAILABLE}
