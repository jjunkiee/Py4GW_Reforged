# Refactor Baseline

Status: current — generated 2026-09-05 against a clean `main` at `8dfdcaf2`
Scope: dependency and architecture baseline for the planned large-scale refactor
Authority: `graph.json` for every number; current source for every verdict

This set exists to answer one question per component: **does it stay as-is, improve in
place, fold into a common owner, split, or go?** It is a baseline, not a plan. It
deliberately stops before sequencing, staging, or migration design.

That sequencing now lives in
[../plans/refactor-sequencing.md](../plans/refactor-sequencing.md), which schedules these
verdicts without adding to them. Where the plan and this register disagree about what a
component *is*, the register is the authority; the plan only decides *when*.

## Contents

| File | What it holds |
|---|---|
| [dependency-map.md](dependency-map.md) | Layer stack, runtime boundaries, per-layer Mermaid graphs, fan-in/fan-out tables, cycles, orphans, data coupling |
| [component-register.md](component-register.md) | One row per component: owner, layer, fan-in, fan-out, external consumers, verdict, rationale, blast radius, confidence |
| [tools/build_graph.py](tools/build_graph.py) | The committed generator |
| [tools/boot_closure.py](tools/boot_closure.py) | Load-time analyser over `graph.json`: boot closure, severance edges, ranked import cuts |
| [tools/boot_capture_compare.py](tools/boot_capture_compare.py) | Checks a live client's `sys.modules` capture against the static boot closure |
| [graph.json](graph.json) | `build_graph.py`'s raw output — the spine everything else sits on |

## Method

**Step 1 was mechanical.** `tools/build_graph.py` walks the in-scope Python with `ast`
and emits `graph.json`. Every number quoted in these documents comes from that file.
Nothing here is counted by hand.

**Step 2 was narrative.** Verdicts were written on top of that data, against current
source, and reconciled with the existing records listed below.

The order matters and is not decorative. The prose sits on the graph; it never replaces
it. Where the two disagree, the graph is the evidence and the prose is the claim.

### Regenerating the graph

```
python docs/architecture/refactor-baseline/tools/build_graph.py
```

Defaults: repository root inferred from the script location, output written to
`docs/architecture/refactor-baseline/graph.json`. Both are overridable:

```
python docs/architecture/refactor-baseline/tools/build_graph.py --repo-root . --out graph.json --quiet
```

Output is deterministic — every collection is sorted before serialisation — so a rerun on
an unchanged tree produces a byte-identical file (verified), and `git diff` on
`graph.json` is a meaningful signal.

`graph.json` is 4.5 MB. That is a deliberate choice, not an oversight: it is the raw
evidence these documents are checked against, and it regenerates in a few seconds from a
single command. It is also the reason `edges` and the per-module `imports`/`importers`
lists are both present despite overlapping — the first supports line-level citation, the
second supports lookup without a scan. If committed size later matters more than
convenience, the file can be dropped and regenerated on demand without losing anything.

The generator passes strict Pyright (`typeCheckingMode: strict`, 1 file analysed,
0 errors, pyright 1.1.411). It has no third-party dependencies and imports nothing from
this repository, so it runs outside the injected client.

### The boot closure

```
python docs/architecture/refactor-baseline/tools/boot_closure.py
```

`graph.json` answers *what imports what*. `boot_closure.py` answers the narrower question
the refactor needs: **what the injected client actually loads when the DLL runs the widget
host.** It re-parses each source file to separate module-level imports from the 1,141 that
sit inside function bodies and never execute at load — a distinction `graph.json` does not
record, and one that changes the startup surface from 487 modules to 288.

It also follows the package `__init__` files Python executes on the way to a submodule.
Those have no import statement, so `graph.json` records no edge for them, and a closure
built from edges alone misses both them and whatever they import. A live client confirmed
the corrected closure exactly; the uncorrected one missed 16 modules.

It reports the boot closure, the *severance edges* (imports from a retained area into the
consumer tier), and two rankings: which single import cut releases the most modules from the
boot closure, and which *module* releases the most if all of its module-level imports go.
The second exists because the first is blind to a facade - a module whose cost is spread
across dozens of edges, none individually significant. In this repository they disagree by a
factor of seven, and the second is the one Phase 2 works from. Output is deterministic and printed by default; `--out` writes JSON. Like the
generator it passes strict Pyright with 0 errors and imports nothing from this repository.

