# Architecture Project Records

This directory contains project-level architecture records that track active
issues and coordination designs without pretending they are current runtime
implementations.

- `pending-fixes.md` is the project issue pool. Each item has its own status,
  evidence, blast radius, and scope boundary; do not fix an item opportunistically.
- `whiteboard-architecture-cross-hero-cast-coordination.md` records the
  cross-hero lock design and its current implementation/verification notes.
- `refactor-phase-0-waypoint.md` records the measured zero-point for the
  refactor scheduled in `../plans/refactor-sequencing.md`: the `refactor-origin`
  tag, the boot-closure figures every later measurement is read against, and the
  diagnosed offline test baseline. It records results only; the
  `../refactor-baseline/component-register.md` keeps the component verdicts.
- `refactor-phase-1-boot-proof.md` holds the MVP gate sentence and the procedure
  for proving the static boot closure against a running client, plus the result
  once a capture has been taken. It is the only place the live-capture runbook
  lives; the probe it describes is temporary instrumentation, not shipped code.
- Current source, tests, and runtime evidence outrank any record here when they
  conflict with an observed implementation.
