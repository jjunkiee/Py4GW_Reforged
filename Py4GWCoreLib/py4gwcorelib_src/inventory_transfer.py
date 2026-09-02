"""Cross-account inventory transfer planner -- pure slot and stack arithmetic.

Phase 1 of `docs/loot/plans/cross-account-inventory-transfer.md`. This module
decides *what may be dropped* and *how many receiver slots a drop will cost*.
It never touches the client: no `Py4GW` import, no `PyInventory`, no shared
memory, no ImGui. That is deliberate -- the slot math is where a silent bug
costs the user real items, so it has to be provable offline. Its fixture is
`Examples and tests/tests/test_inventory_transfer_planner.py`.

Two callers, two different questions:

* The **coordinator** plans a round from the shared-memory snapshot, which
  carries `ModelID` and `Quantity` and nothing else. It cannot see tradability,
  customization or item type, so most eligibility flags arrive as `None`. Its
  question is "how big a drop budget may donor D have?", and a budget that is
  slightly too generous is harmless: the donor drops fewer than asked.
* The **donor** resolves the actual item ids from a live bag read, where every
  flag is known. Its question is "which items do I actually let go of?", and
  getting that wrong is destructive.

Rather than a boolean that means opposite things at the two call sites,
eligibility is three-valued: :data:`VERDICT_ALLOWED`, :data:`VERDICT_UNVERIFIED`
(a flag the check needed was `None`) and :data:`VERDICT_BLOCKED`.
:func:`select_plannable` admits the first two, :func:`select_droppable` admits
only the first. An unresolved flag therefore cannot reach a real `DropItem`
call by accident, and the coordinator still gets a usable budget.

Every stack number here rests on assumptions A1, A4 and A7 in the plan, none of
which is verified against a live client yet. Until they are, keep
:attr:`TransferPolicy.optimistic_merge` off, which makes every ground stack
reserve a whole slot and makes the math unable to under-count.
"""

import math
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import Iterable
from typing import Iterator
from typing import Protocol
from typing import Sequence

# --- Game constants (assumption-bearing; see plan section 4) ----------------

#: Maximum size of a stack. Assumption A4, unverified.
STACK_MAX = 250

#: Models that share one `ModelID` but do not merge into one stack. Dyes are
#: the known case: every colour is `Vial_Of_Dye = 146`, and two colours never
#: combine. Assumption A7, unverified -- a wrong entry here only costs an extra
#: round, a missing one costs a mispredicted slot budget.
NON_MERGING_MODELS: frozenset[int] = frozenset({146})

#: Bag ids the transfer reads, in the order both sides walk them. Equipment
#: Pack (5) and equipped items (22) are absent by construction, so equipped
#: gear is out of reach rather than filtered out. Matches
#: `Inventory.GetInventorySpace`, which also spans bags 1-4 only.
INVENTORY_BAG_IDS: tuple[int, ...] = (1, 2, 3, 4)

#: Snapshot-only fallback for spotting kits by model id, mirroring the same
#: fallback `Inventory.GetFirstIDKit` already uses. The donor should set
#: `is_id_kit` / `is_salvage_kit` from the native usage flags instead; these
#: sets exist because the coordinator's snapshot has nothing but model ids.
ID_KIT_MODELS: frozenset[int] = frozenset(
    {
        2989,  # Identification_Kit
        5899,  # Superior_Identification_Kit
        38620,  # Infinite_Identification_Kit
    }
)
SALVAGE_KIT_MODELS: frozenset[int] = frozenset(
    {
        2991,  # Expert_Salvage_Kit
        2992,  # Salvage_Kit
        5900,  # Superior_Salvage_Kit
        25881,  # Perfect_Salvage_Kit
        38621,  # Infinite_Superior_Salvage_Kit
    }
)


# --- Eligibility vocabulary ------------------------------------------------

VERDICT_ALLOWED = "allowed"
VERDICT_UNVERIFIED = "unverified"
VERDICT_BLOCKED = "blocked"

