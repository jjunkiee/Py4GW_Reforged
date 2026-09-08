"""Offline fixture for the cross-account inventory transfer planner.

Phase 1 of `docs/loot/plans/cross-account-inventory-transfer.md`. The planner
is the part of the transfer that decides how many receiver slots a dropped
stack will cost, and which items a donor is allowed to let go of at all. A
silent bug there does not throw -- it drops the wrong item, or budgets a round
too generously and leaves a pile on the ground when the instance closes. So it
is written without a single client dependency and proved here, with no injected
Guild Wars and no shared memory.

The module is loaded straight off disk rather than imported as
`Py4GWCoreLib.py4gwcorelib_src.inventory_transfer`, because that package's
`__init__` pulls in `Py4GW` and needs the injected runtime. If this fixture
ever stops loading that way, the planner has grown a dependency it should not
have -- and `test_planner_stays_client_free` says so out loud.

The stack assumptions were settled live on 2026-09-03: A4 and A7 held, and A1
did not -- the native binding drops one unit per call whatever quantity it is
handed. The planner works around that with a stack limit rather than pretending
it is fixed, so the fixtures here opt into whole stacks explicitly and the gate
gets its own section. These checks prove the arithmetic is what the plan says it
is; they still cannot prove the game agrees.

Run:  python "Examples and tests/tests/test_inventory_transfer_planner.py"
Exit code 1 on any failure.
"""

import ast
import importlib.util
import pathlib
import sys
from typing import Any
from typing import Iterable
from typing import Sequence

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLANNER_PATH = ROOT / "Py4GWCoreLib" / "py4gwcorelib_src" / "inventory_transfer.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def section(title: str) -> None:
    print("\n== %s" % title)


