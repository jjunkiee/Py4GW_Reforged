"""Offline fixture for the salvage choice dialog (blue/purple/gold items).

Guards the two defects that made "Run Identify + Salvage Now" kill the client
when it met an item that opens a salvage choice dialog:

  * `GetSalvageOptions()` hands back frame **ids** from the live-dialog sweep
    and Frame **handles** from the registry fallback, and `0` for "nothing
    found". The click helper has to survive all three shapes; it used to raise
    `AttributeError: 'int' object has no attribute 'is_usable'` on the first.

  * Reading an option row's text calls a native decoder that hard-crashes the
    client on frames carrying no text label. Nothing on this path may call it,
    so option selection now rests on the dialog's visible order instead.

Run:  python "Examples and tests/tests/test_salvage_upgrade_dialog.py"
Exit code 1 on any failure.
"""

import ast
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def section(title: str) -> None:
    print("\n== %s" % title)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None, "no module spec for %s" % path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. Fakes for the frame layer, then the real UIManagerExtensions helpers
# ---------------------------------------------------------------------------


class FakeFrame:
    """Stands in for Py4GWCoreLib.FrameTree.Frame.

    Mirrors the real contract that matters here: reads answer even on a dead
    frame, and interactions no-op unless the frame is usable.
    """

    # Registry keys the fixture wants to resolve, by key string. Anything not
    # listed resolves to nothing, which is what an off-screen dialog looks like.
    REGISTRY: dict = {}

    def __init__(self, frame_id=0, usable: bool = True, text: str = "", template_type: int = 1):
        if not isinstance(frame_id, int):
            # A FrameId node: the real Frame resolves it through the registry
            # and yields a handle that simply does not exist when it is not up.
            key = getattr(frame_id, "KEY", str(frame_id))
            resolved = FakeFrame.REGISTRY.get(key)
            frame_id = resolved if resolved is not None else 0
            usable = frame_id != 0
            text = key
        self._fid = int(frame_id)
        self.usable = usable
        self._text = text
        self.template_type = template_type
        self.type = 0
        self.clicks: list[str] = []
        # dies_after_click models the real hazard: the first native call tears
        # the dialog down and the frame is gone before the second one lands.
        self.dies_after_click = False

    @classmethod
    def from_id(cls, frame_id: int) -> "FakeFrame":
        return cls(frame_id, usable=True, text="from_id")

    @property
    def frame_id(self) -> int:
        return self._fid

    @property
    def exists(self) -> bool:
        return self._fid != 0

    @property
    def is_created(self) -> bool:
        return self.usable

    @property
    def is_visible(self) -> bool:
        return self.usable

    @property
    def is_usable(self) -> bool:
        return self.usable

    def text(self) -> str:
        return self._text

    def click(self) -> None:
        if not self.usable:
            raise AssertionError("click() fired at a frame that is not usable")
        self.clicks.append("button_click")
        if self.dies_after_click:
            self.usable = False

    def mouse_action(self, state: int, wparam: int = 0, lparam: int = 0) -> None:
        if not self.usable:
            raise AssertionError("mouse_action() fired at a frame that is not usable")
        self.clicks.append("mouse_action:%d" % state)