REASON_NONE = ""
REASON_EMPTY_SLOT = "empty slot"
REASON_NOT_TRADABLE = "not tradable"
REASON_CUSTOMIZED = "customized"
REASON_PROTECTED_MODEL = "protected model"
REASON_MODEL_FLOOR = "would drop below the model quantity floor"
REASON_LAST_ID_KIT = "last identification kit"
REASON_LAST_SALVAGE_KIT = "last salvage kit"
REASON_NOT_STACKABLE = "not stackable"
REASON_MODEL_NOT_LISTED = "model not on the whitelist"
REASON_ITEM_TYPE_NOT_LISTED = "item type not on the whitelist"

REASON_UNKNOWN_TRADABLE = "tradability unknown"
REASON_UNKNOWN_CUSTOMIZED = "customization unknown"
REASON_UNKNOWN_STACKABLE = "stackability unknown"
REASON_UNKNOWN_ITEM_TYPE = "item type unknown"


# --- Records ---------------------------------------------------------------


class SlotLike(Protocol):
    """The two fields the stack math actually reads off a receiver slot."""

    @property
    def model_id(self) -> int: ...

    @property
    def quantity(self) -> int: ...


class SharedSlotLike(Protocol):
    """Structural view of `shared_memory_src.InventorySlotStruct`."""

    BagID: int
    Slot: int
    ModelID: int
    Quantity: int


class SharedBagLike(Protocol):
    """Structural view of `shared_memory_src.InventoryBagStruct`."""

    BagID: int
    Size: int
    Slots: Sequence[SharedSlotLike]


@dataclass(frozen=True)
class ItemRecord:
    """One occupied inventory slot, as either side of the transfer sees it.

    `item_id` is `0` when the record came from the shared-memory snapshot,
    which carries no item ids -- that is exactly why the coordinator ships a
    count budget and the donor resolves the ids locally (plan section 6.4).

    The `bool | None` flags are tri-state on purpose: `None` means "this side
    cannot see it", not "false". See the module docstring.
    """

    bag_id: int
    slot: int
    model_id: int
    quantity: int = 1

    item_id: int = 0
    stackable: bool | None = None
    tradable: bool | None = None
    customized: bool | None = None
    item_type: int | None = None
    is_id_kit: bool | None = None
    is_salvage_kit: bool | None = None

    @property
    def key(self) -> tuple[int, int]:
        """Slot identity. Stable within one snapshot, not across them."""
        return (self.bag_id, self.slot)

    @property
    def sort_key(self) -> tuple[int, int]:
        """Bag 1 -> 4, slot 0 -> n: the order both sides must agree on."""
        try:
            bag_rank = INVENTORY_BAG_IDS.index(self.bag_id)
        except ValueError:
            bag_rank = len(INVENTORY_BAG_IDS)
        return (bag_rank, self.slot)

    @property
    def effective_quantity(self) -> int:
        """Quantity clamped to something a drop could plausibly mean."""
        return max(int(self.quantity), 1)

    def looks_like_id_kit(self) -> bool:
        """Native flag when the donor set one, model-id fallback otherwise."""
        if self.is_id_kit is not None:
            return self.is_id_kit
        return self.model_id in ID_KIT_MODELS

    def looks_like_salvage_kit(self) -> bool:
        """Native flag when the donor set one, model-id fallback otherwise."""
        if self.is_salvage_kit is not None:
            return self.is_salvage_kit
        return self.model_id in SALVAGE_KIT_MODELS


@dataclass(frozen=True)
class BagSnapshot:
    """One bag: its capacity and only its **occupied** slots."""

    bag_id: int
    size: int
    items: tuple[ItemRecord, ...] = ()

    @property
    def used_slots(self) -> int:
        return len(self.items)

    @property
    def free_slots(self) -> int:
        return max(int(self.size) - len(self.items), 0)