def _load(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None, "no module spec for %s" % path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


IT = _load("_inventory_transfer", PLANNER_PATH)


# ---------------------------------------------------------------------------
# Builders. `snapshot_item` is what the coordinator sees through shared memory
# (model id and quantity, every other flag unresolved); `live_item` is what a
# donor sees through a real bag read.
# ---------------------------------------------------------------------------

IRON = 1000  # stand-in for any ordinary stackable material
DYE = 146  # ModelID.Vial_Of_Dye -- one model id, colours that never merge
ID_KIT = 2989  # ModelID.Identification_Kit
SALVAGE_KIT = 2992  # ModelID.Salvage_Kit


def snapshot_item(model_id: int, quantity: int = 1, bag_id: int = 1, slot: int = 0) -> Any:
    return IT.ItemRecord(
        bag_id=bag_id,
        slot=slot,
        model_id=model_id,
        quantity=quantity,
        stackable=True if quantity > 1 else None,
    )


def live_item(
    model_id: int,
    quantity: int = 1,
    bag_id: int = 1,
    slot: int = 0,
    stackable: bool = False,
    tradable: bool = True,
    customized: bool = False,
    # A live donor read always fills the item type, so a live record that leaves it
    # unknown is not a case the donor can produce. 30 is ItemType.Trophy: ordinary
    # junk, and pointedly not ItemType.Quest_Item.
    item_type: int | None = 30,
    item_id: int = 0,
) -> Any:
    return IT.ItemRecord(
        bag_id=bag_id,
        slot=slot,
        model_id=model_id,
        quantity=quantity,
        item_id=item_id or (bag_id * 1000 + slot),
        stackable=stackable,
        tradable=tradable,
        customized=customized,
        item_type=item_type,
    )


def one_bag(items: Iterable[Any], size: int | None = None, bag_id: int = 1) -> Any:
    """An inventory of a single bag. `size` defaults to exactly full."""
    items = tuple(items)
    return IT.InventorySnapshot(
        bags=(IT.BagSnapshot(bag_id=bag_id, size=len(items) if size is None else size, items=items),)
    )


def receiver(free_slots: int, held: Iterable[Any] = ()) -> Any:
    held = tuple(held)
    return one_bag(held, size=len(held) + free_slots)


def donor(key: str, items: Iterable[Any]) -> Any:
    held = tuple(items)
    return IT.DonorSnapshot(key=key, inventory=one_bag(held, size=max(len(held), 1)))


def slots(*pairs: tuple[int, int]) -> list[Any]:
    """Receiver slot list for the bare `predict_slot_cost` signature."""
    return [IT.ProjectedSlot(model_id=model, quantity=quantity) for model, quantity in pairs]


# Every policy below opts into whole stacks. The shipped default is
# `max_stack_quantity = 1` -- "unstacked items only", because the native binding
# drops one unit per call -- and leaving these at that default would make the
# stack gate refuse every fixture in this file, silently gutting the merge and
# budget coverage rather than testing it. The gate has its own section instead.
STACKS_OK = IT.STACK_MAX

DEFAULT = IT.TransferPolicy(max_stack_quantity=STACKS_OK)
NO_MARGIN = IT.TransferPolicy(safety_margin=0, max_stack_quantity=STACKS_OK)
OPTIMISTIC = IT.TransferPolicy(optimistic_merge=True, safety_margin=0, max_stack_quantity=STACKS_OK)


# ---------------------------------------------------------------------------
# 1. The purity claim
# ---------------------------------------------------------------------------


def test_planner_stays_client_free() -> None:
    section("planner has no client dependency")

    tree = ast.parse(PLANNER_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A bare relative import has no module name but still binds this
            # file to the package, which is the thing being ruled out.
            imported.append(node.module or ("." * node.level))

    banned = [name for name in imported if name.split(".")[0] in {"Py4GW", "Py4GWCoreLib", "PyImGui", "PyInventory"}]
    relative = [name for name in imported if name.startswith(".")]

    check("no injected-runtime imports", not banned, repr(banned))
    check("no package-relative imports", not relative, repr(relative))
    check("module loaded straight off disk", IT.__name__ == "_inventory_transfer")


# ---------------------------------------------------------------------------
# 2. predict_slot_cost -- the arithmetic that decides how much a round moves
# ---------------------------------------------------------------------------


def test_slot_cost_conservative_branch() -> None:
    section("slot cost, optimistic off (the default)")

    # With the optimistic branch off, nothing merges as far as the budget is
    # concerned: every ground stack reserves a whole slot. This cannot
    # under-count, which is the entire reason it is the default.
    empty: list[Any] = []
    check(
        "non-stackable costs one slot",
        IT.predict_slot_cost(empty, IRON, 1, False, False) == 1,
        "got %r" % IT.predict_slot_cost(empty, IRON, 1, False, False),
    )
    check(
        "full 250 stack costs one slot",
        IT.predict_slot_cost(empty, IRON, 250, True, False) == 1,
    )
    check(
        "a merge that would fit still costs one slot",
        IT.predict_slot_cost(slots((IRON, 200)), IRON, 40, True, False) == 1,
        "conservative branch must ignore partial stacks",
    )
    check(
        "quantity zero is clamped, never free",
        IT.predict_slot_cost(empty, IRON, 0, True, False) == 1,
    )


def test_slot_cost_optimistic_branch() -> None:
    section("slot cost, optimistic on")

    check(
        "a stack that fits entirely in a partial costs nothing",
        IT.predict_slot_cost(slots((IRON, 200)), IRON, 40, True, True) == 0,
        "got %r" % IT.predict_slot_cost(slots((IRON, 200)), IRON, 40, True, True),
    )
    check(
        "exactly filling the partial costs nothing",
        IT.predict_slot_cost(slots((IRON, 200)), IRON, 50, True, True) == 0,
    )
    check(
        "one over the partial spills into a slot",
        IT.predict_slot_cost(slots((IRON, 200)), IRON, 51, True, True) == 1,
    )
    check(
        "a full partial offers no room",
        IT.predict_slot_cost(slots((IRON, 250)), IRON, 10, True, True) == 1,
    )
    check(
        "a different model is not a merge target",
        IT.predict_slot_cost(slots((IRON, 200)), IRON + 1, 10, True, True) == 1,
    )
    check(
        "two partials are pooled",
        IT.predict_slot_cost(slots((IRON, 200), (IRON, 210)), IRON, 90, True, True) == 0,
    )
    # A7: dyes all report Vial_Of_Dye but colours never combine, so the
    # merge shortcut must not apply however stackable the item looks.
    check(
        "dyes never merge, even with room",
        IT.predict_slot_cost(slots((DYE, 8)), DYE, 1, True, True) == 1,
        "got %r" % IT.predict_slot_cost(slots((DYE, 8)), DYE, 1, True, True),
    )
    check(
        "unstackable items ignore the optimistic branch",
        IT.predict_slot_cost(slots((IRON, 200)), IRON, 1, False, True) == 1,
    )


def test_projection_matches_its_own_prediction() -> None:
    section("projection applies exactly what it predicted")

    # predict_slot_cost and ReceiverProjection.absorb share one core routine on
    # purpose: a preview that disagrees with the projection would drift a round
    # at a time and nobody would notice until items were on the floor.
    projection = IT.ReceiverProjection(free_slots=5, slots=slots((IRON, 200)))
    item = live_item(IRON, quantity=250, stackable=True)

    predicted = projection.cost_of(item, True)
    consumed = projection.absorb(item, True)

    check("prediction and application agree", predicted == consumed == 1, "%r vs %r" % (predicted, consumed))
    check("partial stack topped off to the cap", projection.slots[0].quantity == IT.STACK_MAX)
    check("remainder spilled into a new slot", [(s.model_id, s.quantity) for s in projection.slots[1:]] == [(IRON, 200)])
    check("free slots decremented once", projection.free_slots == 4)

    # A zero-cost merge must not consume a slot, or the budget drifts.
    before = projection.free_slots
    zero = projection.absorb(live_item(IRON, quantity=10, stackable=True), True)
    check("a pure merge costs no slot", zero == 0 and projection.free_slots == before)

    # The conservative branch records the stack it placed, so later items see
    # a truthful picture of the receiver even though they cannot merge into it.
    plain = IT.ReceiverProjection(free_slots=3)
    plain.absorb(live_item(IRON, quantity=250, stackable=True), False)
    check(
        "conservative absorb still records the stack",
        [(s.model_id, s.quantity) for s in plain.slots] == [(IRON, 250)] and plain.free_slots == 2,
    )

    # A projection may never claim more free slots than it has.
    floor = IT.ReceiverProjection(free_slots=0)
    floor.absorb(live_item(IRON), False)
    check("free slots never go negative", floor.free_slots == 0)


# ---------------------------------------------------------------------------
# 3. Eligibility -- the filter that stands between a policy and a real drop
# ---------------------------------------------------------------------------


def test_verdict_is_three_valued() -> None:
    section("eligibility verdicts")

    context = IT.build_donor_context([])

    unresolved = IT.evaluate_item(snapshot_item(IRON, 5), DEFAULT, context)
    check("snapshot record is unverified", unresolved.verdict == IT.VERDICT_UNVERIFIED, unresolved.reason)
    check("unverified is plannable", unresolved.plannable)
    check("unverified is NOT droppable", not unresolved.droppable)

    resolved = IT.evaluate_item(live_item(IRON, 5, stackable=True), DEFAULT, context)
    check("live record is allowed", resolved.verdict == IT.VERDICT_ALLOWED, resolved.reason)
    check("allowed is droppable", resolved.droppable)

    # A6: not-tradable is the proxy for "cannot be dropped" (quest items and
    # the like). A proven violation outranks an unknown flag, so the UI can
    # explain itself instead of shrugging.
    quest = IT.ItemRecord(bag_id=1, slot=0, model_id=IRON, quantity=1, tradable=False)
    verdict = IT.evaluate_item(quest, DEFAULT, context)
    check("untradable blocks", verdict.verdict == IT.VERDICT_BLOCKED and verdict.reason == IT.REASON_NOT_TRADABLE)

    customized = IT.evaluate_item(live_item(IRON, customized=True), DEFAULT, context)
    check("customized blocks by default", customized.reason == IT.REASON_CUSTOMIZED)
    check(
        "customized passes when the policy allows it",
        IT.evaluate_item(live_item(IRON, customized=True), IT.TransferPolicy(allow_customized=True), context).droppable,
    )


def test_selection_gates() -> None:
    section("select_plannable vs select_droppable")

    unresolved = [snapshot_item(IRON, 5, slot=0), snapshot_item(DYE, 1, slot=1)]
    check("coordinator can budget unresolved records", len(IT.select_plannable(unresolved, DEFAULT)) == 2)
    check("donor refuses to drop unresolved records", IT.select_droppable(unresolved, DEFAULT) == ())

    rejections = IT.explain_rejections(unresolved, DEFAULT)
    check(
        "refusal names the missing flag",
        [result.reason for _, result in rejections] == [IT.REASON_UNKNOWN_TRADABLE] * 2,
        repr([result.reason for _, result in rejections]),
    )


def test_protected_models_and_floors() -> None:
    section("protected models and quantity floors")

    policy = IT.TransferPolicy(protected_models=(DYE,), max_stack_quantity=STACKS_OK)
    items = [live_item(IRON, 5, slot=0, stackable=True), live_item(DYE, 3, slot=1, stackable=True)]
    taken = IT.select_droppable(items, policy)
    check("protected model excluded", [item.model_id for item in taken] == [IRON], repr(taken))

    blocked = dict((item.model_id, result.reason) for item, result in IT.explain_rejections(items, policy))
    check("refusal names the protection", blocked.get(DYE) == IT.REASON_PROTECTED_MODEL, repr(blocked))

    # "Keep 5 cupcakes": drops are whole stacks (A1), so the only stack that
    # may go is the one that still leaves the floor standing.
    floors = IT.TransferPolicy(model_floors=((IRON, 5),), max_stack_quantity=STACKS_OK)
    stacks = [live_item(IRON, 10, slot=0, stackable=True), live_item(IRON, 3, slot=1, stackable=True)]
    kept = IT.select_droppable(stacks, floors)
    check("floor keeps the stack it has to", [item.quantity for item in kept] == [3], repr(kept))

    check(
        "a lone stack under the floor never moves",
        IT.select_droppable([live_item(IRON, 6, stackable=True)], floors) == (),
    )

    whitelisted = IT.TransferPolicy(model_whitelist=(IRON,), max_stack_quantity=STACKS_OK)
    check(
        "whitelist excludes everything unlisted",
        [item.model_id for item in IT.select_droppable(items, whitelisted)] == [IRON],
    )


def test_last_kit_protection() -> None:
    section("last ID kit and salvage kit stay home")

    two_kits = [live_item(ID_KIT, slot=0), live_item(ID_KIT, slot=1)]
    check("one of two ID kits may go", len(IT.select_droppable(two_kits, DEFAULT)) == 1)

    single = [live_item(ID_KIT, slot=0), live_item(SALVAGE_KIT, slot=1), live_item(IRON, slot=2)]
    taken = IT.select_droppable(single, DEFAULT)
    check("the only kits stay", [item.model_id for item in taken] == [IRON], repr(taken))

    reasons = dict((item.model_id, result.reason) for item, result in IT.explain_rejections(single, DEFAULT))
    check("ID kit refusal is named", reasons.get(ID_KIT) == IT.REASON_LAST_ID_KIT, repr(reasons))
    check("salvage kit refusal is named", reasons.get(SALVAGE_KIT) == IT.REASON_LAST_SALVAGE_KIT, repr(reasons))

    # The native usage flag wins over the model-id fallback, which exists only
    # because the coordinator's snapshot has nothing but model ids to go on.
    disguised = IT.ItemRecord(bag_id=1, slot=0, model_id=IRON, quantity=1, tradable=True, customized=False,
                              is_id_kit=True)
    check("an explicit kit flag is honoured", IT.select_droppable([disguised], DEFAULT) == ())

    check(
        "kit protection is switchable",
        len(IT.select_droppable(single, IT.TransferPolicy(keep_last_id_kit=False, keep_last_salvage_kit=False))) == 3,
    )


def test_quest_item_protection() -> None:
    section("quest items are droppable, so the policy has to stop them")

    # Live run 2026-09-03: a quest item reported tradable and hit the ground next to
    # the junk item. Nothing else in the filter looks at what an item is for.
    quest = live_item(9001, item_type=IT.QUEST_ITEM_TYPE, tradable=True, customized=False)
    junk = live_item(IRON, slot=1)

    taken = IT.select_droppable([quest, junk], DEFAULT)
    check("the quest item stays", [item.model_id for item in taken] == [IRON], repr(taken))

    reasons = dict((item.model_id, result.reason) for item, result in IT.explain_rejections([quest], DEFAULT))
    check("the refusal is named", reasons.get(9001) == IT.REASON_QUEST_ITEM, repr(reasons))

    # A tradable, uncustomized quest item passes every earlier gate, so this rule has to
    # be what catches it rather than a side effect of one of the others.
    permissive = IT.TransferPolicy(keep_last_id_kit=False, keep_last_salvage_kit=False)
    check("no other rule catches it", IT.select_droppable([quest], permissive) == ())

    check(
        "protection is switchable",
        len(IT.select_droppable([quest], IT.TransferPolicy(protect_quest_items=False))) == 1,
    )

    # The coordinator's snapshot carries no item type, so it cannot prove an item is not
    # a quest item. Unproven, not allowed -- the donor settles it from its own live read.
    unseen = IT.ItemRecord(bag_id=1, slot=0, model_id=IRON, quantity=1, tradable=True, customized=False)
    verdict = IT.evaluate_item(unseen, DEFAULT, IT.build_donor_context([unseen]))
    check("an unseen item type is unverified", verdict.verdict == IT.VERDICT_UNVERIFIED, repr(verdict))
    check("and it says why", verdict.reason == IT.REASON_UNKNOWN_ITEM_TYPE, repr(verdict))
    check("so a donor will not drop it", IT.select_droppable([unseen], DEFAULT) == ())
    check("but a coordinator may plan it", len(IT.select_plannable([unseen], DEFAULT)) == 1)

def test_deterministic_order() -> None:
    section("both sides walk bags 1 -> 4, slot 0 -> n")

    # The coordinator projects in this order and the donor drops in it; if they
    # disagree the budget describes items the donor never touches.
    scrambled = [
        live_item(IRON, slot=3, bag_id=4),
        live_item(IRON, slot=1, bag_id=1),
        live_item(IRON, slot=0, bag_id=2),
        live_item(IRON, slot=0, bag_id=1),
    ]
    order = [(item.bag_id, item.slot) for item in IT.select_droppable(scrambled, DEFAULT)]
    check("selection order is stable", order == [(1, 0), (1, 1), (2, 0), (4, 3)], repr(order))


# ---------------------------------------------------------------------------
# 4. Round planning
# ---------------------------------------------------------------------------


def test_round_budgeting() -> None:
    section("round budgeting")

    empty_donor = IT.DonorSnapshot(key="a@x", inventory=IT.InventorySnapshot())
    blank = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(receiver(10)), [empty_donor], DEFAULT)
    check("an empty donor plans nothing", blank.item_count == 0 and blank.budget_for("a@x") == 0)

    single = donor("a@x", [live_item(IRON, slot=0)])
    plan = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(receiver(10)), [single], DEFAULT)
    check("one non-stackable, one drop", plan.item_count == 1 and plan.slots_committed == 1)
    check("budget starts at free slots minus the margin", plan.budget_start == 9, "got %r" % plan.budget_start)
    check("per-donor count is the message's max_items", plan.budget_for("a@x") == 1)

    # Receiver with no room at all: the round must plan nothing rather than
    # send a donor off to drop items that cannot be collected.
    full = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(receiver(0, [live_item(IRON, slot=i) for i in range(4)])),
                         [donor("a@x", [live_item(IRON, slot=0)])], DEFAULT)
    check("a full receiver plans nothing", full.item_count == 0, repr(full.drops))

    # The margin is the difference between a boring round and a wedged one.
    tight_items = [live_item(IRON, 250, slot=0, stackable=True)]
    tight = receiver(1, [live_item(DYE, slot=0)])
    guarded = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(tight), [donor("a@x", tight_items)], DEFAULT)
    check("one free slot is spent by the safety margin", guarded.item_count == 0)
    ungirded = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(tight), [donor("a@x", tight_items)], NO_MARGIN)
    check("without a margin the last slot is usable", ungirded.item_count == 1)


