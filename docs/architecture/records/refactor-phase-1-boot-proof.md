# Refactor Phase 1 Boot Proof

Status: current; runtime-verified on 2026-09-06
Scope: the MVP gate sentence, and proving the static boot closure against a running client
Authority: [refactor-sequencing.md](../plans/refactor-sequencing.md) schedules this work;
`boot_capture_compare.py` produces the verdict; the live client is the only proof

Phase 1 has two jobs. Settle the one sentence the whole plan hangs on, and find out
whether the boot closure is true of a running client or only of the source.

Both are done. The answer to the second was no, twice, for two different reasons - and
correcting the second of them removed the plan's central demolition charge. See
[Result](#result); the procedure below is kept because Phase 2 reruns it after every cut.

## The MVP gate

> The launcher starts `Gw.exe`, injects the DLL, the host bootstraps, and the launchpad
> renders - with zero widgets present.

Testable by eye in under a minute, and it proves the runtime is alive end to end: process
launch, injection, Python host, settings resolution, ImGui draw path. The boot path is
[`Py4GW_widget_manager.py`](../../../Py4GW_widget_manager.py), whose three module-level
imports are `Settings`, `WidgetManager` and `launch_bar/launchpad`.

Whether `Widgets/System` is inside or outside the gate was left open until the capture
could price it. It now has a number - 6 modules, 6,188 lines - and the reasoning is under
[The `Widgets/System` question](#the-widgetssystem-question).

## Why this needs a live client at all

`boot_closure.py` is candid about its own limit: it measures static reachability at load
time. Three things are invisible to it, and all three are real in this repository.

| Invisible to the static tool | Where it happens |
|---|---|
| Widget discovery by path | `WidgetManager.load_module()` uses `spec_from_file_location` |
| Native callbacks | widgets run via C++ `PyCallbacks` after enable |
| `importlib` with a computed argument | recorded as dynamic sites by `build_graph.py` |

If the live client loads repository modules at the import stage that the closure does not
predict, the ranked cuts in Phase 2 are ordered against the wrong set and the effort spent
on them is wasted. That is the question this procedure answers, and it costs one game
launch.

## How the capture works

The probe lives in the widget host, because only the host knows when bootstrap has
finished. It is **temporary instrumentation, installed to be measured with and then
reverted** - it is not part of the shipped runtime.

It records `sys.modules` at three points, which are not interchangeable:

| Stage | Taken | What it proves |
|---|---|---|
| `import` | after the host's module-level imports, before anything else runs | The only stage comparable to the static boot closure. A repository module here that the closure did not predict is the finding that matters. |
| `post_discover` | after `WidgetHandler.discover()` | Despite the name, this is where widget code executes. `_load_widget_module()` reads each widget's saved enabled state and calls `enable()`, which loads the module ([`WidgetManager.py:940`](../../../Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py#L940)). Measured live: 152 widget modules here, 0 at `import`. |
| `post_bootstrap` | after `_apply_ini_configuration()` | Re-applies saved state and force-enables the `Widgets/System` tier, but everything is already loaded, so it adds nothing to `sys.modules`. The growth from `import` is the startup cost the MVP gate proposes to stop paying. |

The write happens in a `finally` block. `INI_KEY` is set before discovery, so a widget
raising during enable would otherwise prevent `_bootstrap_once()` from ever running again
and cost a whole game launch for nothing.

## Runbook

**The probe is currently reverted.** To take another capture, first reinstall it with
`git revert <the revert commit>`, which restores the probe as a fresh commit and keeps the
history honest about when instrumentation was present.

Do these in order. Every field is spelled out; nothing here relies on a setting being left
over from a previous run.

### Step 1 - confirm the probe is installed

Open a terminal at the repository root and run:

```
git branch --show-current
grep -c "_write_boot_capture" Py4GW_widget_manager.py
```

Expected: the branch is `refactor/truncation`, and the `grep` count is **2** - the probe's
writer function and the one call to it. If the count is `0`, the probe has already been
reverted and the capture cannot be taken.

### Step 2 - clear any stale capture

```
rm -f boot-capture.json
```

This matters. The probe writes once per client launch, and a leftover file from an earlier
run is indistinguishable from a fresh one on inspection.

### Step 3 - launch the client normally

Use the launcher exactly as you normally would. Do not change your widget configuration
first - the point is to measure the configuration you actually run, including whatever the
`Widgets/System` tier currently drags in.

1. Start `Py4GW_Reforged_Launcher`.
2. Select your usual account.
3. Launch Guild Wars and let it inject.
4. Wait until the **launchpad is visible on screen**. That is the signal that
   `_bootstrap_once()` has completed and the capture has been written.

If the launchpad never appears, stop and say so. The probe is wrapped so it cannot prevent
the launchpad from rendering, so a missing launchpad is a separate problem and I would
rather look at it than have you fight it.

### Step 4 - confirm the capture exists

Leave the client running or close it, either is fine. Then:

```
ls -l boot-capture.json
```

Expected: a file of roughly 100-400 KB at the repository root, timestamped within the last
few minutes.

If it is missing, check the in-game console for a line beginning `boot capture failed:` and
report that line verbatim - the probe logs its own failure rather than dying silently.

### Step 5 - report back

Tell me the file exists and I will run the comparison and interpret it. If you would rather
run it yourself, it is exactly these two commands:

```
python docs/architecture/refactor-baseline/tools/boot_closure.py --out boot-closure.json
python docs/architecture/refactor-baseline/tools/boot_capture_compare.py
```

The second exits non-zero when the capture contradicts the closure, and prints one of two
verdicts: `RANKING STANDS` or `RANKING AT RISK`.

**Do not run `build_graph.py` while the probe is installed.** Every other phase boundary
requires it, which is exactly why it is worth calling out here. The probe adds 68 lines to
`Py4GW_widget_manager.py`, and that file is the boot root: regenerating would take the host
from 87 lines to 155 in the committed zero-point, silently inflating the boot closure with
instrumentation. The graph is already current as of the last commit before the probe, and
`boot_closure.py` reads the committed graph rather than rescanning the tree, so it is
unaffected. Regenerate normally once the probe is reverted.

### Step 6 - remove the probe

Not yours to do. Once the capture is interpreted the probe is reverted and this record is
completed with the result. `Py4GW_widget_manager.py` is the boot root the refactor exists to
shrink; leaving 68 lines of instrumentation in it would corrupt the metric.

## Result

**Resolved on 2026-09-06. The boot closure now matches the live client exactly - after two
corrections, one to the probe and one to the analyser. The cut ranking did not survive.**

Final comparison, against the second capture:

| | Modules | Lines |
|---|---|---|
| Static boot closure | 288 | 134,644 |
| Live, at the `import` stage | 288 | 134,644 |
| Loaded but not predicted | 0 | - |
| Predicted but not loaded | 0 | - |

An exact match in both directions. The boot closure is now a runtime-confirmed measurement
rather than a static claim, and [Known Unknown 1](../plans/refactor-sequencing.md#known-unknowns)
is closed.

### The finding: the facade gets there first

The sequencing plan's demolition charge was
`WidgetManager.py:17 -> Py4GWCoreLib.GlobalCache`, credited with releasing 180 modules and
89,091 lines - "eighty-nine thousand lines to obtain a shared-memory handle". **Cutting it
releases nothing.** Traced through the corrected closure:

```
Py4GW_widget_manager.py:18
    imports Py4GWCoreLib.py4gwcorelib_src.Settings
        which forces Python to execute Py4GWCoreLib/__init__.py
            whose line 120 is  from .GlobalCache import GLOBAL_CACHE
```

All three of the host's module-level imports are `Py4GWCoreLib.*`. Importing *anything*
inside that package executes `Py4GWCoreLib/__init__.py` first, and that file imports
`GlobalCache` - constructing the singleton - regardless of what `WidgetManager` does. The
`GlobalCache` cut was never load-bearing; it was shadowed by a path nobody had drawn.

This closes [Known Unknown 3](../plans/refactor-sequencing.md#known-unknowns), and closes it
harder than it was asked. The facade is not merely *possibly* load-bearing at runtime: it is
**structurally unavoidable**. No import into `Py4GWCoreLib` can skip it.

### The corrected work queue

Emptying `Py4GWCoreLib/__init__.py` of its 41 module-level imports releases **122 modules
and 62,414 lines** - taking the boot floor from 288/134,644 to 166/72,230. Nothing else is
close:

| Owner | Module-level imports | Releases |
|---|---|---|
| `Py4GWCoreLib/__init__.py` | 41 | 122 modules / 62,414 loc |
| `Py4GWCoreLib/Routines.py` | 11 | 43 modules / 26,653 loc |
| `Py4GWCoreLib/Botting.py` | 21 | 37 modules / 8,221 loc |
| `Py4GWCoreLib/GlobalCache/SharedMemory.py` | 16 | 29 modules / 4,415 loc |
| `Py4GWCoreLib/routines_src/BehaviourTrees.py` | 14 | 22 modules / 18,027 loc |

The best *single edge* now releases 38 modules and 8,583 lines. That gap between the best
edge and the best owner is the point: the facade's cost is spread across 41 imports, none
of which looks significant alone, so an edge-ranked queue hides it behind cuts worth a
fraction as much. `boot_closure.py` now reports both rankings for that reason.

### The `Widgets/System` question

Answered with a number, as intended. The widget tier costs 308 modules and 260,091 lines at
startup, and `Widgets/System` is **6 modules and 6,188 lines** of it - 2.4% of the widget
cost, 4.6% of the boot closure. The remaining 149 widgets are the captured account's own
configuration, and they drag 153 further non-widget modules with them.

So including `Widgets/System` in the MVP is close to free, and the plan's recommendation to
exclude it should rest on its actual argument - that `Messaging.py` is a misfiled transport
carried in a widget, and the register's verdict on it is `Split` - rather than on startup
cost. [Known Unknown 2](../plans/refactor-sequencing.md#known-unknowns) is closed as a
measurement; the sequencing judgement remains the plan's to make.

### Two corrections were needed to get here

Neither was a surprise about the client. Both were defects in the measuring apparatus, found
because a live client disagreed with it - which is the entire reason this phase exists.

#### Correction 1: the probe lost paths to a mutating `sys.modules`

The first capture invalidated itself. The probe recorded module *names* at each stage but the name-to-path map only once, at
write time. Several widgets call `Utils.ClearSubModules` during discovery, which deletes
entries from `sys.modules`. Any module present at an earlier stage and removed before the
write therefore lost its path, and the comparer - which resolves repository membership by
path - counted it as external. 135 of the 526 import-stage entries had no path.

That defect manufactured the entire `predicted but not loaded` column. All 19 rows were
`system_settings` modules that **were** loaded at the import stage under names such as
`Py4GWCoreLib.py4gwcorelib_src.system_settings.loot_filters`, and were cleared before the
write. The live totals were understated for the same reason.

Fixed by recording `name -> __file__` at each stage, at the moment it is taken. The capture
schema is now version 2, and schema 1 captures are rejected rather than reinterpreted,
because a silently misread capture is worse than no capture.

A second capture was taken with the corrected probe. It is the one every figure above
comes from.

#### Correction 2: the closure did not model implicit package initialisation

This is the one that changed the plan. Of the
16 modules loaded at the import stage that the closure did not predict, 12 are package
`__init__.py` files whose packages already have predicted children - Python loads a
package's `__init__` when importing any submodule, and `build_graph.py` records no edge for
that. The remaining 4 are second-order: `launch_bar/model.py` and
`system_settings/{controller,model,persistence}.py` are imported *by* those unmodelled
`__init__.py` files.

The fix went into `boot_closure.py`, not `build_graph.py`. Nobody writes an import
statement for a package `__init__`, so inventing graph edges would have inflated the
register's fan-in and fan-out numbers for a relationship that exists in Python's semantics
rather than in the source. `graph.json` is therefore unchanged and the component register
is untouched; only the boot-closure metric moved, which is this plan's own metric.

The boot closure went from 272 modules / 133,713 loc to **288 / 134,644**, and the recorded
zero-point moves with it. That is the correct direction: the old number was an
under-measurement of the same tree.

### Still open

- Phase 2 needs re-planning around the corrected queue. Its ordering puts the facade last;
  the measurement puts it first, and worth ten times the next item.
- ~~The MVP gate has not been run.~~ **Run and passed on 2026-09-06**, after the consumer
  tier was deleted in `c586f15d`: the launcher starts the client, the DLL injects, the host
  bootstraps and the launchpad renders, with the widget browser empty. That is the gate
  sentence satisfied exactly, and it is tagged `mvp-baseline`.
- The 166 modules / 72,230 loc that remain after the facade is emptied have not been
  analysed. That is the next floor, and nobody has looked at what holds it up.