@dataclass(frozen=True)
class InventorySnapshot:
    """Bags 1-4 of one account at one instant.

    Free slots are derived the same way `Inventory.GetFreeSlotCount` derives
    them -- capacity minus occupied -- so the offline projection and the live
    re-check cannot drift apart in definition, only in freshness.
    """

    bags: tuple[BagSnapshot, ...] = ()

    @property
    def capacity(self) -> int:
        return sum(int(bag.size) for bag in self.bags)

    @property
    def used_slots(self) -> int:
        return sum(bag.used_slots for bag in self.bags)

    @property
    def free_slots(self) -> int:
        return max(self.capacity - self.used_slots, 0)

    def items(self) -> tuple[ItemRecord, ...]:
        """Every occupied slot in the agreed bag 1 -> 4, slot 0 -> n order."""
        every: list[ItemRecord] = []
        for bag in self.bags:
            every.extend(bag.items)
        every.sort(key=lambda item: item.sort_key)
        return tuple(every)

    def quantity_of(self, model_id: int) -> int:
        return sum(item.effective_quantity for item in self.items() if item.model_id == int(model_id))

    def without(self, keys: Iterable[tuple[int, int]]) -> "InventorySnapshot":
        """Copy with the named slots emptied. Used to advance a simulation."""
        removed = set(keys)
        return InventorySnapshot(
            bags=tuple(
                replace(bag, items=tuple(item for item in bag.items if item.key not in removed)) for bag in self.bags
            )
        )


@dataclass(frozen=True)
class DonorSnapshot:
    """One donor account and what it is holding.

    `key` is whatever the session addresses the account by -- the plan's
    message protocol puts the donor email in `ExtraData`, so that is the
    expected value, but the planner only ever compares it.
    """

    key: str
    inventory: InventorySnapshot


def snapshot_from_shared_bags(bags: Iterable[SharedBagLike]) -> InventorySnapshot:
    """Adapt `InventoryBagsStruct.iter_bags()` into planner records.

    Kept here rather than in the widget so there is one conversion, not one per
    consumer. The `Slots` array is fixed at `SHMEM_MAX_INVENTORY_BAG_SLOTS` and
    zero-filled past the end, so `Size` bounds the read and a zero `ModelID`
    marks an empty slot.

    Everything the struct cannot express stays `None`, which is what makes the
    resulting records plannable but not droppable.
    """
    collected: list[BagSnapshot] = []
    for bag in bags:
        size = int(bag.Size)
        items: list[ItemRecord] = []
        for index, slot in enumerate(bag.Slots):
            if index >= size:
                break
            model_id = int(slot.ModelID)
            if model_id == 0:
                continue
            quantity = int(slot.Quantity)
            items.append(
                ItemRecord(
                    bag_id=int(bag.BagID),
                    slot=int(slot.Slot),
                    model_id=model_id,
                    quantity=max(quantity, 1),
                    # A stack of more than one proves stackability; a single
                    # item proves nothing, and `None` keeps it that way.
                    stackable=True if quantity > 1 else None,
                )
            )
        collected.append(BagSnapshot(bag_id=int(bag.BagID), size=size, items=tuple(items)))
    return InventorySnapshot(bags=tuple(collected))


# --- Policy and eligibility ------------------------------------------------


@dataclass(frozen=True)
class TransferPolicy:
    """Everything the user gets to decide about a transfer.

    Named by string in the `TransferDropItems` message and resolved to this
    object on the donor, so both sides filter by the same rules.
    """

    name: str = "default"

    #: Exploit partial stacks on the receiver when budgeting. Off by default:
    #: with it off every ground stack reserves a whole slot, which can waste a
    #: round but can never under-count. Depends on unverified A4 and A7.
    optimistic_merge: bool = False

    #: Slots held back so one mispredicted merge cannot wedge a round.
    safety_margin: int = 1

    keep_last_id_kit: bool = True
    keep_last_salvage_kit: bool = True

    #: Customized gear is useless to the receiver, so it is skipped by default.
    allow_customized: bool = False

    #: Models never dropped, whatever else says.
    protected_models: tuple[int, ...] = ()

    #: `(model_id, keep_at_least)`. An item is only droppable if the donor
    #: still holds the floor afterwards -- drops are whole stacks (A1), so a
    #: single stack of 10 with a floor of 5 simply stays put.
    model_floors: tuple[tuple[int, int], ...] = ()

    #: Optional inclusion filters. An empty tuple means "no constraint".
    stackables_only: bool = False
    model_whitelist: tuple[int, ...] = ()
    item_type_whitelist: tuple[int, ...] = ()

    def floor_for(self, model_id: int) -> int:
        for model, floor in self.model_floors:
            if int(model) == int(model_id):
                return max(int(floor), 0)
        return 0


