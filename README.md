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

### Real-time equipment dispatchability

`equipment.availability` aggregates every ledger signal — equipment state,
unresolved incidents, due/overdue maintenance plans, battery or fuel reserve,
reported position and task deadline — into one dispatchability view
(`dispatchable` / `constrained` / `blocked`). `equipment.dispatch_board.DispatchBoard`
sits on top of the existing `FleetRegistry` and observes state transitions:

- `availability_view(now, kind=...)` gives dispatchers the unified real-time list;
- `assign` / `auto_assign` pick equipment for a task, and every refusal raises
  `DispatchRejected` carrying structured `RejectReason` entries with concrete
  codes and messages (fault, overdue maintenance, energy shortfall, unreachable
  deadline, unknown location, capacity/kind mismatch, busy, ...);
- fleet state changes only revoke **not-yet-started** assignments via
  `refresh(now)`; in-progress tasks keep running and the unit becomes
  unavailable for new work.
