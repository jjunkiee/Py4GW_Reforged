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

**Result as measured: 11 pass, 7 fail. After the Phase 0 repairs below: 16 pass, 2 fail.**

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

Four of the seven are harness staleness, one pair is already slated for removal,
and exactly one - `test_salvage_upgrade_dialog.py` - is reporting a live
production defect.

### What was done about them

The plan requires this decision before Phase 2, so it was taken here rather than
deferred.

- **The four harness failures were repaired** (`8dda7186`). Two were the
  `parents[2]` fix. `test_recolor_outcomes` gained the two missing keys. The
  paragon fixture needed two repairs: a non-generator predicate for the
  bootstrap gate, and derived ids for the 27 skills the contract references but
  no test names, so the fixture stops going stale every time the contract gains
  a skill.
- **The production defect was fixed separately** (`5a528ab1`), by completing the
  half-finished rename in `UIManagerExtensions.py`. This is a behaviour change:
  a fallback that always raised now returns options. **It has offline proof
  only** and still needs the salvage dialog exercised in an injected client.
- **The `test_sqlite3` pair is deliberately left red.** Both need native modules
  that do not exist offline, and both carry a `Remove` verdict. Phase 3 deletes
  them. Fixing them would be work spent on condemned code.

Two red tests remain, both known and both condemned. That is a baseline against
which "did I break this?" has an answer.

The repairs moved the graph, as expected and by exactly the accountable amount:
boot closure 133,710 to 133,713 lines (the production fix, which is inside the
boot closure via the module-level severance edge at
`behaviourtrees_src/items.py:73`), consumer tier 441,163 to 441,205 (the same 3
lines plus 39 in the test files). Module counts are unchanged at 272 and 1,076.

## Open at the end of Phase 0

1. **Live proof of the salvage fix.** `GetSalvageOptions()`'s fallback is
   repaired against its offline test, and nothing more. How often the fallback
   is reached in-game was already unresolved in the register.
2. **Live boot-closure confirmation.** Phase 1 work, not Phase 0. The 272-module
   figure remains a static claim until a live `sys.modules` capture confirms it.

Closed during Phase 0: the fix-or-accept decision on the failing tests, recorded
above; and the user data snapshot, taken off-tree to
`_py4gw_phase0_backup6-09-06` outside the repository, with source and backup
file counts confirmed matching.
