# Architecture Plans

This folder contains proposed, multi-step work on the project's structure and
ownership: sequencing, staging, and migration design that spans subsystems.
Plans are not current runtime contracts and never describe current behavior.

Authority order for claims in this folder:

1. Current owning Python or native source, stubs, and build configuration.
2. Injected-client observation and reproducible offline tests.
3. The generated evidence in `../refactor-baseline/` - `graph.json` and the
   reports its tools produce.
4. The narrative records in `../reference/` and `../records/`.
5. The plans here, which are the least authoritative of the five and are
   superseded the moment the work they schedule is done differently.

A plan that schedules work already recorded elsewhere cites the owning record
rather than restating it. Where a plan and the component register disagree
about what a component *is*, the register wins; the plan only decides *when*.

## Map

- `refactor-sequencing.md` - proposed staging order for the bottom-to-top
  refactor: truncate to a minimal launcher-booting baseline, cut the import
  edges that dominate startup, delete the consumer tier, refactor the core
  while nothing depends on it, then re-admit functionality layer by layer.
  Carries the boot-closure metric and the per-phase verification loop. Its
  verdicts come from `../refactor-baseline/component-register.md`; it adds
  sequencing only.

## Reproducing the numbers

Every figure in this folder comes from one of two committed generators, and
both are deterministic - a rerun on an unchanged tree is byte-identical, so
`git diff` on their output is a meaningful signal:

```
python docs/architecture/refactor-baseline/tools/build_graph.py
python docs/architecture/refactor-baseline/tools/boot_closure.py
```

The first rewrites `graph.json`, the static import graph. The second reads it
and reports the boot closure, the severance edges, and the ranked import cuts.
A third, `boot_capture_compare.py`, checks those static figures against a
`sys.modules` capture taken inside a running client. None imports anything from
this repository, so all three run outside the injected client, and all three
pass Pyright in `strict` mode with zero errors.

Regenerate both at every phase boundary. A plan whose numbers have not been
refreshed against current source is a historical record wearing a proposal's
status field.