@dataclass(frozen=True)
class DonorContext:
    """Donor-wide totals the per-item checks need.

    Built once per donor rather than recomputed per item, and carried as a
    value so a caller can advance it (kit counts fall as kits get scheduled).
    """

    model_totals: dict[int, int] = field(default_factory=dict[int, int])
    id_kits: int = 0
    salvage_kits: int = 0

    def quantity_of(self, model_id: int) -> int:
        return self.model_totals.get(int(model_id), 0)


def build_donor_context(items: Iterable[ItemRecord]) -> DonorContext:
    """Total up a donor's holdings for the floor and last-kit checks."""
    totals: dict[int, int] = {}
    id_kits = 0
    salvage_kits = 0
    for item in items:
        totals[item.model_id] = totals.get(item.model_id, 0) + item.effective_quantity
        if item.looks_like_id_kit():
            id_kits += 1
        if item.looks_like_salvage_kit():
            salvage_kits += 1
    return DonorContext(model_totals=totals, id_kits=id_kits, salvage_kits=salvage_kits)


@dataclass(frozen=True)
class EligibilityResult:
    """Why an item may, may not, or cannot yet be shown to be droppable."""

    verdict: str
    reason: str = REASON_NONE

    @property
    def blocked(self) -> bool:
        return self.verdict == VERDICT_BLOCKED

    @property
    def plannable(self) -> bool:
        """Good enough to budget against."""
        return self.verdict != VERDICT_BLOCKED

    @property
    def droppable(self) -> bool:
        """Good enough to hand to `DropItem`."""
        return self.verdict == VERDICT_ALLOWED


def evaluate_item(item: ItemRecord, policy: TransferPolicy, context: DonorContext) -> EligibilityResult:
    """Rule on one item.

    A known violation always wins over a missing flag: an item that is proven
    protected reports BLOCKED even if its tradability is unknown, so the UI can
    say *why* instead of shrugging. Only when nothing is proven wrong and
    something is unknown does the verdict fall back to UNVERIFIED.
    """
    if item.model_id == 0:
        return EligibilityResult(VERDICT_BLOCKED, REASON_EMPTY_SLOT)

    if item.model_id in {int(model) for model in policy.protected_models}:
        return EligibilityResult(VERDICT_BLOCKED, REASON_PROTECTED_MODEL)

    if policy.model_whitelist and item.model_id not in {int(model) for model in policy.model_whitelist}:
        return EligibilityResult(VERDICT_BLOCKED, REASON_MODEL_NOT_LISTED)

    if item.tradable is False:
        return EligibilityResult(VERDICT_BLOCKED, REASON_NOT_TRADABLE)

    if item.customized is True and not policy.allow_customized:
        return EligibilityResult(VERDICT_BLOCKED, REASON_CUSTOMIZED)

    if policy.stackables_only and item.stackable is False:
        return EligibilityResult(VERDICT_BLOCKED, REASON_NOT_STACKABLE)

    if policy.item_type_whitelist and item.item_type is not None:
        if int(item.item_type) not in {int(kind) for kind in policy.item_type_whitelist}:
            return EligibilityResult(VERDICT_BLOCKED, REASON_ITEM_TYPE_NOT_LISTED)

    floor = policy.floor_for(item.model_id)
    if floor > 0 and context.quantity_of(item.model_id) - item.effective_quantity < floor:
        return EligibilityResult(VERDICT_BLOCKED, REASON_MODEL_FLOOR)

    if policy.keep_last_id_kit and item.looks_like_id_kit() and context.id_kits <= 1:
        return EligibilityResult(VERDICT_BLOCKED, REASON_LAST_ID_KIT)

    if policy.keep_last_salvage_kit and item.looks_like_salvage_kit() and context.salvage_kits <= 1:
        return EligibilityResult(VERDICT_BLOCKED, REASON_LAST_SALVAGE_KIT)

    if item.tradable is None:
        return EligibilityResult(VERDICT_UNVERIFIED, REASON_UNKNOWN_TRADABLE)
    if item.customized is None and not policy.allow_customized:
        return EligibilityResult(VERDICT_UNVERIFIED, REASON_UNKNOWN_CUSTOMIZED)
    if policy.stackables_only and item.stackable is None:
        return EligibilityResult(VERDICT_UNVERIFIED, REASON_UNKNOWN_STACKABLE)
    if policy.item_type_whitelist and item.item_type is None:
        return EligibilityResult(VERDICT_UNVERIFIED, REASON_UNKNOWN_ITEM_TYPE)

    return EligibilityResult(VERDICT_ALLOWED)


