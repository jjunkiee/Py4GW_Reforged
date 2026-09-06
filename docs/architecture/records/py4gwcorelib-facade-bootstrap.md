# The Py4GWCoreLib Facade Bootstrap

Status: current
Scope: what [`Py4GWCoreLib/__init__.py`](../../../Py4GWCoreLib/__init__.py) does at import
time, who depends on it, and what that costs the refactor
Authority: current source, plus closure measurements from
[`boot_closure.py`](../refactor-baseline/tools/boot_closure.py) and the live capture recorded
in [refactor-phase-1-boot-proof.md](refactor-phase-1-boot-proof.md)

[Phase 1](refactor-phase-1-boot-proof.md) established that this file is the boot floor and
that it is unavoidable: importing anything under `Py4GWCoreLib` executes it first. It also
left a warning inherited from the baseline - that the file has import-time side effects,
that those may be *why* the pattern exists, and that no core split may proceed on the
assumption they are incidental.

**They are not incidental. They are the file's actual job.** The 41 re-export imports are
the part that could go; the side effects are load-bearing runtime bootstrap that every
import into the package depends on. Anyone reading this file as "a 323-module facade cycle
to be untangled" has the emphasis backwards.

## What runs, in order

The order is itself a constraint: each step depends on the ones before it.

| # | Lines | Side effect |
|---|---|---|
| 1 | 15-29 | Locates system Python via `subprocess.check_output("where python", shell=True)`, appends its `site-packages` to `sys.path`, and prepends it to `os.environ["PATH"]`. |
| 2 | 37-43 | Imports the native `Py4GW`, `PySystem`, `PyGameThread`, `PyPing`, `PyScanner`, `PyImGui`, `PyCallback` modules. |
| 3 | 47-50 | Injects `Console` and `ConsoleLog` into `builtins`. |
| 4 | 60-69 | **Monkey-patches `PyInventory.Bag.GetItems`** to convert returned dicts into `SimpleNamespace`. |
| 5 | 86-92 | Injects `PySystem`, `PyPing`, `PyGameThread`, `PyDXOverlay`, `PyAgentEvents` into `builtins`. |
| 6 | 94-186 | The 41 module-level re-export imports, and their self-assignments. |
| 7 | 191-210 | Replaces `sys.stdout` and `sys.stderr` with writers that log to the Py4GW console. |

Only step 6 is a facade. Steps 1-5 and 7 are process-wide mutations of the interpreter, the
native bindings, and the environment.

## Who depends on each, measured

Counts are call sites with **no corresponding import in the same file** - that is, sites that
resolve only because of the injection.

| Side effect | Whole repository | Retained tier only |
|---|---|---|
| `builtins.PySystem` | 3,375 sites / 253 files | 148 sites / 21 files |
| `builtins.ConsoleLog` | 1,168 sites / 57 files | 6 sites / 3 files |
| `builtins.Console` | 587 sites / 33 files | 1 site / 1 file |
| `builtins.PyGameThread` | 33 sites / 10 files | 0 |
| `builtins.PyPing` | 6 sites / 3 files | 0 |
| `builtins.PyDXOverlay` | 2 sites / 1 file | 0 |
| `builtins.PyAgentEvents` | **0 sites** | 0 |

"Retained tier" is what survives Phase 3: `Py4GWCoreLib`, `py4gw_bridge`, and root scripts.

Three things fall out of that table:

- **`builtins.PySystem` is not removable.** 148 sites across 21 files in code the refactor
  keeps, and 3,375 across the repository. Any change that stops this injection is a
  repository-wide edit, not a core change.
