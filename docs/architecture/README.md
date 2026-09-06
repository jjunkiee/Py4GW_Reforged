# Architecture Documentation

Use this topic for the project model, ownership, governance, and durable
project records. Current source and runtime evidence outrank these records
when they conflict.

- `reference/` contains the conceptual model and derived feature inventory.
- `guides/` contains the traceable-refactor and migration review guide.
- `records/` contains pending work, whiteboards, the Reforged migration
  record, and the refactor Phase 0 waypoint - the measured zero-point the
  refactor is read against.
- `refactor-baseline/` contains the dependency-and-architecture baseline for the
  planned large-scale refactor: committed generators, the raw `graph.json`, the
  dependency map, and a per-component verdict register. It answers *what each
  component is and who owns it*, and stops before sequencing.
  Its numbers are regenerated from source, not hand-maintained — run
  `python docs/architecture/refactor-baseline/tools/build_graph.py` and
  `python docs/architecture/refactor-baseline/tools/boot_closure.py` before
  relying on them.
- `plans/` contains proposed multi-step structural work. It currently holds the
  refactor sequencing plan — the staging, demolition order, and per-phase
  verification loop built on the baseline. The baseline is the evidence; a plan
  only decides when the work happens.