Its figures are consumed by [../plans/refactor-sequencing.md](../plans/refactor-sequencing.md).
All three tools are meant to be rerun at every phase boundary of that plan.

### Proving the boot closure against a live client

```
python docs/architecture/refactor-baseline/tools/boot_closure.py --out boot-closure.json
python docs/architecture/refactor-baseline/tools/boot_capture_compare.py
```

`boot_closure.py` is honest about its own limit: it measures *static* reachability at load
time and cannot see widget discovery, native callbacks, or `importlib` with a computed
argument. `boot_capture_compare.py` settles that. It reads a `sys.modules` capture taken
inside a running client and reports which repository modules loaded that the closure did
not predict — the direction that would put the cut ranking on sand — alongside the reverse,
and the cost of the widget tier that loads after bootstrap.

Taking the capture needs a temporary probe in the widget host, because only the host knows
when bootstrap has finished. The probe is not part of the shipped runtime; it is installed,
run once, and reverted. The procedure and the current result are recorded in
[../records/refactor-phase-1-boot-proof.md](../records/refactor-phase-1-boot-proof.md).

Unlike the other two, this tool consumes their published output rather than importing them,
so all three remain standalone. It passes strict Pyright with 0 errors and returns a
non-zero exit code when the capture contradicts the closure, so it can gate a phase.

### What the generator records

Import edges (`import`, `from`, relative, `importlib.import_module`/`__import__` with a
literal argument), per-module fan-in and fan-out, import cycles via iterative Tarjan SCC,
modules with zero external fan-in, references to files under `json/`, `Settings/`,
`data/` and `offsets/`, owned-persistence call sites, raw file-I/O sites, native-boundary
crossings, dynamic-dispatch sites, and broken in-repo imports.

### Scope

**In scope:** `Py4GWCoreLib`, `Widgets`, `Bots`, `py4gw_bridge`, `BridgeRuntime`,
`Py4GW_Reforged_Launcher`, all of `Sources/` (every author namespace, held to the same
standard as core), `Examples and tests/`, root-level `*.py` entry points, and data
coupling into `json/`, `Settings/`, `data/`, `offsets/`.

Root-level `*.py` is an addition to the stated list. `Py4GW_widget_manager.py` is the
always-on host the C++ DLL runs; excluding it would have hidden 38 inbound edges and one
member of the core import cycle. The inclusion is recorded in
`meta.include_root_level_scripts`.

**Out of scope:** `Assets/`, `stubs/`, `docs/` probe tools, and the sibling
`Py4GW_Reforged_Native` repository. Native-boundary crossings are *recorded* — which
in-scope module imports which binding module — but native internals are not analysed.

### Resolution confidence

6,929 edges, one per `(source, target)` pair, resolved as follows. This matters because a
mis-resolved edge produces a false verdict.

| Resolution | Edges | Meaning |
|---|---|---|
| `exact` | 5,030 | Dotted name matched a file directly |
| `relative-file` | 1,628 | `from .mod import X` resolved against the file tree |
| `relative-package` | 200 | Relative import resolved to a package `__init__.py` |
| `unique-leaf` | 58 | Heuristic: models `sys.path` mutation; weaker evidence |
| `sibling` | 9 | Bare name resolved to a file in the same directory |
| `member-of` | 4 | `from pkg.mod import Symbol` attributed to `pkg/mod.py` |

**98.8% is exact or path-resolved.** Only 67 edges rest on a heuristic, and ambiguous
references are reported rather than guessed.

Three resolution decisions are worth stating, because each one changed the numbers
materially and each was made to avoid a false claim:

- **Relative imports resolve by path, not by dotted name.** Much of this repository lives
  in directories that are not legal identifiers (`Bots/Example Bots/YAVB/`), so those
  packages have no dotted name at all — but `from .FSM import ...` inside them resolves
  fine at runtime. Resolving these by dotted name lost 42 real edges across 15 modules and
  made live modules look like orphans.
- **Edges are unique per `(source, target)`.** An earlier revision keyed them by
  `(target, kind)`, so `from X import A, B` counted twice. That inflated the edge total by
  roughly 60% and every fan-in figure with it.