def _select(
    items: Iterable[ItemRecord],
    policy: TransferPolicy,
    context: DonorContext | None,
    accept: str,
) -> tuple[tuple[ItemRecord, ...], tuple[tuple[ItemRecord, EligibilityResult], ...]]:
    """Walk items in the agreed order, ruling on each.

    Kit counts fall as kits are taken, so a donor holding three ID kits gives
    up two and keeps the third. That sequencing is why this is a loop and not a
    comprehension.
    """
    ordered = sorted(items, key=lambda item: item.sort_key)
    running = build_donor_context(ordered) if context is None else context

    taken: list[ItemRecord] = []
    rejected: list[tuple[ItemRecord, EligibilityResult]] = []
    for item in ordered:
        result = evaluate_item(item, policy, running)
        wanted = result.droppable if accept == VERDICT_ALLOWED else result.plannable
        if not wanted:
            rejected.append((item, result))
            continue
        taken.append(item)
        totals = dict(running.model_totals)
        totals[item.model_id] = max(totals.get(item.model_id, 0) - item.effective_quantity, 0)
        running = DonorContext(
            model_totals=totals,
            id_kits=running.id_kits - (1 if item.looks_like_id_kit() else 0),
            salvage_kits=running.salvage_kits - (1 if item.looks_like_salvage_kit() else 0),
        )
    return tuple(taken), tuple(rejected)


def select_plannable(
    items: Iterable[ItemRecord], policy: TransferPolicy, context: DonorContext | None = None
) -> tuple[ItemRecord, ...]:
    """Coordinator side: everything not proven ineligible, for budgeting."""
    return _select(items, policy, context, VERDICT_UNVERIFIED)[0]


def select_droppable(
    items: Iterable[ItemRecord], policy: TransferPolicy, context: DonorContext | None = None
) -> tuple[ItemRecord, ...]:
    """Donor side: only items proven eligible, in drop order."""
    return _select(items, policy, context, VERDICT_ALLOWED)[0]


def explain_rejections(
    items: Iterable[ItemRecord], policy: TransferPolicy, context: DonorContext | None = None
) -> tuple[tuple[ItemRecord, EligibilityResult], ...]:
    """Everything `select_droppable` refused, with the reason, for the UI."""
    return _select(items, policy, context, VERDICT_ALLOWED)[1]


# --- Stack prediction ------------------------------------------------------


@dataclass
class ProjectedSlot:
    """A receiver slot as the projection believes it will look."""

    model_id: int
    quantity: int


def _resolve_stackable(item: ItemRecord) -> bool:
    """Unknown stackability counts as not stackable: costs a slot, never lies."""
    return bool(item.stackable)