def test_round_respects_the_budget() -> None:
    section("a round never over-commits the receiver")

    held = [live_item(IRON, slot=i) for i in range(6)]
    target = receiver(4, held)  # capacity 10, four free
    givers = [
        donor("a@x", [live_item(IRON, slot=i) for i in range(5)]),
        donor("b@x", [live_item(DYE, slot=i) for i in range(5)]),
    ]
    plan = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(target), givers, DEFAULT)

    check("commits exactly the budget", plan.slots_committed == 3, "got %r" % plan.slots_committed)
    check("budget is spent", plan.budget_left == 0)
    check("the second donor still gets a share of nothing left over", plan.budget_for("b@x") == 0)
    check("first donor takes the whole budget", plan.budget_for("a@x") == 3)
    check("planned drops carry their donor", set(plan.donor_keys) == {"a@x"}, repr(plan.donor_keys))
    check("projection reserves the margin", plan.projected_receiver.free_slots == 1)

    # plan_round must not mutate the projection it was handed, or a caller
    # replanning the same round would silently double-book the receiver.
    fresh = IT.ReceiverProjection.from_snapshot(target)
    IT.plan_round(1, fresh, givers, DEFAULT)
    check("input projection is untouched", fresh.free_slots == 4)


def test_round_keeps_free_merges_after_the_budget() -> None:
    section("zero-cost merges survive an exhausted budget")

    # Deliberate divergence from the plan's section 6.3 pseudocode, which
    # breaks out of the item loop at `budget <= 0`. `cost > budget` already
    # blocks everything that costs a slot while still admitting a merge that
    # costs none, so breaking early would strand free items for another round.
    target = receiver(1, [live_item(IRON, 200, slot=0, stackable=True)])
    givers = [
        donor(
            "a@x",
            [
                live_item(DYE, slot=0),  # costs the one usable slot
                live_item(IRON, 10, slot=1, stackable=True),  # merges for free
            ],
        )
    ]
    plan = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(target), givers, OPTIMISTIC)
    moved = [(drop.item.model_id, drop.slot_cost) for drop in plan.drops]
    check("both the paid and the free item move", moved == [(DYE, 1), (IRON, 0)], repr(moved))
    check("budget landed at zero", plan.budget_left == 0)


def test_donors_advance_between_rounds() -> None:
    section("planned items leave the donor's remaining snapshot")

    givers = [donor("a@x", [live_item(IRON, slot=i) for i in range(5)])]
    plan = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(receiver(3)), givers, DEFAULT)
    left = plan.remaining_donors[0].inventory.items()
    check("two planned, three left", plan.item_count == 2 and len(left) == 3, "%r / %r" % (plan.item_count, len(left)))
    check("the right three are left", [item.slot for item in left] == [2, 3, 4], repr([i.slot for i in left]))


