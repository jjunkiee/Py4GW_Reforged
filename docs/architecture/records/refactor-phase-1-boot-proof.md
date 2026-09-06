# Refactor Phase 1 Boot Proof

Status: current procedure; result unresolved pending a live capture
Scope: the MVP gate sentence, and proving the static boot closure against a running client
Authority: [refactor-sequencing.md](../plans/refactor-sequencing.md) schedules this work;
`boot_capture_compare.py` produces the verdict; the live client is the only proof

Phase 1 has two jobs. Settle the one sentence the whole plan hangs on, and find out
whether the 272-module boot closure is true of a running client or only of the source.
Until the second is answered, the Phase 2 cut ranking is a static claim, and Phase 2 is
where the expensive work lives.

## The MVP gate

> The launcher starts `Gw.exe`, injects the DLL, the host bootstraps, and the launchpad
> renders - with zero widgets present.

Testable by eye in under a minute, and it proves the runtime is alive end to end: process
launch, injection, Python host, settings resolution, ImGui draw path. The boot path is
[`Py4GW_widget_manager.py`](../../../Py4GW_widget_manager.py), whose three module-level
imports are `Settings`, `WidgetManager` and `launch_bar/launchpad`.

**Whether `Widgets/System` is inside or outside the gate is deliberately not decided here.**
The plan recommends excluding it, and records that as a judgement rather than a finding
([Known Unknown 2](../plans/refactor-sequencing.md#known-unknowns)). The capture procedure
below measures what that tier actually costs at startup, so the decision can be made
against a number instead of an opinion. Deciding it first would waste the measurement.

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

Not yours to do. Once the capture is interpreted the probe is reverted, and the record
below is completed with the result.

## Result

**Unresolved. One capture taken on 2026-09-06; it invalidated itself and a second is
required.** The verdict on the cut ranking is therefore still open.

The comparison tool had been exercised against synthetic captures covering both outcomes,
so the instrument's *logic* was sound. Its input was not.

### What the first capture found, and what it broke

The probe recorded module *names* at each stage but the name-to-path map only once, at
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

### What survives the defect

Two findings are unaffected, because the modules involved kept their paths throughout.

**1. Discovery is where widget code runs.** 152 widget modules at `post_discover`, 0 at
`import`, and `post_bootstrap` identical to `post_discover`. The stage table above was
wrong on this point and has been corrected. `_apply_ini_configuration()` re-applies state
to modules that are already loaded; the cost is paid inside `discover()`.

**2. The graph does not model implicit package initialisation.** This one matters. Of the
16 modules loaded at the import stage that the closure did not predict, 12 are package
`__init__.py` files whose packages already have predicted children - Python loads a
package's `__init__` when importing any submodule, and `build_graph.py` records no edge for
that. The remaining 4 are second-order: `launch_bar/model.py` and
`system_settings/{controller,model,persistence}.py` are imported *by* those unmodelled
`__init__.py` files.

So the boot closure has a genuine blind spot, from a single systematic cause, and it
under-predicts. Whether to teach the generators about package initialisation is a real
decision - it changes `graph.json` and the recorded zero-point - and it is deliberately not
taken here. It should be taken once a trustworthy capture exists, so both corrections can
be applied and the ranking re-derived once rather than twice.

### Still open

- A schema 2 capture. Rerun the runbook above from Step 2.
- Whether the ranked cuts survive both corrections.
- Whether `Widgets/System` belongs inside the MVP gate, which the widget bootstrap cost
  answers once the capture is trustworthy.
