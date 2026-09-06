# Refactor Phase 0 Waypoint

Status: current
Scope: the recorded zero-point for the refactor scheduled in
[refactor-sequencing.md](../plans/refactor-sequencing.md)
Authority: measured at tag `refactor-origin` by the two committed generators and
by executing the offline suite. This document records results, not verdicts. The
[component register](../refactor-baseline/component-register.md) remains the
authority on what each component is.

Phase 0 preserves the map before the refactor destroys the tree it describes.
This is what was preserved, what was measured, and what is still open.

## The git waypoint

| Item | Value |
|---|---|
| Baseline commit | `ef92c918` - the baseline set and sequencing plan |
| Code tree | identical to `8dfdcaf2`; the baseline commit is documentation only |
| Tag | `refactor-origin`, annotated, on `ef92c918` |
| Work branch | `refactor/truncation`, branched from `refactor-origin` |
| Shippable branch | `main`, untouched at `8dfdcaf2` |

`baseline` is one commit ahead of `origin/baseline` and holds the same baseline
commit. Nothing has been pushed.

## The zero-point measurement

Both generators were rerun at `ef92c918` before it was committed. The regenerated
`graph.json` was byte-identical to the copy on disk, which confirms the
determinism contract the plan depends on: a rerun on an unchanged tree produces
no diff, so a diff means the tree moved.

| Measure | Modules | Lines |
|---|---|---|
| Whole repository, in scope | 1,701 | 659,975 |
| Static closure of the widget host | 477 | 183,044 |
| **Boot closure** | **272** | **133,710** |
| Consumer tier | 1,076 | 441,163 |

Severance edges: 4 module-level, 6 deferred. Graph totals: 6,929 edges, 12
cycles, 19 broken imports, 1 parse error.

Every later boot-closure number is read against the 272 / 133,710 figure.

`graph.json` is stored with git's `text: auto` handling under `core.autocrlf=true`.
The generator writes in text mode, so the round trip is consistent on this
machine and `git status` stays clean after a rerun. A Windows checkout with
`core.autocrlf=false` would see the whole file as modified after every run,
which would destroy the diff signal rather than report a real change. Worth
knowing before a second machine joins the work.

## The offline test baseline

All 18 files in `Examples and tests/tests/` were executed individually at
`refactor-origin` with Python 3.14.7.

**Result: 11 pass, 7 fail.**

This corrects the register, which recorded 13 pass and 5 fail. Two further
failures reproduce at this commit; both are named below. The register and the
sequencing plan have been updated to match, and this record holds the roster.

Passing (11): `test_filter_set_selection`, `test_inventory_transfer_planner`,
`test_loot_filter_matcher`, `test_name_obfuscation_smoke`,
`test_path_following_geometry`, `test_salvage_keep_list`,
`test_salvage_upgrade_slot`, `test_settings_migration`,
`test_skills_unlocker_routes`, `test_skills_unlocker_widget`,
`test_system_items_map_gate`.

Failing (7):

| Test | Symptom | Side | Cause |
|---|---|---|---|
| `test_energy_denial_ledger.py` | `FileNotFoundError` | harness | `MODULE_PATH` uses `.parent.parent`, resolving inside `Examples and tests/` instead of the repository root. One-token fix: `parents[2]`. Has not run since the folder move. Register row already carries this. |
| `test_skill_helper_registry.py` | `FileNotFoundError` | harness | Same defect, same row. |
| `test_sqlite3.py` | `ModuleNotFoundError: Py4GW` | dead | Requires the injected native module and imports the pre-move `Py4GWCoreLib.DBMgr` path. Register verdict is `Remove`. |
| `test_sqlite3 - Copy - Copy.py` | `ModuleNotFoundError: PySystem` | dead | Same row, same verdict. |
| `test_salvage_upgrade_dialog.py` | `NameError: prefix_option` | **production** | `Sources/frenkeyLib/ItemHandling/UIManagerExtensions.py:153-167` reads four names that are never bound; `:148-151` bind `salvage_window_mod_one_id` through `_four`. The test is doing its job and failing on a real defect. |
| `test_paragon_refrain_atomic_handlers.py` | `AttributeError: IsHeroicRefrainSelfReady` | harness | The test builds a synthetic skill group at `:155` (`type("SkillGroup", (), {})()`). Production gained the call at `Py4GWCoreLib/Builds/Paragon/P_W/Defensive Refrain.py:163` in `05b7576d`. The real method exists at `Py4GWCoreLib/Builds/Skills/paragon/Leadership.py:27`, so this is a stale fake, not a dead call. |
| `test_recolor_outcomes.py` | one case failed | harness | An exact-dict assertion predates the `modifiers` and `upgrades` fields on `Py4GWCoreLib/py4gwcorelib_src/system_settings/loot_filter_factory/model.py:198`. Production emits two keys the test does not expect. The other 20 cases in the file pass. |

Five of the seven are harness staleness, one pair is already slated for removal,
and exactly one - `test_salvage_upgrade_dialog.py` - is reporting a live
production defect.

## Open at the end of Phase 0

1. **Fix or accept the 7 failures.** The plan requires this decision before
   Phase 2, because "did I break this?" is unanswerable in Phase 4 against a
   suite that was already red. Not decided here.
2. **User data snapshot.** `json/`, `Settings/` and `Py4GW.ini` are not copied
   off-tree yet. This is the one hazard deletion genuinely creates and it is a
   standing constraint of the plan. Not done here.
3. **Live boot-closure confirmation.** Phase 1 work, not Phase 0. The 272-module
   figure remains a static claim until a live `sys.modules` capture confirms it.
