"""Offline fixture for the outcome relocation (filter-structure contract, T2).

A filter is criteria only; what it marks for THIS account lives in Recolor &
Beacons' per-account store (``Widgets/System/RecolorOutcomes.json``). This
fixture drives the REAL factory and recolor stores against in-memory fakes
and proves:

- ``MarkOutcome`` round-trips (including a malformed colour falling back);
- the one-time legacy import copies the pre-contract ``mark_*`` fields off the
  global filters into this account's store, keyed by filter id -- silent,
  idempotent, and NEVER overwriting an id the account already has (so a
  cleared outcome cannot be resurrected);
- a factory save no longer knows outcomes, yet keeps the legacy fields on disk
  (preserve-on-save shim) until the live gate removes the bridge.

Run:  python "Examples and tests/tests/test_recolor_outcomes.py"
Exit code 1 on any failure.
"""

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
SS_DIR = ROOT / "Py4GWCoreLib" / "py4gwcorelib_src" / "system_settings"

_FACTORY_DOC = "Widgets/System/LootFilterFactory.json"
_OUTCOMES_DOC = "Widgets/System/RecolorOutcomes.json"

# -- a fake Py4GWCoreLib package chain, so the real stores load offline ----------


def _fake_package(name: str) -> types.ModuleType:
    package = types.ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package
    return package


for _name in (
    "Py4GWCoreLib",
    "Py4GWCoreLib.py4gwcorelib_src",
    "Py4GWCoreLib.py4gwcorelib_src.system_settings",
    "Py4GWCoreLib.py4gwcorelib_src.system_settings.loot_filter_factory",
    "Py4GWCoreLib.py4gwcorelib_src.system_settings.recolor_beacons",
):
    _fake_package(_name)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None, "could not build a module spec for %s" % path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


factory_model = _load("Py4GWCoreLib.py4gwcorelib_src.system_settings.loot_filter_factory.model",
                      SS_DIR / "loot_filter_factory" / "model.py")
factory_store = _load("Py4GWCoreLib.py4gwcorelib_src.system_settings.loot_filter_factory.store",
                      SS_DIR / "loot_filter_factory" / "store.py")
recolor_model = _load("Py4GWCoreLib.py4gwcorelib_src.system_settings.recolor_beacons.model",
                      SS_DIR / "recolor_beacons" / "model.py")
recolor_store = _load("Py4GWCoreLib.py4gwcorelib_src.system_settings.recolor_beacons.store",
                      SS_DIR / "recolor_beacons" / "store.py")

Filter = factory_model.Filter
MarkOutcome = recolor_model.MarkOutcome

# -- in-memory JsonFactory / ConsoleLog -----------------------------------------


class FakeJsonFactory:
    _DOCS: dict = {}

    def __init__(self, path: str, scope: str):
        self._data = FakeJsonFactory._DOCS.setdefault((str(path), str(scope)), {})

    def get_json(self, key: str, default=None):
        return self._data.get(key, default)

    def set_json(self, key: str, value) -> None:
        self._data[key] = value

    @staticmethod
    def doc(path: str, scope: str) -> dict:
        return FakeJsonFactory._DOCS.setdefault((str(path), str(scope)), {})

    @staticmethod
    def reset() -> None:
        FakeJsonFactory._DOCS.clear()


_CONSOLE: list = []


def _console_log(tag: str, message: str) -> None:
    _CONSOLE.append((tag, message))


_console_module = types.ModuleType("Py4GWCoreLib.Py4GWcorelib")
setattr(_console_module, "ConsoleLog", _console_log)
sys.modules["Py4GWCoreLib.Py4GWcorelib"] = _console_module
_json_module = types.ModuleType("Py4GWCoreLib.py4gwcorelib_src.JsonFactory")
setattr(_json_module, "JsonFactory", FakeJsonFactory)
sys.modules["Py4GWCoreLib.py4gwcorelib_src.JsonFactory"] = _json_module

# -- helpers ---------------------------------------------------------------------

failures = 0