# ---------------------------------------------------------------------------
# 5. Multi-round decomposition and the stall guard
# ---------------------------------------------------------------------------


def test_multi_round_decomposition() -> None:
    section("multi-round decomposition")

    # Rounds repeat because a round under-delivers, not because the receiver
    # empties -- nothing frees a slot inside an explorable instance. Here the
    # coordinator budgets three, the donor drops one (a stale snapshot, a
    # filter difference, a bad moment), and the next round re-plans from what
    # actually happened. This is the live loop's shape, driven by hand.
    held: list[Any] = []
    givers = [donor("a@x", [live_item(IRON, slot=i) for i in range(5)])]

    first = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(receiver(4, held)), givers, DEFAULT)
    check("round 1 budgets three", first.item_count == 3, "got %r" % first.item_count)

    # The donor only managed the first item; re-read state reflects that.
    delivered = first.drops[0].item
    after_donor = IT.DonorSnapshot(
        key="a@x", inventory=givers[0].inventory.without([delivered.key])
    )
    after_receiver = receiver(3, [live_item(IRON, slot=0)])

    second = IT.plan_round(2, IT.ReceiverProjection.from_snapshot(after_receiver), [after_donor], DEFAULT)
    check("round 2 re-plans from what is really there", second.item_count == 2, "got %r" % second.item_count)
    check(
        "round 2 does not re-plan the delivered item",
        delivered.key not in [drop.item.key for drop in second.drops],
    )
    check("round ids are carried through", (first.round_id, second.round_id) == (1, 2))


def test_session_forecast() -> None:
    section("session forecast")

    nothing = IT.plan_session(receiver(10), [IT.DonorSnapshot(key="a@x", inventory=IT.InventorySnapshot())], DEFAULT)
    check("no donors, no rounds", nothing.round_count == 0)
    check("stop reason is exhaustion", nothing.stop_reason == IT.STOP_DONORS_EXHAUSTED, nothing.stop_reason)

    givers = [donor("a@x", [live_item(IRON, slot=i) for i in range(8)])]
    forecast = IT.plan_session(receiver(4), givers, DEFAULT)
    check("forecast moves what fits", forecast.item_count == 3, "got %r" % forecast.item_count)
    check("forecast stops on a full receiver", forecast.stop_reason == IT.STOP_RECEIVER_FULL, forecast.stop_reason)
    check("leftover is reported, not hidden", len(forecast.leftover) == 5, repr(len(forecast.leftover)))
    check("leftover names its donor", set(key for key, _ in forecast.leftover) == {"a@x"})
    check("drops iterate flat", len(list(forecast.iter_drops())) == 3)

    everything = IT.plan_session(receiver(20), [donor("a@x", [live_item(IRON, slot=i) for i in range(3)])], DEFAULT)
    check("a transfer that fits leaves nothing behind", everything.leftover == () and everything.item_count == 3)
    check("and stops because the donors are empty", everything.stop_reason == IT.STOP_DONORS_EXHAUSTED)


def test_stall_detection() -> None:
    section("stall detection")

    tracker = IT.StallTracker()
    check("one empty round is not a stall", tracker.record(0) is False)
    check("two consecutive empty rounds are", tracker.record(0) is True)

    tracker = IT.StallTracker()
    tracker.record(0)
    check("progress clears the count", tracker.record(4) is False and tracker.consecutive_empty == 0)
    check("and the guard starts over", tracker.record(0) is False)

    tracker.reset()
    check("reset clears the count", tracker.consecutive_empty == 0)


def test_reconcile_round() -> None:
    section("reconciling a round against live re-reads")

    plan = IT.plan_round(1, IT.ReceiverProjection.from_snapshot(receiver(6)),
                         [donor("a@x", [live_item(IRON, slot=i) for i in range(3)])], DEFAULT)
    check("plan under test moved three", plan.item_count == 3)

    clean = IT.reconcile_round(plan, {"a@x": 3}, receiver_free_before=6, receiver_free_after=3, ground_items_left=0)
    check("a clean round matches its plan", clean.matched_plan and clean.settled)
    check("slots used come from the receiver delta", clean.slots_used == 3)
    check("nothing stalled", not clean.stalled)

    # The settle warning exists for exactly this: the donor let go of three,
    # the receiver only got two, and one is lying on the floor.
    stranded = IT.reconcile_round(plan, {"a@x": 3}, receiver_free_before=6, receiver_free_after=4, ground_items_left=1)
    check("leftovers block 'settled'", not stranded.settled and not stranded.matched_plan)
    check("collected counts what actually landed", stranded.collected_items == 2, "got %r" % stranded.collected_items)

    # Progress is judged by the receiver's free-slot delta, not by what donors
    # claim: a donor reporting three drops that nobody picked up is a stall.
    tracker = IT.StallTracker()
    IT.reconcile_round(plan, {"a@x": 3}, 6, 6, 3, tracker)
    second = IT.reconcile_round(plan, {"a@x": 3}, 6, 6, 6, tracker)
    check("two rounds that moved nothing stall", second.stalled and second.slots_used == 0)

    # The free-slot delta under-counts every merge: an item that lands in an
    # existing stack costs no slot at all. Two such rounds in a row would stall a
    # session that is working, so the receiver's own report wins when there is one.
    merged = IT.StallTracker()
    IT.reconcile_round(plan, {"a@x": 3}, 6, 6, 0, merged, collected_items=3)
    still_going = IT.reconcile_round(plan, {"a@x": 3}, 6, 6, 0, merged, collected_items=3)
    check("a round that merged everything is not a stall", not still_going.stalled)
    check("and it reports what the receiver said it took", still_going.collected_items == 3)

    starved = IT.StallTracker()
    IT.reconcile_round(plan, {"a@x": 3}, 6, 6, 3, starved, collected_items=0)
    twice = IT.reconcile_round(plan, {"a@x": 3}, 6, 6, 3, starved, collected_items=0)
    check("but a reported zero still stalls", twice.stalled)

    check(
        "without a report the old derivation still applies",
        IT.reconcile_round(plan, {"a@x": 3}, 6, 4, 1).collected_items == 2,
    )


# ---------------------------------------------------------------------------
# 6. The shared-memory adapter
# ---------------------------------------------------------------------------


class FakeSharedSlot:
    def __init__(self, bag_id: int, slot: int, model_id: int, quantity: int) -> None:
        self.BagID = bag_id
        self.Slot = slot
        self.ModelID = model_id
        self.Quantity = quantity


class FakeSharedBag:
    """Mirrors InventoryBagStruct: a fixed 20-slot array, zero-filled past Size."""

    def __init__(self, bag_id: int, size: int, filled: Sequence[tuple[int, int]]) -> None:
        self.BagID = bag_id
        self.Size = size
        self.Slots = [FakeSharedSlot(bag_id, index, 0, 0) for index in range(20)]
        for index, (model_id, quantity) in enumerate(filled):
            self.Slots[index] = FakeSharedSlot(bag_id, index, model_id, quantity)