def _merge_plan(
    receiver_slots: Sequence[SlotLike],
    model_id: int,
    quantity: int,
    stackable: bool,
    optimistic: bool,
) -> tuple[int, tuple[tuple[int, int], ...], int]:
    """Core of the stack math, shared by prediction and projection.

    Returns `(new slots needed, [(slot index, resulting quantity)], spill)`,
    where `spill` is the quantity that could not be merged and has to land in
    fresh slots. Prediction reads the first element; the projection applies all
    three. One owner, so the preview and the projection cannot disagree.
    """
    amount = max(int(quantity), 1)

    if not stackable or int(model_id) in NON_MERGING_MODELS or not optimistic:
        return 1, (), amount

    remaining = amount
    top_ups: list[tuple[int, int]] = []
    for index, slot in enumerate(receiver_slots):
        if int(slot.model_id) != int(model_id):
            continue
        held = int(slot.quantity)
        if held >= STACK_MAX:
            continue
        room = STACK_MAX - held
        moved = min(room, remaining)
        top_ups.append((index, held + moved))
        remaining -= moved
        if remaining <= 0:
            return 0, tuple(top_ups), 0

    return math.ceil(remaining / STACK_MAX), tuple(top_ups), remaining


def predict_slot_cost(
    receiver_slots: Sequence[SlotLike],
    model_id: int,
    quantity: int,
    stackable: bool,
    optimistic: bool,
) -> int:
    """Slots the receiver must have free to absorb one dropped ground stack.

    With `optimistic` off the answer is always 1, which is the conservative
    branch the plan defaults to until A4 and A7 are confirmed live.
    """
    return _merge_plan(receiver_slots, model_id, quantity, stackable, optimistic)[0]


@dataclass
class ReceiverProjection:
    """What the receiver's bags are expected to look like mid-round.

    Mutable and cheap on purpose: a round scans every donor item against it,
    and only stack contents and the free-slot count matter -- which physical
    bag and slot an item lands in does not.
    """

    free_slots: int
    slots: list[ProjectedSlot] = field(default_factory=list[ProjectedSlot])

    @classmethod
    def from_snapshot(cls, snapshot: "InventorySnapshot") -> "ReceiverProjection":
        return cls(
            free_slots=snapshot.free_slots,
            slots=[
                ProjectedSlot(model_id=item.model_id, quantity=item.effective_quantity) for item in snapshot.items()
            ],
        )

    def copy(self) -> "ReceiverProjection":
        return ReceiverProjection(
            free_slots=self.free_slots,
            slots=[ProjectedSlot(model_id=slot.model_id, quantity=slot.quantity) for slot in self.slots],
        )

    def cost_of(self, item: ItemRecord, optimistic: bool) -> int:
        return predict_slot_cost(
            self.slots, item.model_id, item.effective_quantity, _resolve_stackable(item), optimistic
        )

    def absorb(self, item: ItemRecord, optimistic: bool) -> int:
        """Apply a predicted move and return the slots it consumed."""
        needed, top_ups, spill = _merge_plan(
            self.slots, item.model_id, item.effective_quantity, _resolve_stackable(item), optimistic
        )
        for index, resulting in top_ups:
            self.slots[index].quantity = resulting
        remaining = spill
        for _ in range(needed):
            placed = min(remaining, STACK_MAX) if remaining > 0 else item.effective_quantity
            self.slots.append(ProjectedSlot(model_id=item.model_id, quantity=placed))
            remaining -= placed
        self.free_slots = max(self.free_slots - needed, 0)
        return needed


# --- Round planning --------------------------------------------------------


@dataclass(frozen=True)
class PlannedDrop:
    """One item the coordinator expects a donor to let go of this round.

    The coordinator cannot address it by id -- `item_id` is `0` on snapshot
    records and `ExtraData` is far too small for a manifest -- so this is a
    projection, not an instruction. The instruction is the per-donor count in
    :meth:`RoundPlan.budget_for`.
    """

    donor_key: str
    item: ItemRecord
    slot_cost: int


