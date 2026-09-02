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

Every stack number rests on plan assumptions A1, A4 and A7, none of which is
verified against a live client. These checks prove the arithmetic is what the
plan says it is; they cannot prove the game agrees.

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
    item_type: int | None = None,
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


DEFAULT = IT.TransferPolicy()
NO_MARGIN = IT.TransferPolicy(safety_margin=0)
OPTIMISTIC = IT.TransferPolicy(optimistic_merge=True, safety_margin=0)


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

    policy = IT.TransferPolicy(protected_models=(DYE,))
    items = [live_item(IRON, 5, slot=0, stackable=True), live_item(DYE, 3, slot=1, stackable=True)]
    taken = IT.select_droppable(items, policy)
    check("protected model excluded", [item.model_id for item in taken] == [IRON], repr(taken))

    blocked = dict((item.model_id, result.reason) for item, result in IT.explain_rejections(items, policy))
    check("refusal names the protection", blocked.get(DYE) == IT.REASON_PROTECTED_MODEL, repr(blocked))

    # "Keep 5 cupcakes": drops are whole stacks (A1), so the only stack that
    # may go is the one that still leaves the floor standing.
    floors = IT.TransferPolicy(model_floors=((IRON, 5),))
    stacks = [live_item(IRON, 10, slot=0, stackable=True), live_item(IRON, 3, slot=1, stackable=True)]
    kept = IT.select_droppable(stacks, floors)
    check("floor keeps the stack it has to", [item.quantity for item in kept] == [3], repr(kept))

    check(
        "a lone stack under the floor never moves",
        IT.select_droppable([live_item(IRON, 6, stackable=True)], floors) == (),
    )

    whitelisted = IT.TransferPolicy(model_whitelist=(IRON,))
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
    # A model id parked past Size is outside the bag and must not be read.
    bags[1].Slots[5] = FakeSharedSlot(2, 5, IRON, 99)

    snapshot = IT.snapshot_from_shared_bags(bags)
    check("capacity is the sum of bag sizes", snapshot.capacity == 6, "got %r" % snapshot.capacity)
    check("empty slots are skipped", snapshot.used_slots == 2, "got %r" % snapshot.used_slots)
    check("free slots follow capacity minus used", snapshot.free_slots == 4)
    check("nothing is read past Size", all(item.quantity != 99 for item in snapshot.items()))

    records = snapshot.items()
    check("records keep bag and slot", [(r.bag_id, r.slot) for r in records] == [(1, 0), (1, 2)], repr(records))
    check("a real stack proves stackability", records[0].stackable is True)
    check("a single item proves nothing", records[1].stackable is None)
    check("the snapshot carries no item ids", all(r.item_id == 0 for r in records))
    check("so nothing from a snapshot is droppable", IT.select_droppable(records, DEFAULT) == ())
    check("but everything is plannable", len(IT.select_plannable(records, DEFAULT)) == 2)
    check("quantity_of totals a model", snapshot.quantity_of(IRON) == 250)


def main() -> int:
    test_planner_stays_client_free()
    test_slot_cost_conservative_branch()
    test_slot_cost_optimistic_branch()
    test_projection_matches_its_own_prediction()
    test_verdict_is_three_valued()
    test_selection_gates()
    test_protected_models_and_floors()
    test_last_kit_protection()
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

    print("\n" + "-" * 60)
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