def test_shared_memory_adapter() -> None:
    section("shared-memory bags to planner records")

    bags = [
        FakeSharedBag(1, 4, [(IRON, 250), (0, 0), (DYE, 1)]),
        FakeSharedBag(2, 2, []),
    ]
    # An item parked past the published Size. This fixture used to assert such an
    # item was junk outside the bag and must not be read. A live client disproved
    # that: Size carries the bag's item count, not its capacity, so honouring it
    # silently drops real items out of the plan. See test_untrustworthy_bag_size.
    bags[1].Slots[5] = FakeSharedSlot(2, 5, IRON, 99)

    snapshot = IT.snapshot_from_shared_bags(bags)
    check("every occupied slot is read", snapshot.used_slots == 3, "got %r" % snapshot.used_slots)
    check("an item past Size is still read", any(item.quantity == 99 for item in snapshot.items()))
    check("empty slots are skipped", all(item.model_id != 0 for item in snapshot.items()))
    check("capacity is floored at the highest occupied slot", snapshot.capacity == 10, "got %r" % snapshot.capacity)
    check("free slots follow capacity minus used", snapshot.free_slots == 7)

    records = snapshot.items()
    check(
        "records keep bag and slot",
        [(r.bag_id, r.slot) for r in records] == [(1, 0), (1, 2), (2, 5)],
        repr(records),
    )
    check("a real stack proves stackability", records[0].stackable is True)
    check("a single item proves nothing", records[1].stackable is None)
    check("the snapshot carries no item ids", all(r.item_id == 0 for r in records))
    check("so nothing from a snapshot is droppable", IT.select_droppable(records, DEFAULT) == ())
    check("but everything is plannable", len(IT.select_plannable(records, DEFAULT)) == 3)
    check("quantity_of totals a model", snapshot.quantity_of(IRON) == 349)


def test_untrustworthy_bag_size() -> None:
    section("a Size that is really an item count cannot break the budget")

    # Reproduces what a live client published on 2026-09-03: a character holding
    # 19 items across 45 slots reported a total Size of 19. Bounding the read by
    # Size lost five of those items and reported five free slots that did not
    # exist, while the 26 slots that were actually free went unseen.
    packed = FakeSharedBag(1, 3, [(IRON, 10), (DYE, 1), (SALVAGE_KIT, 1)])
    sparse = FakeSharedBag(2, 1, [])
    sparse.Slots[9] = FakeSharedSlot(2, 9, IRON, 5)

    snapshot = IT.snapshot_from_shared_bags([packed, sparse])
    check("no item is lost to a short Size", snapshot.used_slots == 4, "got %r" % snapshot.used_slots)
    check("capacity can never sit below its contents", snapshot.capacity >= snapshot.used_slots)
    check("free slots are never negative", snapshot.free_slots >= 0)

    # The client that owns the bags knows the real number and overrides the guess.
    corrected = snapshot.with_capacity(45)
    check("an authoritative capacity replaces the guess", corrected.capacity == 45)
    check("and the free count follows it", corrected.free_slots == 41)
    check("the items survive the correction", corrected.used_slots == snapshot.used_slots)
    check(
        "item identity survives too",
        [(r.bag_id, r.slot) for r in corrected.items()] == [(r.bag_id, r.slot) for r in snapshot.items()],
    )
    check("a capacity below the contents is refused", snapshot.with_capacity(1).capacity == 4)


def test_policy_registry() -> None:
    section("named policies resolve the same on both sides")

    default = IT.resolve_policy("default")
    check("the default policy is the conservative one", default.optimistic_merge is False and default.safety_margin == 1)
    check("an unknown name falls back to default", IT.resolve_policy("no-such-policy").name == IT.DEFAULT_POLICY_NAME)
    check("an empty name falls back to default", IT.resolve_policy("").name == IT.DEFAULT_POLICY_NAME)
    check("names are case- and space-insensitive", IT.resolve_policy("  Stackables ").stackables_only is True)
    check("optimistic is opt-in by name", IT.resolve_policy("optimistic").optimistic_merge is True)
    check(
        "every built-in policy is named after its key",
        all(name == policy.name for name, policy in IT.BUILTIN_POLICIES.items()),
        repr(IT.BUILTIN_POLICIES),
    )

    # A widget must be able to add user policies without the planner growing a
    # mutable global, and a user policy may shadow a built-in one.
    custom = IT.TransferPolicy(name="mine", protected_models=(IRON,))
    check("caller-supplied policies win", IT.resolve_policy("mine", {"mine": custom}) is custom)
    check("built-ins are untouched by that", "mine" not in IT.BUILTIN_POLICIES)


def test_policy_text_parsers() -> None:
    section("policy text boxes parse into policy fields")

    check("a plain list parses", IT.parse_model_list("1000, 146") == (1000, 146))
    check("semicolons work too", IT.parse_model_list("1000; 146") == (1000, 146))
    check("order is preserved", IT.parse_model_list("146,1000") == (146, 1000))
    check("duplicates collapse", IT.parse_model_list("1000,1000") == (1000,))
    check("empty text is an empty tuple", IT.parse_model_list("") == ())
    check("whitespace-only text is empty", IT.parse_model_list("  ,  ") == ())

    # The user is still typing. A half-finished entry must drop itself, not the
    # entries that were already valid.
    check("garbage is dropped, not fatal", IT.parse_model_list("1000, abc, 146") == (1000, 146))
    check("a trailing comma is harmless", IT.parse_model_list("1000,") == (1000,))
    check("zero and negatives are not model ids", IT.parse_model_list("0, -5, 1000") == (1000,))

    check("a floor pair parses", IT.parse_model_floors("1000:5") == ((1000, 5),))
    check("several pairs parse", IT.parse_model_floors("1000:5, 146:2") == ((1000, 5), (146, 2)))
    check("spacing is ignored", IT.parse_model_floors("  1000 : 5  ") == ((1000, 5),))
    check("a pair with no colon is dropped", IT.parse_model_floors("1000, 146:2") == ((146, 2),))
    check("a non-numeric floor is dropped", IT.parse_model_floors("1000:many") == ())
    check("a zero floor is not a floor", IT.parse_model_floors("1000:0") == ())
    check("the first entry for a model wins", IT.parse_model_floors("1000:5, 1000:1") == ((1000, 5),))

    # The point of parsing at all: the result has to drive a real policy.
    policy = IT.TransferPolicy(
        protected_models=IT.parse_model_list("146"),
        model_floors=IT.parse_model_floors("1000:200"),
    )
    check("parsed protections reach the policy", policy.floor_for(IRON) == 200)
    held = [snapshot_item(IRON, 250, slot=0), snapshot_item(DYE, 1, slot=1)]
    check("and both exclusions bite", IT.select_plannable(held, policy) == ())


def test_status_codes() -> None:
    section("round status codes survive the c_float round trip")

    codes = [
        IT.STATUS_OK,
        IT.STATUS_BUSY,
        IT.STATUS_NOT_EXPLORABLE,
        IT.STATUS_NOTHING_ELIGIBLE,
        IT.STATUS_RALLY_FAILED,
        IT.STATUS_ERROR,
        IT.STATUS_NO_BUDGET,
        IT.STATUS_INVENTORY_FULL,
    ]
    check("codes are distinct", len(set(codes)) == len(codes), repr(codes))
    check("ok is falsy so a plain truth test cannot invert it", IT.STATUS_OK == 0)
    check(
        "every code round-trips through a float exactly",
        all(int(float(code)) == code for code in codes),
    )
    check("every code has a name", all(IT.status_name(code) != "" for code in codes))
    check(
        "every code is in STATUS_NAMES",
        all(code in IT.STATUS_NAMES for code in codes),
        repr(sorted(IT.STATUS_NAMES)),
    )
    check("an unknown code still reads as something", "unknown" in IT.status_name(99))




# ---------------------------------------------------------------------------
# 7. The session precheck -- the widget's review gate (plan phase 4)
# ---------------------------------------------------------------------------


class FakeSharedMap:
    def __init__(self, map_id: int, region: int = 1, district: int = 2, language: int = 3) -> None:
        self.MapID = map_id
        self.Region = region
        self.District = district
        self.Language = language


class FakeSharedHealth:
    def __init__(self, current: float) -> None:
        self.Current = current