@dataclass(frozen=True)
class RoundPlan:
    """One PLAN -> DROP -> COLLECT pass, computed but not yet sent."""

    round_id: int
    drops: tuple[PlannedDrop, ...] = ()
    budget_start: int = 0
    budget_left: int = 0
    projected_receiver: ReceiverProjection = field(default_factory=lambda: ReceiverProjection(free_slots=0))
    remaining_donors: tuple[DonorSnapshot, ...] = ()

    @property
    def item_count(self) -> int:
        return len(self.drops)

    @property
    def slots_committed(self) -> int:
        return sum(drop.slot_cost for drop in self.drops)

    @property
    def donor_keys(self) -> tuple[str, ...]:
        seen: list[str] = []
        for drop in self.drops:
            if drop.donor_key not in seen:
                seen.append(drop.donor_key)
        return tuple(seen)

    def budget_for(self, donor_key: str) -> int:
        """`max_items` for this donor's `TransferDropItems` message."""
        return sum(1 for drop in self.drops if drop.donor_key == donor_key)

    def drops_for(self, donor_key: str) -> tuple[PlannedDrop, ...]:
        return tuple(drop for drop in self.drops if drop.donor_key == donor_key)


def plan_round(
    round_id: int,
    receiver: ReceiverProjection,
    donors: Sequence[DonorSnapshot],
    policy: TransferPolicy,
) -> RoundPlan:
    """Fill one round's budget from the donors, in order.

    Does not mutate `receiver`; the post-round state comes back on the plan, so
    chaining rounds is copying a value rather than trusting an alias.

    A note on the stopping rule: the plan document's pseudocode breaks out of a
    donor's items once `budget <= 0`, but `cost > budget` already admits a
    zero-cost merge at zero budget and blocks everything else. Breaking early
    would throw those free merges away, so the cost test alone decides. With
    `optimistic_merge` off no cost is ever zero and the two are identical.
    """
    projection = receiver.copy()
    budget = projection.free_slots - max(int(policy.safety_margin), 0)
    budget_start = budget

    drops: list[PlannedDrop] = []
    taken_by_donor: dict[str, set[tuple[int, int]]] = {}

    for donor in donors:
        for item in select_plannable(donor.inventory.items(), policy):
            cost = projection.cost_of(item, policy.optimistic_merge)
            if cost > budget:
                continue
            budget -= projection.absorb(item, policy.optimistic_merge)
            drops.append(PlannedDrop(donor_key=donor.key, item=item, slot_cost=cost))
            taken_by_donor.setdefault(donor.key, set()).add(item.key)

    remaining = tuple(
        replace(donor, inventory=donor.inventory.without(taken_by_donor.get(donor.key, set()))) for donor in donors
    )

    return RoundPlan(
        round_id=round_id,
        drops=tuple(drops),
        budget_start=budget_start,
        budget_left=budget,
        projected_receiver=projection,
        remaining_donors=remaining,
    )


# --- Session simulation ----------------------------------------------------

STOP_DONORS_EXHAUSTED = "donors have nothing eligible left"
STOP_RECEIVER_FULL = "receiver has no usable free slots"
STOP_STALLED = "two consecutive rounds moved nothing"
STOP_ROUND_LIMIT = "round limit reached"

#: Consecutive empty rounds before the session gives up (plan section 6.7).
STALL_LIMIT = 2

#: Simulation guard only. The live session re-reads state each round and stops
#: on its own terms; this keeps a preview from looping on a pathological input.
DEFAULT_MAX_ROUNDS = 16


@dataclass
class StallTracker:
    """Counts rounds that moved nothing.

    Shared by the preview and, later, the live session, so both agree on what
    "stalled" means.
    """

    limit: int = STALL_LIMIT
    consecutive_empty: int = 0

    def record(self, items_moved: int) -> bool:
        """Feed one round's result; returns True once the session should stop."""
        if items_moved > 0:
            self.consecutive_empty = 0
        else:
            self.consecutive_empty += 1
        return self.consecutive_empty >= self.limit

    def reset(self) -> None:
        self.consecutive_empty = 0


