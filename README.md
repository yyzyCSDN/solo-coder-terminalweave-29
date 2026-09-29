# TerminalWeave

TerminalWeave is a dependency-light Python engine for modelling day-to-day container-terminal operations. It covers marine access, berth use, quay cranes, yard placement, truck-gate appointments, customs holds, reefer monitoring, hazardous-goods segregation, equipment maintenance, rail blocks, demurrage billing and transshipment connections.

The package is intentionally usable without a database or web framework. Domain services expose ordinary Python APIs, while the persistence layer supplies an append-only event store and snapshots for embedding in larger systems.

## Quick start

```bash
python -m pip install -e ".[test]"
python -m terminalweave.cli demo
python -m pytest -q
```

Run the small HTTP adapter with `terminalweave serve`. It exposes `GET /health`,
`POST /tide/clearance`, and `POST /billing/demurrage`. The other packages are
library components intended to be composed by an embedding application.

## Design principles

- domain calculations are deterministic for identical inputs;
- time values are timezone-aware UTC datetimes;
- operational state transitions are explicit and validated;
- safety constraints fail closed when required information is absent;
- money uses `Decimal`;
- core domain modules depend only on the Python standard library.

## Main packages

`marine`, `cranes`, `yard`, `gate`, `customs`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `planning`, `persistence`, `reporting`, and `api`.

### Unified dispatch availability

`terminalweave.equipment.availability.AvailabilityBoard` folds the separate
equipment signals — fleet status, unresolved incidents, due/overdue
maintenance work orders, battery reserve or fuel margin (including estimated
burn for the trip), live position, and task deadlines — into one real-time
view.

```python
board = AvailabilityBoard(fleet, positions=..., incidents=..., maintenance=...,
                          fuel_tracker=..., fuel_levels=...)
view = board.view(now, task)          # every unit, with specific block reasons
decision = board.dispatch(task, now)  # best feasible unit or DispatchRejectedError
```

`board.dispatch` raises `DispatchRejectedError` carrying a concrete
`BlockReason` per candidate (wrong kind, low battery, overdue maintenance,
deadline unreachable, ...). Equipment status changes only release tasks that
have not started; in-progress work stays attached until completion.