class FakeSharedAgent:
    def __init__(self, name: str, map_id: int, health: float) -> None:
        self.CharacterName = name
        self.Map = FakeSharedMap(map_id)
        self.Health = FakeSharedHealth(health)


class FakeSharedParty:
    def __init__(self, party_id: int) -> None:
        self.PartyID = party_id


class FakeSharedBags:
    def __init__(self, bags: Sequence[Any]) -> None:
        self._bags = tuple(bags)

    def iter_bags(self):
        return iter(self._bags)


class FakeSharedAccount:
    """Mirrors the AccountStruct fields the precheck adapter reads."""

    def __init__(
        self,
        email: str,
        name: str = "Someone",
        map_id: int = 42,
        party_id: int = 7,
        health: float = 1.0,
        in_aggro: bool = False,
        isolation_group: int = 0,
        isolated: bool = False,
        bags: Sequence[Any] = (),
    ) -> None:
        self.AccountEmail = email
        self.AgentData = FakeSharedAgent(name, map_id, health)
        self.AgentPartyData = FakeSharedParty(party_id)
        self.InventoryBags = FakeSharedBags(bags)
        self.IsolationGroupID = isolation_group
        self.IsIsolated = isolated
        self.InAggro = in_aggro


def participant(key: str, **overrides: Any) -> Any:
    """A participant that passes every check unless an override breaks one."""
    settings: dict[str, Any] = dict(
        name=key.title(),
        map_id=42,
        region=1,
        district=2,
        language=3,
        party_id=7,
        explorable=True,
        health=1.0,
        inventory=receiver(10),
    )
    settings.update(overrides)
    return IT.ParticipantState(key=key, **settings)


def named(results: Sequence[Any], name: str) -> Any:
    for result in results:
        if result.name == name:
            return result
    raise AssertionError("no precheck named %r" % name)


def test_precheck_adapter() -> None:
    section("shared-memory account to participant state")

    account = FakeSharedAccount(
        "donor@example.com",
        name="Donor One",
        bags=[FakeSharedBag(1, 4, [(IRON, 250), (DYE, 1)])],
    )
    state = IT.participant_from_shared_account(account, explorable=True)

    check("the email is the session key", state.key == "donor@example.com")
    check("the character name is the label", state.label == "Donor One")
    check("instance identity carries map and party", state.instance_key == (42, 1, 2, 3, 7))
    check("bags come through the one adapter", state.inventory.used_slots == 2 and state.inventory.free_slots == 2)
    check("the caller owns the explorable answer", state.explorable is True)
    check("an unanswered explorable stays unresolved", IT.participant_from_shared_account(account).explorable is None)
    check("full health is alive", state.alive is True)
    corpse = IT.participant_from_shared_account(FakeSharedAccount("x", health=0.0))
    check("zero health is not alive", corpse.alive is False)
    check("a state converts to a donor snapshot", state.as_donor().key == "donor@example.com")
    check("an email with no character name still labels", IT.ParticipantState(key="only@mail").label == "only@mail")


def test_precheck_can_communicate() -> None:
    section("the isolation mirror agrees with AllAccounts._can_communicate")

    solo = participant("solo", party_id=0, isolation_group=0)
    check("an account may always reach itself", IT.can_communicate(solo, solo) is True)

    a = participant("a", party_id=7, isolation_group=1)
    b = participant("b", party_id=7, isolation_group=2)
    check("a shared party beats mismatched isolation groups", IT.can_communicate(a, b) is True)

    c = participant("c", party_id=0, isolation_group=1)
    d = participant("d", party_id=0, isolation_group=2)
    check("mismatched groups without a party are blocked", IT.can_communicate(c, d) is False)
    same_group = participant("e", party_id=0, isolation_group=1)
    check("matching groups without a party are fine", IT.can_communicate(c, same_group) is True)
    check("one grouped and one not is blocked", IT.can_communicate(c, participant("f", party_id=0)) is False)

    lonely = participant("g", party_id=0, isolated=True)
    check("legacy isolation blocks the ungrouped", IT.can_communicate(lonely, participant("h", party_id=0)) is False)
    plain_one = participant("i", party_id=0)
    plain_two = participant("j", party_id=0)
    check("two plain ungrouped accounts talk", IT.can_communicate(plain_one, plain_two) is True)


def test_precheck_passes_a_sound_session() -> None:
    section("a sound session passes every check")

    home = participant("receiver", inventory=receiver(6))
    away = participant("donor", inventory=one_bag([snapshot_item(IRON, 250)]))
    results = IT.precheck_session(home, home, [away], DEFAULT)

    check("every check reports", len(results) == 7, repr([r.name for r in results]))
    failed = [(r.name, r.state, r.detail) for r in results if not r.passed]
    check("all of them pass", IT.precheck_passed(results), repr(failed))
    check("the roles line names the receiver", "receiver" in named(results, IT.CHECK_ROLES).detail.lower())
    check(
        "usable free slots exclude the safety margin",
        named(results, IT.CHECK_RECEIVER_SPACE).detail.startswith("5 "),
        named(results, IT.CHECK_RECEIVER_SPACE).detail,
    )


def test_precheck_catches_each_failure() -> None:
    section("each precheck fails for its own reason")

    home = participant("receiver", inventory=receiver(6))
    away = participant("donor", inventory=one_bag([snapshot_item(IRON, 250)]))

    def check_state(label: str, results: Sequence[Any], name: str, state: str) -> None:
        result = named(results, name)
        check(label, result.state == state, "%s -> %s %r" % (name, result.state, result.detail))

    check_state(
        "no donor selected fails the roles check",
        IT.precheck_session(home, home, [], DEFAULT),
        IT.CHECK_ROLES,
        IT.CHECK_FAIL,
    )
    check_state(
        "the receiver cannot also donate",
        IT.precheck_session(home, home, [home], DEFAULT),
        IT.CHECK_ROLES,
        IT.CHECK_FAIL,
    )
    check_state(
        "an outpost fails the explorable check",
        IT.precheck_session(home, home, [participant("donor", explorable=False)], DEFAULT),
        IT.CHECK_EXPLORABLE,
        IT.CHECK_FAIL,
    )
    check_state(
        "an unknown instance type is unresolved, not a pass",
        IT.precheck_session(home, home, [participant("donor", explorable=None)], DEFAULT),
        IT.CHECK_EXPLORABLE,
        IT.CHECK_UNKNOWN,
    )
    check_state(
        "a different map fails the instance check",
        IT.precheck_session(home, home, [participant("donor", map_id=43)], DEFAULT),
        IT.CHECK_SAME_INSTANCE,
        IT.CHECK_FAIL,
    )
    check_state(
        "a different district fails the instance check",
        IT.precheck_session(home, home, [participant("donor", district=9)], DEFAULT),
        IT.CHECK_SAME_INSTANCE,
        IT.CHECK_FAIL,
    )
    check_state(
        "nobody in a party fails the instance check",
        IT.precheck_session(
            participant("receiver", party_id=0),
            participant("receiver", party_id=0),
            [participant("donor", party_id=0)],
            DEFAULT,
        ),
        IT.CHECK_SAME_INSTANCE,
        IT.CHECK_FAIL,
    )
    check_state(
        "aggro fails the combat check",
        IT.precheck_session(home, home, [participant("donor", in_aggro=True)], DEFAULT),
        IT.CHECK_COMBAT_FREE,
        IT.CHECK_FAIL,
    )
    check_state(
        "a corpse fails the combat check",
        IT.precheck_session(home, home, [participant("donor", health=0.0)], DEFAULT),
        IT.CHECK_COMBAT_FREE,
        IT.CHECK_FAIL,
    )
    check_state(
        "a full receiver fails the space check",
        IT.precheck_session(
            participant("receiver", inventory=receiver(0)),
            participant("receiver", inventory=receiver(0)),
            [away],
            DEFAULT,
        ),
        IT.CHECK_RECEIVER_SPACE,
        IT.CHECK_FAIL,
    )
    check_state(
        "a receiver with only the margin free fails the space check",
        IT.precheck_session(
            participant("receiver", inventory=receiver(1)),
            participant("receiver", inventory=receiver(1)),
            [away],
            DEFAULT,
        ),
        IT.CHECK_RECEIVER_SPACE,
        IT.CHECK_FAIL,
    )
    check_state(
        "one free slot is enough with no margin",
        IT.precheck_session(
            participant("receiver", inventory=receiver(1)),
            participant("receiver", inventory=receiver(1)),
            [away],
            NO_MARGIN,
        ),
        IT.CHECK_RECEIVER_SPACE,
        IT.CHECK_PASS,
    )
    check_state(
        "a running session refuses a second one",
        IT.precheck_session(home, home, [away], DEFAULT, session_busy=True),
        IT.CHECK_SESSION_FREE,
        IT.CHECK_FAIL,
    )

    # An isolated coordinator that is not in the party cannot reach anyone.
    outsider = participant("coordinator", party_id=0, isolation_group=5)
    check_state(
        "isolation blocks the coordinator",
        IT.precheck_session(outsider, home, [away], DEFAULT),
        IT.CHECK_CAN_COMMUNICATE,
        IT.CHECK_FAIL,
    )

    check(
        "an unresolved check is not a pass",
        not IT.precheck_passed(IT.precheck_session(home, home, [participant("donor", explorable=None)], DEFAULT)),
    )