@dataclass(frozen=True)
class SessionPlan:
    """A whole transfer as the preview shows it, before a packet is sent.

    Rounds after the first assume every earlier round landed exactly as
    predicted, which the live session never assumes -- it re-reads state and
    re-plans. This is a forecast for the user, not a script.
    """

    rounds: tuple[RoundPlan, ...] = ()
    stop_reason: str = STOP_DONORS_EXHAUSTED
    leftover: tuple[tuple[str, ItemRecord], ...] = ()

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    @property
    def item_count(self) -> int:
        return sum(plan.item_count for plan in self.rounds)

    def iter_drops(self) -> Iterator[PlannedDrop]:
        for plan in self.rounds:
            for drop in plan.drops:
                yield drop


def plan_session(
    receiver: InventorySnapshot,
    donors: Sequence[DonorSnapshot],
    policy: TransferPolicy,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> SessionPlan:
    """Forecast the whole transfer: rounds, stop reason, and what stays behind."""
    projection = ReceiverProjection.from_snapshot(receiver)
    current = tuple(donors)
    tracker = StallTracker()

    rounds: list[RoundPlan] = []
    stop_reason = STOP_ROUND_LIMIT

    for index in range(max(int(max_rounds), 0)):
        if projection.free_slots - max(int(policy.safety_margin), 0) <= 0:
            stop_reason = STOP_RECEIVER_FULL
            break
        if not any(select_plannable(donor.inventory.items(), policy) for donor in current):
            stop_reason = STOP_DONORS_EXHAUSTED
            break

        plan = plan_round(index + 1, projection, current, policy)
        if plan.item_count > 0:
            rounds.append(plan)
        projection = plan.projected_receiver
        current = plan.remaining_donors

        if tracker.record(plan.item_count):
            stop_reason = STOP_STALLED
            break

    leftover: list[tuple[str, ItemRecord]] = []
    for donor in current:
        for item in select_plannable(donor.inventory.items(), policy):
            leftover.append((donor.key, item))

    return SessionPlan(rounds=tuple(rounds), stop_reason=stop_reason, leftover=tuple(leftover))


# --- Reconciliation --------------------------------------------------------


@dataclass(frozen=True)
class RoundReport:
    """What a round actually did, measured against what it promised.

    Built from live re-reads after COLLECT (plan section 6.7). `on_ground` is
    the count of unowned items still inside the rally radius: while it is
    non-zero the session has not settled, whatever else succeeded.
    """

    round_id: int
    planned_items: int
    dropped_items: int
    slots_used: int
    on_ground: int
    stalled: bool

    @property
    def collected_items(self) -> int:
        return max(self.dropped_items - self.on_ground, 0)

    @property
    def settled(self) -> bool:
        return self.on_ground == 0

    @property
    def matched_plan(self) -> bool:
        return self.dropped_items == self.planned_items and self.settled


def reconcile_round(
    plan: RoundPlan,
    dropped_by_donor: dict[str, int],
    receiver_free_before: int,
    receiver_free_after: int,
    ground_items_left: int,
    tracker: StallTracker | None = None,
) -> RoundReport:
    """Turn one round's live re-reads into a report and advance the stall guard.

    "Moved" is measured by the receiver's free-slot delta rather than by the
    donors' reports: a donor reports what it dropped, which is not the same as
    what the receiver managed to pick up, and only the latter earns the session
    another round.
    """
    dropped = sum(max(int(count), 0) for count in dropped_by_donor.values())
    slots_used = max(int(receiver_free_before) - int(receiver_free_after), 0)
    on_ground = max(int(ground_items_left), 0)

    guard = tracker if tracker is not None else StallTracker()
    stalled = guard.record(slots_used)

    return RoundReport(
        round_id=plan.round_id,
        planned_items=plan.item_count,
        dropped_items=dropped,
        slots_used=slots_used,
        on_ground=on_ground,
        stalled=stalled,
    )