def _install_fake_modules() -> None:
    """A Py4GW-shaped import surface, so the real module loads offline."""

    def module(name: str, is_package: bool = False) -> types.ModuleType:
        mod = types.ModuleType(name)
        if is_package:
            mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    module("Py4GW")
    module("PyUIManager")

    corelib = module("Py4GWCoreLib", is_package=True)

    inventory_mod = module("Py4GWCoreLib.Inventory")

    class _Inventory:
        """Only the salvage-dialog surface UIManagerExtensions actually touches."""

        material_confirm_visible = False
        option_entries: list = []
        confirm_frame = FakeFrame(0, usable=False)

        @staticmethod
        def IsSalvageChoiceMaterialConfirmVisible() -> bool:
            return _Inventory.material_confirm_visible

        @staticmethod
        def _build_visible_frame_entry_map():
            return {}

        @staticmethod
        def _build_frame_children_map():
            return {}

        @staticmethod
        def _get_salvage_choice_dialog_options(_visible=None):
            return 0, [], list(_Inventory.option_entries)

        @staticmethod
        def _collect_salvage_choice_option_text(_roots, children_map=None, max_depth=2, text_source=None):
            return ""

        @staticmethod
        def _choose_salvage_choice_dialog_option(option_entries, strategy):
            return real_choose(option_entries, strategy)

        @staticmethod
        def _salvage_confirm():
            return _Inventory.confirm_frame

        @staticmethod
        def inventory_instance():
            return _FakeInventoryInstance()

    class _FakeInventoryInstance:
        accepted = 0

        def AcceptSalvageWindow(self) -> None:
            _FakeInventoryInstance.accepted += 1

    inventory_mod.Inventory = _Inventory  # type: ignore[attr-defined]
    inventory_mod.FakeInventoryInstance = _FakeInventoryInstance  # type: ignore[attr-defined]

    uimanager_mod = module("Py4GWCoreLib.UIManager")
    uimanager_mod.UIManager = object  # type: ignore[attr-defined]

    frametree = module("Py4GWCoreLib.FrameTree")
    frametree.Frame = FakeFrame  # type: ignore[attr-defined]

    class _Node:
        def __init__(self, name: str = ""):
            self.KEY = name

        def __getattr__(self, item: str) -> "_Node":
            return _Node("%s.%s" % (self.KEY, item))

    frametree.FrameId = _Node("FrameId")  # type: ignore[attr-defined]

    module("Sources", is_package=True)
    module("Sources.frenkeyLib", is_package=True)
    module("Sources.frenkeyLib.ItemHandling", is_package=True)
    module("Sources.frenkeyLib.ItemHandling.Rules", is_package=True)

    types_mod = module("Sources.frenkeyLib.ItemHandling.Rules.types")
    import enum as _enum

    class SalvageMode(_enum.IntEnum):
        NONE = 1
        LesserCraftingMaterials = 2
        RareCraftingMaterials = 3
        Prefix = 4
        Suffix = 5
        Inscription = 6

    types_mod.SalvageMode = SalvageMode  # type: ignore[attr-defined]
    corelib.__dict__["SalvageMode"] = SalvageMode
    return SalvageMode


# The option-choosing logic is real code lifted straight out of Inventory.py by
# import, not a reimplementation - a fixture that re-states the logic it is
# meant to guard proves nothing.
def _load_real_chooser():
    source = (ROOT / "Py4GWCoreLib" / "Inventory.py").read_text(encoding="utf-8")
    start = source.index("    def _choose_salvage_choice_dialog_option(")
    end = source.index("    # DEBUG BLOCK START: salvage choice dialog troubleshooting")
    body = source[start:end]
    # dedent one level and drop the decorator-less staticmethod framing
    body = "\n".join(line[4:] if line.startswith("    ") else line for line in body.split("\n"))
    namespace: dict = {}
    exec(compile(body, "<Inventory._choose_salvage_choice_dialog_option>", "exec"), namespace)
    return namespace["_choose_salvage_choice_dialog_option"]


real_choose = _load_real_chooser()
SalvageMode = _install_fake_modules()

UIManagerExtensions = _load(
    "uimanager_extensions_fixture",
    ROOT / "Sources" / "frenkeyLib" / "ItemHandling" / "UIManagerExtensions.py",
).UIManagerExtensions

from Py4GWCoreLib.Inventory import Inventory as FakeInventory  # noqa: E402


# ---------------------------------------------------------------------------
# 2. Click targets: every shape the salvage dialog can hand back
# ---------------------------------------------------------------------------