def test_coordinator_exclusions_are_proven_only() -> None:
    section("the preview lists proven exclusions, not unknowns")

    items = [snapshot_item(IRON, 250, slot=0), snapshot_item(DYE, 1, slot=1)]
    policy = IT.TransferPolicy(protected_models=(DYE,), max_stack_quantity=STACKS_OK)

    excluded = IT.explain_exclusions(items, policy)
    check("only the proven-protected item is excluded", len(excluded) == 1, repr(excluded))
    check("and it carries the reason", excluded[0][1].reason == IT.REASON_PROTECTED_MODEL)
    check("the plannable item survives", IT.select_plannable(items, policy)[0].model_id == IRON)

    # The droppable explainer is the donor's view and rejects both, because a
    # snapshot cannot prove tradability. That is exactly why the preview needs
    # its own explainer rather than reusing this one.
    rejected = IT.explain_rejections(items, policy)
    check("the droppable explainer rejects both", len(rejected) == 2, repr(rejected))
    check(
        "the unproven one is only unverified",
        any(result.verdict == IT.VERDICT_UNVERIFIED for _item, result in rejected),
        repr(rejected),
    )


# ---------------------------------------------------------------------------
# 12. The stack gate -- the workaround for the disproved A1
# ---------------------------------------------------------------------------


def test_stack_limit_gate() -> None:
    section("stacks larger than the limit never move")

    shipped = IT.TransferPolicy()
    check("the shipped default is unstacked items only", shipped.stack_limit == 1)
    check(
        "every built-in policy agrees",
        all(policy.stack_limit == 1 for policy in IT.BUILTIN_POLICIES.values()),
        repr({name: policy.stack_limit for name, policy in IT.BUILTIN_POLICIES.items()}),
    )

    single = live_item(IRON, 1, slot=0, stackable=True)
    pair = live_item(IRON, 2, slot=1, stackable=True)
    check("a single unit passes the default", IT.evaluate_item(single, shipped, IT.DonorContext()).droppable)
    refused = IT.evaluate_item(pair, shipped, IT.DonorContext())
    check("a stack of two does not", refused.blocked, repr(refused))
    check("and it says why", refused.reason == IT.REASON_STACK_TOO_LARGE, refused.reason)

    five = IT.TransferPolicy(max_stack_quantity=5)
    check("a raised limit admits its own size", IT.evaluate_item(live_item(IRON, 5, stackable=True), five, IT.DonorContext()).droppable)
    check("and refuses one more", IT.evaluate_item(live_item(IRON, 6, stackable=True), five, IT.DonorContext()).blocked)

    # The gate has to be decided identically on both sides, or the coordinator
    # budgets a stack the donor will refuse and every round comes up short. The
    # snapshot record below has every tri-state flag unresolved, which is what
    # makes it *plannable* -- but quantity is never unresolved, so the verdict
    # here is a hard block on both sides rather than an unverified pass.
    snapshot_stack = snapshot_item(IRON, 4, slot=0)
    coordinator_view = IT.evaluate_item(snapshot_stack, shipped, IT.DonorContext())
    check("the coordinator blocks it too, not merely unverified", coordinator_view.blocked, repr(coordinator_view))
    check("for the same stated reason", coordinator_view.reason == IT.REASON_STACK_TOO_LARGE)
    check(
        "so it never reaches a round plan",
        IT.select_plannable([snapshot_stack], shipped) == (),
    )
    check(
        "and the preview lists it as excluded",
        [result.reason for _item, result in IT.explain_exclusions([snapshot_stack], shipped)]
        == [IT.REASON_STACK_TOO_LARGE],
    )

    check(
        "the limit floors at one rather than disabling the transfer",
        IT.TransferPolicy(max_stack_quantity=0).stack_limit == 1,
    )
    check(
        "and is not silently capped, so a policy means what it says",
        IT.TransferPolicy(max_stack_quantity=250).stack_limit == 250,
    )


def test_ground_pile_accounting() -> None:
    section("a stack becomes one ground pile per unit")

    # One drop call per unit (A1 disproved), so a donor asked to move a stack of
    # three leaves three piles. The receiver's pickup budget counts piles; if it
    # counted bag slots it would collect one and walk away from the other two.
    three = IT.TransferPolicy(max_stack_quantity=3, safety_margin=0)
    items = [live_item(IRON, 3, slot=0, stackable=True), live_item(DYE, 1, slot=1)]
    plan = IT.plan_round(1, IT.ReceiverProjection(free_slots=10), [donor("a@x", items)], three)

    check("both items are planned", plan.item_count == 2, repr(plan.drops))
    check("but four piles hit the floor", plan.ground_item_count == 4, repr(plan.ground_item_count))
    check("counted per donor as well", plan.ground_items_for("a@x") == 4)
    check("the donor's own budget stays in items", plan.budget_for("a@x") == 2)

    stack_drop = next(drop for drop in plan.drops if drop.item.model_id == IRON)
    check("the stack alone is three piles", stack_drop.ground_items == 3)
    check("and still reserves one receiver slot", stack_drop.slot_cost == 1)

    check(
        "a single item is one pile",
        next(drop for drop in plan.drops if drop.item.model_id == DYE).ground_items == 1,
    )