- **Implicit namespace packages are resolved as such.** Most of this repository ships
  without `__init__.py`; treating a directory reference as broken produced 16 false
  positives, which were removed.

String literals that exactly name a real in-repo module are recorded as `string-ref` edges
(121 of them) to capture config-driven dispatch tables. They resolve **only** on an exact
module-path match — the leaf and sibling heuristics are refused for strings, because any
bare word matching a filename would otherwise manufacture an edge.

## How to read the register

One row per component. A **component** is a namespace (area plus its first subdirectory,
which is where ownership actually sits in this repository), plus an individual row for
any module large or distinctive enough to be a component in its own right.

**Verdicts** are exactly one of: `Keep` · `Improve in place` · `Fold into <named owner>` ·
`Split` · `Remove`.

Every verdict cites fan-in, who the consumers actually are, whether an existing owner
already provides the capability, and the blast radius on public contracts consumed by
`Sources/`, `Widgets/` and `Bots/`.

**Confidence** is `verified` (read at source, cited by file:line), `inferred` (reasoned
from the graph), or `unresolved` (could not be proven — recorded under Known Unknowns
instead of being guessed).

**Tangential/over-specialised flags** used throughout:

| Flag | Meaning |
|---|---|
| T1 | Zero fan-in outside its own namespace, or exactly one consumer |
| T2 | Duplicates a capability an existing core owner already provides |
| T3 | Reaches across a runtime boundary it does not own (native, shared memory, ImGui state, script lifecycle) |
| T4 | Reachable only from a single bot, widget or source script |
| T5 | Coupled to one config, profile or data-file shape such that it cannot serve others |

### The reachability rule — read before acting on any zero-fan-in row

Widgets are discovered at runtime by `os.walk` plus
`importlib.util.spec_from_file_location` in
`Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py` ([:889](../../../Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py#L889),
[:390](../../../Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py#L390)). Bots and `Sources/`
scripts are launched by path. Chat commands and message routing are table-driven.

**No static import edge ever points at a widget.** 181 of 188 widget modules show zero
fan-in, and that is the expected, healthy state. Reading zero fan-in as "dead" would
recommend deleting every widget in the repository.

There is, however, a second reachability test that *is* decisive, and the register uses it
where it applies. `WidgetManager` only loads `.py` files from a directory containing a
`.widget` marker file ([WidgetManager.py:891](../../../Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py#L891));
57 such directories exist. A widget module that is both unmarked **and** has zero fan-in is
unreachable by both mechanisms. Only two components meet that bar, and they are the only
`Remove`-on-reachability verdicts in the register.

Everywhere else the import graph cannot prove reachability, the register says `unresolved`
and the case is listed under Known Unknowns. That is deliberate. An honest unresolved entry
is more useful than a confident guess — and this baseline contains a worked example of why:
`Sources/ZaishenBounty` and `Sources/oazix` have identical graph signatures (zero fan-in,
zero external consumers) and turned out opposite ways. One is fully live through a path
loader; the other has never been able to run.

## Reconciliation with existing records

This set is standalone but not amnesiac. It cites and reconciles with:

- [py4-gw-conceptual-model.md](../reference/py4-gw-conceptual-model.md) — layer stack,
  canonical naming map, MCP boundary rule, known unknowns
- [pending-fixes.md](../records/pending-fixes.md) — PF-1 … PF-5, linked and not restated
- [frenkeylib-decision-autopsy.md](../records/reforged-migration/frenkeylib-decision-autopsy.md)
- [frenkeylib-migration-failure-and-rollback-record.md](../records/reforged-migration/frenkeylib-migration-failure-and-rollback-record.md)

Every disagreement with those documents is listed in
[dependency-map.md](dependency-map.md#reconciliation-with-existing-records), with a
statement of which is right on current source evidence.

### Controls inherited from the autopsy

The autopsy's *Non-negotiable controls for the replacement migration* bind this work.
Concretely, in this baseline:

- ownership analysis is kept out of product semantics — "who should own this code" is
  answered, "should this feature exist" is not;
- distinct systems are not conflated, even where they touch the same domain;
- no verdict proposes replacing a contract that must be persisted; where a verdict would
  break saved user data, the row says so in its blast-radius column;
- static conformance is not presented as progress. A clean import is not evidence that
  anything runs, and the register never treats it as such.