def test_click_targets() -> None:
    section("click targets the salvage dialog actually produces")

    # GetSalvageOptions' live-sweep branch stores raw frame ids; its registry
    # fallback stores Frame handles. Both reach _click_frame.
    frame = FakeFrame(101, text="Crafting Materials")
    check("a Frame handle is clicked", UIManagerExtensions._click_frame(frame) is True)
    check("both natives fired", frame.clicks == ["button_click", "mouse_action:8"], repr(frame.clicks))

    check("a raw frame id is clicked, not fatal", UIManagerExtensions._click_frame(4242) is True)

    # 0 is the "nothing found" sentinel from any dialog sweep that came up
    # empty, and from _get_confirm_salvage_window_frame() finding nothing.
    check("the 0 sentinel is refused quietly", UIManagerExtensions._click_frame(0) is False)
    check("None is refused quietly", UIManagerExtensions._click_frame(None) is False)

    dead = FakeFrame(202, usable=False)
    check("a frame that is no longer usable is refused", UIManagerExtensions._click_frame(dead) is False)
    check("nothing was fired at the dead frame", dead.clicks == [], repr(dead.clicks))

    # The real hazard: button_click dismisses the dialog, and the follow-up
    # mouse_action would then be aimed at a frame that no longer exists.
    vanishing = FakeFrame(303, text="Inscription")
    vanishing.dies_after_click = True
    check("a dialog that closes on the first click is handled", UIManagerExtensions._click_frame(vanishing) is True)
    check(
        "no input is fired at the frame the first click destroyed",
        vanishing.clicks == ["button_click"],
        repr(vanishing.clicks),
    )

    check("_frame_exists accepts an id", UIManagerExtensions._frame_exists(999) is True)
    check("_frame_exists accepts the 0 sentinel", UIManagerExtensions._frame_exists(0) is False)


def test_confirm_lookup() -> None:
    section("confirmation dialog lookup")

    FakeInventory.material_confirm_visible = False
    check(
        "no confirmation up -> no frame",
        UIManagerExtensions._get_confirm_salvage_window_frame() is None,
    )

    # ConfirmModMaterialSalvage runs on purple/gold items. With no confirm frame
    # on screen it must decline, having still issued AcceptSalvageWindow.
    before = sys.modules["Py4GWCoreLib.Inventory"].FakeInventoryInstance.accepted
    result = None
    raised = None
    try:
        result = UIManagerExtensions.ConfirmModMaterialSalvage()
    except Exception as exc:
        raised = exc
    after = sys.modules["Py4GWCoreLib.Inventory"].FakeInventoryInstance.accepted
    check("confirming with no dialog on screen does not raise", raised is None, repr(raised))
    check("confirming with no dialog on screen reports failure", result is False, repr(result))
    check("AcceptSalvageWindow was still attempted", after == before + 1)


# ---------------------------------------------------------------------------
# 3. Option choice on a realistic gold-item dialog
# ---------------------------------------------------------------------------


def _gold_item_dialog() -> list[dict]:
    """What a gold sword with a prefix, a suffix and an inscription offers."""
    return [
        {"frame_id": 11, "offset": 1, "text": "Sundering Sword Hilt", "area": 900.0, "top": 10.0},
        {"frame_id": 12, "offset": 2, "text": "Sword Pommel of Fortitude", "area": 900.0, "top": 30.0},
        {"frame_id": 13, "offset": 3, "text": "Inscription: Strength and Honor", "area": 900.0, "top": 50.0},
        {"frame_id": 14, "offset": 4, "text": "Crafting Materials", "area": 900.0, "top": 70.0},
    ]