def test_stack_limit_wire_field() -> None:
    section("the stack limit crosses the wire in ExtraData[2]")

    check("it round-trips", IT.parse_stack_limit(IT.format_stack_limit(5)) == 5)
    check("the rendered form is short enough for the field", len(IT.format_stack_limit(20)) < 63)
    check("a bare integer is accepted too", IT.parse_stack_limit("7") == 7)

    # An unset field means a coordinator that did not ask for stacks. The quiet
    # answer to that is to move none -- never to guess a larger number.
    check("an empty field falls back to the default", IT.parse_stack_limit("") == IT.DEFAULT_MAX_STACK_QUANTITY)
    check("so does an unreadable one", IT.parse_stack_limit("stack=lots") == IT.DEFAULT_MAX_STACK_QUANTITY)
    check("the caller may name its own fallback", IT.parse_stack_limit("", default=4) == 4)

    check("an absurd request is clamped, not honoured", IT.parse_stack_limit("stack=9999") == IT.MAX_STACK_QUANTITY)
    check("and so is a negative one", IT.parse_stack_limit("stack=-3") == 1)
    check("formatting clamps on the way out too", IT.format_stack_limit(9999).endswith(str(IT.MAX_STACK_QUANTITY)))

    # Both sides rebuild the policy through one function, so a stack the preview
    # refused is refused by the donor for the same stated reason.
    wired = IT.resolve_wire_policy("stackables", IT.format_stack_limit(5))
    check("the named policy still decides everything else", wired.name == "stackables" and wired.stackables_only)
    check("and the loose field decides the stack limit", wired.stack_limit == 5)
    check(
        "an unknown name still falls back to default without losing the limit",
        IT.resolve_wire_policy("nonsense", "stack=3").name == "default"
        and IT.resolve_wire_policy("nonsense", "stack=3").stack_limit == 3,
    )
    check("a message with no stack field moves no stacks", IT.resolve_wire_policy("default", "").stack_limit == 1)


# ---------------------------------------------------------------------------
# 13. Round reports -- what the live session waits on
# ---------------------------------------------------------------------------


def report(reporter: str, round_id: int = 1, moved: int = 0, status: int | None = None, session: str = "s1") -> Any:
    return IT.TransferReportEntry(
        session_id=session,
        round_id=round_id,
        reporter_email=reporter,
        items_moved=moved,
        slots_used=moved,
        status=IT.STATUS_OK if status is None else status,
    )


def test_report_store() -> None:
    section("the report store keeps one answer per participant per round")

    IT.clear_transfer_reports("s1")
    IT.file_transfer_report(report("donor@x", moved=2))
    IT.file_transfer_report(report("recv@x", moved=2))
    check("both are filed", len(IT.get_transfer_reports("s1", 1)) == 2)

    # A participant that answers twice for one round must overwrite, not
    # double-count: a report is how the coordinator decides a round is finished,
    # and two of them would end the round on the first.
    IT.file_transfer_report(report("donor@x", moved=5))
    filed = IT.get_transfer_reports("s1", 1)
    check("a second answer replaces the first", len(filed) == 2, repr(filed))
    check("with the newer value", any(entry.items_moved == 5 for entry in filed))

    IT.file_transfer_report(report("donor@x", round_id=2, moved=1))
    check("rounds stay separate", len(IT.get_transfer_reports("s1", 2)) == 1)
    check("and the session view spans both", len(IT.get_transfer_reports("s1")) == 3)

    IT.file_transfer_report(report("donor@x", session="s2"))
    IT.clear_transfer_reports("s1")
    check("clearing one session leaves the other", IT.get_transfer_reports("s1") == ())
    check("the other session survives", len(IT.get_transfer_reports("s2")) == 1)
    IT.clear_transfer_reports("s2")

    # A coordinator clears each round as it reconciles it, but nothing is obliged
    # to: an aborted session or a recall leaves entries with no owner. The store
    # has to be self-limiting or it grows for as long as the client runs.
    for index in range(IT.MAX_ROUND_REPORTS + 50):
        IT.file_transfer_report(report("donor@x", round_id=index, session="flood"))
    check(
        "the store stops growing at its bound",
        len(IT.get_transfer_reports("flood")) <= IT.MAX_ROUND_REPORTS,
        str(len(IT.get_transfer_reports("flood"))),
    )
    check(
        "and it is the newest reports that survive",
        any(entry.round_id == IT.MAX_ROUND_REPORTS + 49 for entry in IT.get_transfer_reports("flood")),
    )
    IT.clear_transfer_reports("flood")


def test_instance_cohort() -> None:
    section("the whole instance gets quietened, not just the participants")

    # A live run showed the party racing the receiver for the pile: selecting donors
    # decides whose items move, and has nothing to do with who can interfere.
    home = participant("recv", map_id=100, party_id=7)
    everyone = [
        home,
        participant("donor", map_id=100, party_id=7),
        participant("bystander", map_id=100, party_id=7),
        participant("other_party", map_id=100, party_id=9),
        participant("other_map", map_id=200, party_id=7),
        participant("solo", map_id=100, party_id=0),
    ]

    cohort = IT.accounts_sharing_instance(home, everyone)
    check("the receiver is included", "recv" in cohort, repr(cohort))
    check("so is a donor", "donor" in cohort)
    check("and so is a bystander nobody selected", "bystander" in cohort, repr(cohort))
    check("a different party is not", "other_party" not in cohort)
    check("a different map is not", "other_map" not in cohort)
    check("and neither is a partyless account on the same map", "solo" not in cohort)
    check("nobody is listed twice", len(set(cohort)) == len(cohort))

    check(
        "a partyless receiver quietens nobody, because it shares no instance",
        IT.accounts_sharing_instance(participant("recv", map_id=100, party_id=0), everyone) == (),
    )


def test_round_outcome() -> None:
    section("a round is over when everyone asked has answered")

    donors = ("d1@x", "d2@x")

    waiting = IT.summarize_round_reports(1, "s1", "recv@x", donors, reports=[report("d1@x", moved=2)])
    check("one donor short is not complete", not waiting.drops_complete)
    check("and it names who is missing", waiting.missing_donors == ("d2@x",), repr(waiting.missing_donors))
    check("the receiver has not spoken", not waiting.receiver_reported)

    # "Complete" means everyone answered, not that everyone succeeded. A donor
    # with nothing left reports NOTHING_ELIGIBLE, which is how a transfer ends
    # normally -- treating that as an incomplete round would hang the session.
    done = IT.summarize_round_reports(
        1,
        "s1",
        "recv@x",
        donors,
        reports=[
            report("d1@x", moved=2),
            report("d2@x", moved=0, status=IT.STATUS_NOTHING_ELIGIBLE),
            report("recv@x", moved=2),
        ],
    )
    check("every donor answered", done.drops_complete)
    check("the receiver answered", done.receiver_reported)
    check("donor items are summed without the receiver's", done.dropped == 2, repr(done.dropped))
    check("the receiver's count is read separately", done.collected == 2)
    check("the unhappy answer is still surfaced", len(done.failures) == 1, repr(done.failures))

    # Reports for another round must not satisfy this one's wait.
    stray = IT.summarize_round_reports(2, "s1", "recv@x", donors, reports=[report("d1@x", round_id=1)])
    check("a previous round's report does not count", stray.missing_donors == donors, repr(stray.missing_donors))


def main() -> int:
    test_planner_stays_client_free()
    test_slot_cost_conservative_branch()
    test_slot_cost_optimistic_branch()
    test_projection_matches_its_own_prediction()
    test_verdict_is_three_valued()
    test_selection_gates()
    test_protected_models_and_floors()
    test_last_kit_protection()
    test_quest_item_protection()
    test_deterministic_order()
    test_round_budgeting()
    test_round_respects_the_budget()
    test_round_keeps_free_merges_after_the_budget()
    test_donors_advance_between_rounds()
    test_multi_round_decomposition()
    test_session_forecast()
    test_stall_detection()
    test_reconcile_round()
    test_shared_memory_adapter()
    test_untrustworthy_bag_size()
    test_policy_registry()
    test_policy_text_parsers()
    test_status_codes()
    test_precheck_adapter()
    test_precheck_can_communicate()
    test_precheck_passes_a_sound_session()
    test_precheck_catches_each_failure()
    test_coordinator_exclusions_are_proven_only()
    test_stack_limit_gate()
    test_ground_pile_accounting()
    test_stack_limit_wire_field()
    test_report_store()
    test_instance_cohort()
    test_round_outcome()

    print("\n" + "-" * 60)
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