- **Four of the five module injections are dead weight for the retained tier.**
  `PyGameThread`, `PyPing` and `PyDXOverlay` are used bare only by widgets and examples, and
  widgets already receive them by a second mechanism: `WidgetManager.load_module()` assigns
  all five directly into each widget's namespace before executing it
  ([`WidgetManager.py:403-407`](../../../Py4GWCoreLib/py4gwcorelib_src/WidgetManager.py#L403-L407)).
- **`builtins.PyAgentEvents` has no consumers at all**, by either mechanism. It is injected
  and never read.

### The monkey-patch is load-bearing for the boot path

`PyInventory.Bag.GetItems()` returns dicts. The patch wraps each in a `SimpleNamespace` so
attribute access works. [`InventoryCache.py:87`](../../../Py4GWCoreLib/GlobalCache/InventoryCache.py#L87)
does `item.slot` on the result, and `InventoryCache` is inside the boot closure. There are 76
`.GetItems()` call sites in the repository.

Without the patch, the core inventory cache raises `AttributeError` on its first call. This
is a global mutation of a native binding type performed as a side effect of importing a
package - the exact pattern the engineering guidance forbids adding - and it cannot simply
be deleted, because 76 call sites were written against the patched behaviour.

### The stdout redirect owns all diagnostic output

80 `print()` calls across 19 retained files reach the Py4GW console only because of step 7.
Nothing else in the repository assigns `sys.stdout` or `sys.stderr`. Remove the redirect and
those become invisible rather than broken, which is the worse failure mode.

## What this means for the refactor

### Repointing the back-edges does not lower the boot floor

This is the correction that matters most, and it contradicts a reasonable reading of the
register's `Improve in place` verdict.

Rewriting `from Py4GWCoreLib import X` into `from Py4GWCoreLib.foo import X` does **not**
skip `Py4GWCoreLib/__init__.py`. Python executes a package's `__init__` before any submodule
of it, so the facade and everything it imports still load. Phase 1 proved this against a
live client: the host never imports `Py4GWCoreLib` directly, only three submodules of it,
and the facade runs anyway.

Repointing the 524 `Builds` back-edges is still worth doing - it breaks the import cycle and
makes ownership legible - but it is a **cycle fix, not a startup fix**, and it should not be
scheduled as though it will move the boot closure. It will not move it at all.

### The only lever is what `__init__.py` itself imports

Reducing the file to its bootstrap - steps 1-5 and 7, dropping the 41 re-exports - gives:

| | Modules | Lines |
|---|---|---|
| Boot closure today | 288 | 134,644 |
| Facade reduced to its bootstrap | 166 | 72,230 |
| Released | **122** | **62,414** |

The bootstrap needs almost nothing from the repository. Its imports are stdlib (`sys`, `os`,
`subprocess`, `builtins`, `types`), the native `Py*` modules, and `ConsoleLog` - and
`ConsoleLog`'s own module adds nothing that is not reached anyway.

The obvious way to keep `from Py4GWCoreLib import X` working while dropping the eager
imports is a module-level `__getattr__` (PEP 562; the client runs Python 3.13). That is a
design proposal, not a finding, and it has a caveat worth stating before anyone starts: a
consumer doing `from Py4GWCoreLib import X` at module scope still triggers the import of `X`
immediately. Lazy re-export helps the boot path only to the extent that the boot path does
not ask for those names. How much of the 62,414 that recovers in practice is **unmeasured**,
and should be measured on a branch before the approach is committed to.

### The next floor

The 166 modules that remain are 165 `Py4GWCoreLib` modules plus the host. The heaviest are
enum and data tables and the ImGui layer:

```
   3145  Py4GWCoreLib/enums_src/Texture_enums.py
   3142  Py4GWCoreLib/enums_src/Model_enums.py
   2797  Py4GWCoreLib/ImGui_src/ImGuisrc.py
   2328  Py4GWCoreLib/Map.py
   2087  Py4GWCoreLib/py4gwcorelib_src/BehaviorTree.py
```

The top ten account for 22,480 lines, 31% of that floor. This is a healthier shape than the
current one - leaf data and a UI layer rather than a singleton constructing nine caches -
and the register already records `enums_src` as the cleanest component in the core. Nobody
has yet examined whether the boot path genuinely needs the enum tables at load time.

## Incidental findings

Recorded because they were observed while establishing the above, not because this record
proposes acting on them.

- **A shell process is spawned during client startup.** Step 1 runs
  `subprocess.check_output("where python", shell=True)` at import time, inside the injected
  game process, synchronously. Measured at roughly 50 ms outside the client; the in-client
  cost was not measured. `sys.prefix` is already consulted as the fallback and would in most
  cases give the same answer without a shell.
- **`builtins.PyAgentEvents` is injected and never read.**
- **The five `builtins` module injections are duplicated** by
  `WidgetManager.load_module()` for widgets specifically, so for widgets they are redundant
  today.

## What this record does not decide

- Whether to adopt a lazy `__getattr__`, split the bootstrap into its own module, or leave
  the facade intact. All three are defensible and the measurement above does not choose
  between them.
- Whether the monkey-patch should be pushed down into the native binding so
  `Bag.GetItems()` returns objects directly. That is a `Py4GW_Reforged_Native` question and
  crosses a repository boundary, so nothing here proposes it.
- Anything about behaviour under a client where system Python is absent, where step 1 falls
  back to `sys.prefix`. Not reproduced.