def test_option_choice() -> None:
    section("which option the dialog picks for a gold item")

    entries = _gold_item_dialog()

    materials, why_materials = real_choose(entries, strategy=0)
    check("strategy 0 picks crafting materials", materials is not None and materials["frame_id"] == 14, why_materials)

    upgrades, why_upgrades = real_choose(entries, strategy=1)
    check(
        "strategy 1 picks an upgrade component, never the materials row",
        upgrades is not None and upgrades["frame_id"] != 14,
        "%s -> %r" % (why_upgrades, upgrades),
    )

    # A blue item with one mod: two rows, and neither strategy may come back
    # empty-handed while an option is on screen.
    blue = [
        {"frame_id": 21, "offset": 1, "text": "Fiery Sword Hilt", "area": 900.0, "top": 10.0},
        {"frame_id": 22, "offset": 2, "text": "Crafting Materials", "area": 900.0, "top": 30.0},
    ]
    blue_materials, _ = real_choose(blue, strategy=0)
    blue_upgrade, _ = real_choose(blue, strategy=1)
    check("blue item, strategy 0 -> materials", blue_materials is not None and blue_materials["frame_id"] == 22)
    check("blue item, strategy 1 -> the mod", blue_upgrade is not None and blue_upgrade["frame_id"] == 21)

    # Empty option text is now the PRODUCTION case, not an edge case: reading
    # these labels calls a native decoder that kills the client (crash journal,
    # 2026-08-31, frame 440), so the salvage path no longer reads them at all.
    # Selection therefore rests entirely on the dialog's visible order, which
    # Guild Wars keeps stable - upgrade rows first, crafting materials last.
    untexted = [
        {"frame_id": 31, "offset": 1, "text": "", "area": 900.0, "top": 10.0},
        {"frame_id": 32, "offset": 2, "text": "", "area": 900.0, "top": 30.0},
        {"frame_id": 33, "offset": 3, "text": "", "area": 900.0, "top": 50.0},
    ]
    fallback_materials, why_materials = real_choose(untexted, strategy=0)
    fallback_upgrade, why_upgrade = real_choose(untexted, strategy=1)
    check(
        "with no text, strategy 0 takes the last row (materials sit last)",
        fallback_materials is not None and fallback_materials["frame_id"] == 33,
        "%s -> %r" % (why_materials, fallback_materials),
    )
    check(
        "with no text, strategy 1 takes the first row (upgrades sit first)",
        fallback_upgrade is not None and fallback_upgrade["frame_id"] == 31,
        "%s -> %r" % (why_upgrade, fallback_upgrade),
    )
    check(
        "neither strategy comes back empty while the dialog is open",
        fallback_materials is not None and fallback_upgrade is not None,
    )

    empty_choice, why_empty = real_choose([], strategy=1)
    check("an empty dialog yields no choice", empty_choice is None and why_empty == "no options")


def test_select_and_salvage() -> None:
    section("SelectSalvageOptionAndSalvage end to end")

    FakeInventory.option_entries = [dict(entry) for entry in _gold_item_dialog()]
    FakeInventory.confirm_frame = FakeFrame(99, text="Salvage")

    options = UIManagerExtensions.GetSalvageOptions()
    check("every mode the dialog can serve is mapped", set(options) == set(SalvageMode) - {SalvageMode.NONE}, repr(sorted(m.name for m in options)))
    check(
        "each mapped target is clickable after coercion",
        all(UIManagerExtensions._coerce_frame(target) is not None for target in options.values()),
        repr(options),
    )

    raised = None
    result = None
    try:
        result = UIManagerExtensions.SelectSalvageOptionAndSalvage(SalvageMode.Prefix)
    except Exception as exc:
        raised = exc
    check("selecting an upgrade component does not raise", raised is None, repr(raised))
    check("selecting an upgrade component reports success", result is True, repr(result))

    # A mode the dialog does not offer must cancel cleanly rather than blunder
    # into a click on whatever happens to be under the cursor.
    FakeInventory.option_entries = []
    fell_back = UIManagerExtensions.GetSalvageOptions()
    check("no dialog on screen -> no options", fell_back == {}, repr(fell_back))


# ---------------------------------------------------------------------------
# 4. Which salvage modes each rarity is allowed to try
# ---------------------------------------------------------------------------