def check(label: str, expected, observed) -> None:
    global failures
    ok = expected == observed
    if not ok:
        failures += 1
    print("[%s] %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        print("      expected: %r" % (expected,))
        print("      observed: %r" % (observed,))


def reset_all() -> None:
    FakeJsonFactory.reset()
    _CONSOLE.clear()


LEGACY_FILTER = {
    "id": "filter_1", "name": "Caster", "enabled": True, "mode": "all",
    "min_value": 100,
    "mark_recolor": True, "mark_color": [0.5, 0.6, 0.7, 1.0],
    "mark_blank": False, "mark_beacon": True, "mark_preset": "Tower",
}
SILENT_FILTER = {"id": "filter_2", "name": "No outcome", "mode": "all"}

# -- 1. MarkOutcome round-trip ---------------------------------------------------

check("outcome round-trips through the dict shape",
      {"recolor": True, "color": [0.5, 0.6, 0.7, 1.0], "blank": False,
       "beacon": True, "preset": "Tower"},
      MarkOutcome.from_dict(MarkOutcome(recolor=True, color=(0.5, 0.6, 0.7, 1.0),
                                        beacon=True, preset="Tower").to_dict()).to_dict())
check("a malformed colour falls back to white", (1.0, 1.0, 1.0, 1.0),
      MarkOutcome.from_dict({"color": "not a colour"}).color)
check("a short colour falls back to white", (1.0, 1.0, 1.0, 1.0),
      MarkOutcome.from_dict({"color": [0.5]}).color)
check("an outcome with everything off marks nothing", False, MarkOutcome().marks())
check("a recolor outcome marks", True, MarkOutcome(recolor=True).marks())

# -- 2. the silent one-time legacy import ----------------------------------------

reset_all()
FakeJsonFactory.doc(_FACTORY_DOC, "global")["filters"] = [dict(LEGACY_FILTER), dict(SILENT_FILTER)]
outcomes = recolor_store.load_outcomes()
check("imports the legacy outcome keyed by filter id", ["filter_1"], list(outcomes))
check("the imported colour survives the prefix strip", (0.5, 0.6, 0.7, 1.0),
      outcomes["filter_1"].color)
check("the imported preset survives", "Tower", outcomes["filter_1"].preset)
check("an entry with no marks is not imported", False, "filter_2" in outcomes)
check("the account document was written once",
      {"filter_1": {"recolor": True, "color": [0.5, 0.6, 0.7, 1.0], "blank": False,
                    "beacon": True, "preset": "Tower"}},
      FakeJsonFactory.doc(_OUTCOMES_DOC, "account").get("outcomes"))
check("the import logs exactly once",
      [("Recolor & Beacons", "imported 1 legacy outcome(s) from the factory into this account's store")],
      _CONSOLE)
again = recolor_store.load_outcomes()
check("the second load is idempotent and silent",
      [("Recolor & Beacons", "imported 1 legacy outcome(s) from the factory into this account's store")],
      _CONSOLE)
check("the second load keeps the outcome", "Tower", again["filter_1"].preset)

# -- 3. an account's own entry is never overwritten ------------------------------

reset_all()
FakeJsonFactory.doc(_FACTORY_DOC, "global")["filters"] = [dict(LEGACY_FILTER)]
# This account already decided: cleared everything for filter_1.
FakeJsonFactory.doc(_OUTCOMES_DOC, "account")["outcomes"] = {
    "filter_1": {"recolor": False, "color": [1.0, 1.0, 1.0, 1.0], "blank": False,
                 "beacon": False, "preset": ""},
}
outcomes = recolor_store.load_outcomes()
check("a stored (cleared) outcome is not resurrected by the import", False,
      outcomes["filter_1"].marks())
check("no import happened at all", [], _CONSOLE)

# -- 4. the factory no longer carries outcomes, but keeps them on disk -----------

reset_all()
FakeJsonFactory.doc(_FACTORY_DOC, "global")["filters"] = [dict(LEGACY_FILTER), dict(SILENT_FILTER)]
parsed = factory_store.load_filters()
check("parsed filters carry no outcome attribute", False, hasattr(parsed[0], "mark_recolor"))
check("the criteria survive the parse", 100, parsed[0].min_value)
check("the raw legacy marks are still readable for the import",
      {"filter_1": {"mark_recolor": True, "mark_color": [0.5, 0.6, 0.7, 1.0],
                    "mark_blank": False, "mark_beacon": True, "mark_preset": "Tower"}},
      factory_store.legacy_mark_entries())
factory_store.save_filters(parsed)
stored = FakeJsonFactory.doc(_FACTORY_DOC, "global")["filters"]
check("a factory save preserves the legacy mark fields", LEGACY_FILTER["mark_recolor"],
      stored[0].get("mark_recolor"))
check("a factory save keeps the legacy colour", LEGACY_FILTER["mark_color"],
      stored[0].get("mark_color"))
check("an entry with no marks carries no mark keys after save",
      {"id": "filter_2", "name": "No outcome", "enabled": True, "mode": "all",
       "item_types": [], "model_ids": [], "dye_colors": [], "salvages_into": [],
       "name_contains": [], "rarities": [], "max_requirement": None,
       "requirement_attribute": None, "min_value": None, "min_damage": None,
       "damage_types": [], "modifiers": [], "upgrades": []},
      stored[1])

print("=" * 68)
print("%d case(s) failed" % failures)
sys.exit(1 if failures else 0)