def test_mode_selection_matrix() -> None:
    """The rule the auto handler applies, restated as the table it should be.

    Not a call into AutoInventoryHandler (that needs the whole Py4GW import
    chain), but a pin on the intent: an upgrade mode is only ever offered when a
    kit that can pull upgrades is in the bags. Offering Prefix/Suffix/Inscription
    on a lesser kit is what opens a choice dialog nothing can then answer.
    """
    section("salvage mode selection per rarity and kit")

    def modes_for(rarity: str, caps: dict, upgrade_slots: set, strategy: int) -> list[str]:
        material: list[str] = []
        if rarity == "Green":
            return []
        if rarity in ("White", "Blue", "Purple", "Gold"):
            if caps["lesser"]:
                material.append("LesserCraftingMaterials")
            elif caps["expert"]:
                material.append("RareCraftingMaterials")
        upgrade = [name for name in ("Inscription", "Suffix", "Prefix") if caps["upgrade"] and name in upgrade_slots]
        return material + upgrade if strategy == 0 else upgrade + material

    lesser_only = {"lesser": True, "expert": False, "upgrade": False}
    expert = {"lesser": False, "expert": True, "upgrade": True}
    both = {"lesser": True, "expert": True, "upgrade": True}

    check(
        "lesser kit on a gold item never offers an upgrade mode",
        modes_for("Gold", lesser_only, {"Prefix", "Suffix", "Inscription"}, 1) == ["LesserCraftingMaterials"],
    )
    check(
        "expert kit on a gold item offers upgrades first under strategy 1",
        modes_for("Gold", expert, {"Prefix", "Inscription"}, 1)
        == ["Inscription", "Prefix", "RareCraftingMaterials"],
    )
    check(
        "strategy 0 keeps materials ahead of upgrades",
        modes_for("Purple", both, {"Prefix"}, 0) == ["LesserCraftingMaterials", "Prefix"],
    )
    check(
        "a blue item with no mods only ever gets a material mode",
        modes_for("Blue", both, set(), 1) == ["LesserCraftingMaterials"],
    )
    check("green items are never salvaged", modes_for("Green", both, {"Prefix"}, 1) == [])


def test_native_decoder_stays_disarmed() -> None:
    """The salvage path must not call the native text decoder.

    A source-level guard rather than a behavioural one: executing the real
    helper needs the injected client, and the thing being prevented is a crash
    that no assertion could survive anyway. What we can check offline is that
    the default has not quietly drifted back to "decoded".
    """
    section("native text decoder stays disarmed on the salvage path")

    source = (ROOT / "Py4GWCoreLib" / "Inventory.py").read_text(encoding="utf-8")

    start = source.index("    def _collect_salvage_choice_option_text(")
    signature = source[start : source.index(") -> str:", start)]
    check(
        "_collect_salvage_choice_option_text defaults to reading no text",
        'text_source: str = "none"' in signature,
        signature,
    )

    # Neither caller may override that default back to the decoder. Parsed
    # rather than grepped: the docstring warning about "decoded" is prose, and
    # a substring search cannot tell the warning from the mistake.
    for path in (
        ROOT / "Py4GWCoreLib" / "Inventory.py",
        ROOT / "Sources" / "frenkeyLib" / "ItemHandling" / "UIManagerExtensions.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [
            "line %d" % node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "text_source"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "decoded"
        ]
        check("%s never asks for decoded salvage text" % path.name, not offenders, repr(offenders))

    # The decoder is reachable by any `.text()` call, not only through
    # text_source. An earlier _describe() called it to make the journal nicer
    # and crashed the client from inside the crash instrumentation, so the ban
    # is on the call itself everywhere on this path except the one guarded
    # branch in _collect_frame_text that exists to be opted into.
    ume = ROOT / "Sources" / "frenkeyLib" / "ItemHandling" / "UIManagerExtensions.py"
    tree = ast.parse(ume.read_text(encoding="utf-8"))
    text_calls = [
        "line %d" % node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "text"
        and not node.args
    ]
    check(
        "nothing on the click path calls the native text decoder",
        not text_calls,
        repr(text_calls),
    )


def main() -> int:
    test_native_decoder_stays_disarmed()
    test_click_targets()
    test_confirm_lookup()
    test_option_choice()
    test_select_and_salvage()
    test_mode_selection_matrix()

    print("\n" + "-" * 60)
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
