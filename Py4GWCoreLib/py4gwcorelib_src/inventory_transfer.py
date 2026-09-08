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

Live runs on 2026-09-03 settled the stack assumptions this module encodes. A4
(a 250 cap, and a pickup topping off partial stacks before spilling) and A7
(dyes share a model id and refuse to merge across colours) both held. A1 did
not: the native binding drops **one unit per call** whatever quantity it is
handed, so moving a stack of ten means ten drops, ten ground piles and ten
pickup walks. That is a defect in `Py4GW_Reforged_Native`, not something this
module can fix, so it is bounded instead --
:attr:`TransferPolicy.max_stack_quantity` refuses any stack the session is not
willing to spend that many interruptible steps on, and defaults to 1, which
means "unstacked items only".

:attr:`TransferPolicy.optimistic_merge` stays off by default even though A4 and
A7 now support it. With it off every ground stack reserves a whole slot: that
can waste a round, and it can never under-count, which is still the trade worth
making on the path that moves someone's real items.
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

#: Units one `DropItem` call actually moves, whatever quantity it is handed.
#: Assumption A1 was **disproved** live on 2026-09-03: the native binding builds
#: a `DROP_ITEM` packet carrying a quantity word of 1 no matter what Python
#: passed, so a stack only leaves the bags if the donor calls the drop once per
#: unit. The defect belongs to `GW::item::DropItem` in `Py4GW_Reforged_Native`;
#: everything here is the workaround, and it becomes dead weight -- not wrong --
#: the day that binding honours its second argument.
DROP_UNITS_PER_CALL = 1

#: Largest stack the transfer will move when nothing says otherwise. One means
#: "unstacked items only", which is the honest default while a stack costs one
#: drop call, one ground pile and one pickup walk *per unit*: a stack of ten is
#: ten chances to be interrupted, and an interrupted stack ends up split across
#: two accounts and the floor.
DEFAULT_MAX_STACK_QUANTITY = 1

#: Ceiling the widget's input and :func:`parse_stack_limit` clamp to. Not a game
#: limit and not enforced by `TransferPolicy` itself -- it bounds what a user or
#: a message may *ask* for, on the grounds that no inventory slot should be
#: allowed to become twenty interruptible steps of walking and dropping. Raise
#: it when `DROP_UNITS_PER_CALL` does, and not before.
MAX_STACK_QUANTITY = 20

#: Bag ids the transfer reads, in the order both sides walk them. Equipment
#: Pack (5) and equipped items (22) are absent by construction, so equipped
#: gear is out of reach rather than filtered out. Matches
#: `Inventory.GetInventorySpace`, which also spans bags 1-4 only.
INVENTORY_BAG_IDS: tuple[int, ...] = (1, 2, 3, 4)

#: `ItemType.Quest_Item` from `Py4GWCoreLib/enums_src/Item_enums.py`, mirrored as
#: a literal because this module imports nothing from Py4GWCoreLib. Quest items
#: report as tradable and drop without complaint (verified live 2026-09-03), so
#: the tradability filter never sees them and they need a rule of their own.
QUEST_ITEM_TYPE = 21

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
REASON_QUEST_ITEM = "quest item"
REASON_PROTECTED_MODEL = "protected model"
REASON_MODEL_FLOOR = "would drop below the model quantity floor"
REASON_LAST_ID_KIT = "last identification kit"
REASON_LAST_SALVAGE_KIT = "last salvage kit"
REASON_NOT_STACKABLE = "not stackable"
REASON_STACK_TOO_LARGE = "stack larger than the drop limit"
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
    """Structural view of `shared_memory_src.InventoryBagStruct`.

    `Slots` is a read-only property rather than an attribute so the real struct
    matches: it declares `list[InventorySlotStruct]`, and a mutable protocol
    attribute would demand that exact type instead of accepting any sequence of
    something slot-shaped. Nothing here writes to a bag, so read-only is also
    the honest declaration.
    """

    BagID: int
    Size: int

    @property
    def Slots(self) -> Sequence[SharedSlotLike]: ...


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

    def with_capacity(self, capacity: int) -> "InventorySnapshot":
        """Copy with the total capacity replaced by an authoritative count.

        The shared-memory publisher does not write a capacity this module can
        trust (see :func:`snapshot_from_shared_bags`), so a client reading its
        own bags should correct the snapshot rather than budget against a number
        it knows is wrong. Per-bag structure collapses because nothing
        downstream reads it -- item ordering rides on each record's own bag id,
        not on the bag it is filed under here.
        """
        held = self.items()
        return InventorySnapshot(bags=(BagSnapshot(bag_id=0, size=max(int(capacity), len(held)), items=held),))

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
    the publisher zero-fills it on every update, so a zero `ModelID` is the only
    marker of an empty slot that this read needs.

    **`Size` is not currently a trustworthy capacity.** The publisher fills it
    from `ItemArray.GetBag(...).GetSize()`, and observed values match the bag's
    item count rather than its capacity -- a 45-slot character holding 19 items
    published a total `Size` of 19. Two consequences, both handled here rather
    than papered over: this read no longer stops at `Size` (it would silently
    drop any item sitting past the item count, losing it from the plan), and the
    capacity it reports is floored at the highest occupied slot. A client that
    can read its own bags should still correct the result through
    :meth:`InventorySnapshot.with_capacity` instead of trusting this number.

    Everything the struct cannot express stays `None`, which is what makes the
    resulting records plannable but not droppable.
    """
    collected: list[BagSnapshot] = []
    for bag in bags:
        published_size = int(bag.Size)
        highest_used = -1
        items: list[ItemRecord] = []
        for index, slot in enumerate(bag.Slots):
            model_id = int(slot.ModelID)
            if model_id == 0:
                continue
            highest_used = index
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
        # An item sitting at slot 7 proves the bag has at least 8 slots. That
        # floor matters because `Size` cannot currently be trusted to be the
        # bag's capacity, and a capacity below the contents would report negative
        # free space as zero and quietly refuse every transfer.
        size = max(published_size, highest_used + 1)
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

    #: Largest stack this transfer will move. Each unit costs its own drop call
    #: while `DROP_UNITS_PER_CALL` is 1, so this is the knob that decides how
    #: long a single inventory slot may hold a round open. Unlike the fields
    #: below it, this one does reach the donor: it rides in the drop message's
    #: third `ExtraData` field rather than being implied by the policy name.
    max_stack_quantity: int = DEFAULT_MAX_STACK_QUANTITY

    keep_last_id_kit: bool = True
    keep_last_salvage_kit: bool = True

    #: Customized gear is useless to the receiver, so it is skipped by default.
    allow_customized: bool = False

    #: Quest items are droppable and tradable, so nothing else here stops them
    #: leaving. Ferrying one to the wrong character can strand a quest, which is
    #: not a loss the receiver can undo by handing it back.
    protect_quest_items: bool = True

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

    @property
    def stack_limit(self) -> int:
        """The limit as every check reads it: at least one, otherwise as given.

        Deliberately floored and not capped. `MAX_STACK_QUANTITY` is what the
        widget's input and :func:`parse_stack_limit` clamp to, because those read
        untrusted values; a policy built in code is trusted with the number it
        was handed. Silently overriding a caller's field would make a policy that
        does not do what it says, which is a worse failure than a large one.
        """
        return max(1, int(self.max_stack_quantity))

    @property
    def drop_calls_per_stack(self) -> int:
        """Drop calls the largest stack this policy allows will cost.

        For the preview, which would otherwise be silent about the fact that
        raising the limit buys *slower* rounds, not bigger ones.
        """
        return -(-self.stack_limit // max(DROP_UNITS_PER_CALL, 1))


#: The policy a `TransferDropItems` message means when it names nothing, or
#: names something this build does not know.
DEFAULT_POLICY_NAME = "default"

#: Policies both sides can resolve from a name alone. `ExtraData` carries 63
#: characters, not a serialized policy, so the name is the whole contract: the
#: coordinator budgets with one of these and the donor filters with the same
#: one. A donor running an older build than the coordinator resolves an unknown
#: name to `default`, which is the most conservative entry -- that direction of
#: mismatch drops fewer items, never more.
BUILTIN_POLICIES: dict[str, TransferPolicy] = {
    "default": TransferPolicy(name="default"),
    "stackables": TransferPolicy(name="stackables", stackables_only=True),
    "optimistic": TransferPolicy(name="optimistic", optimistic_merge=True),
}


def resolve_policy(name: str, extra: dict[str, TransferPolicy] | None = None) -> TransferPolicy:
    """Resolve a policy name to a policy, falling back to the conservative default.

    `extra` lets a widget register user-defined policies without the planner
    growing a mutable global; it is searched first so a user policy may shadow a
    built-in one by name.
    """
    key = str(name or "").strip().lower()
    if extra and key in extra:
        return extra[key]
    return BUILTIN_POLICIES.get(key, BUILTIN_POLICIES[DEFAULT_POLICY_NAME])


#: Prefix the drop message's third `ExtraData` field carries, so a reader can
#: tell a stack limit from whatever else that field may grow to hold later.
STACK_LIMIT_FIELD_PREFIX = "stack="


def format_stack_limit(limit: int) -> str:
    """Render a stack limit for `TransferDropItems` `ExtraData[2]`.

    Every account runs the same build from one launcher, so this is not a
    version-negotiation field -- it exists because the limit is a *session*
    decision the coordinator makes and the donor enforces, and a policy name has
    no room to say it. Six characters into a 63-character field.
    """
    return "%s%d" % (STACK_LIMIT_FIELD_PREFIX, max(1, min(int(limit), MAX_STACK_QUANTITY)))


def parse_stack_limit(text: str, default: int = DEFAULT_MAX_STACK_QUANTITY) -> int:
    """Read `ExtraData[2]` back, falling back to the conservative default.

    Accepts the prefixed form and a bare integer, and answers `default` for an
    empty or unreadable field. Falling back rather than raising is deliberate:
    an unset field means a coordinator that did not ask for stacks, and the
    quiet answer to that is to move none -- never to guess a larger number.
    """
    fallback = max(1, min(int(default), MAX_STACK_QUANTITY))
    token = str(text or "").strip().lower()
    if not token:
        return fallback
    if token.startswith(STACK_LIMIT_FIELD_PREFIX):
        token = token[len(STACK_LIMIT_FIELD_PREFIX) :].strip()
    try:
        limit = int(token)
    except ValueError:
        return fallback
    return max(1, min(limit, MAX_STACK_QUANTITY))


def resolve_wire_policy(
    policy_name: str,
    stack_limit_field: str = "",
    extra: dict[str, TransferPolicy] | None = None,
) -> TransferPolicy:
    """Rebuild the policy a `TransferDropItems` message describes.

    The message carries a policy *name* plus one loose value, and both sides have
    to arrive at the same object from them: the coordinator to budget the round,
    the donor to decide what it actually lets go of. One function, so a stack the
    preview refused is refused by the donor for the same stated reason instead of
    for a coincidentally similar one.
    """
    return replace(
        resolve_policy(policy_name, extra),
        max_stack_quantity=parse_stack_limit(stack_limit_field),
    )


def parse_model_list(text: str) -> tuple[int, ...]:
    """Read `"1234, 5678"` into model ids for `TransferPolicy.protected_models`.

    Lives here rather than in the widget because it builds a policy field, and a
    policy that has to mean the same thing on two clients cannot afford two
    parsers. Anything that is not a positive integer is dropped rather than
    raising: this reads a text box the user is still typing into, and a
    half-finished entry must not blank the whole list.
    """
    models: list[int] = []
    for chunk in str(text or "").replace(";", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            model_id = int(token)
        except ValueError:
            continue
        if model_id > 0 and model_id not in models:
            models.append(model_id)
    return tuple(models)


def parse_model_floors(text: str) -> tuple[tuple[int, int], ...]:
    """Read `"1234:5, 5678:10"` into `TransferPolicy.model_floors` pairs.

    Same forgiving rule as :func:`parse_model_list`, and the first entry for a
    model wins so a duplicated line cannot quietly loosen a floor.
    """
    floors: list[tuple[int, int]] = []
    seen: set[int] = set()
    for chunk in str(text or "").replace(";", ",").split(","):
        token = chunk.strip()
        if not token or ":" not in token:
            continue
        raw_model, _, raw_floor = token.partition(":")
        try:
            model_id = int(raw_model.strip())
            floor = int(raw_floor.strip())
        except ValueError:
            continue
        if model_id > 0 and floor > 0 and model_id not in seen:
            seen.add(model_id)
            floors.append((model_id, floor))
    return tuple(floors)


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

    if policy.protect_quest_items and item.item_type == QUEST_ITEM_TYPE:
        return EligibilityResult(VERDICT_BLOCKED, REASON_QUEST_ITEM)

    # Quantity is the one flag that is never tri-state: the shared-memory
    # snapshot carries it and so does a live bag read, so both sides reach the
    # same verdict here and a stack refused by the coordinator is refused by the
    # donor for the same stated reason. It sits with the protections rather than
    # with the inclusion filters below because it is a capability limit -- the
    # binding cannot move this in one call -- not a user preference, and saying
    # so is more actionable than "not on the whitelist".
    if item.effective_quantity > policy.stack_limit:
        return EligibilityResult(VERDICT_BLOCKED, REASON_STACK_TOO_LARGE)

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
    if item.item_type is None and (policy.item_type_whitelist or policy.protect_quest_items):
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


def explain_exclusions(
    items: Iterable[ItemRecord], policy: TransferPolicy, context: DonorContext | None = None
) -> tuple[tuple[ItemRecord, EligibilityResult], ...]:
    """Everything `select_plannable` refused, with the reason, for the UI.

    The coordinator's counterpart to :func:`explain_rejections`. Its snapshot
    cannot see tradability, so nearly every item would appear in the droppable
    rejections with "tradability unknown" -- true, and useless as a preview.
    This lists only the items the coordinator can *prove* are ineligible, which
    is what a preview should show as excluded.
    """
    return _select(items, policy, context, VERDICT_UNVERIFIED)[1]


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

    @property
    def ground_items(self) -> int:
        """Separate ground piles this drop will produce.

        One per unit, because the binding moves one unit per call (A1). This is
        what the receiver's pickup budget has to count: budgeting it in bag
        slots would tell a receiver holding a donor's stack of three to collect
        one pile and walk away from the other two.
        """
        return max(self.item.effective_quantity // max(DROP_UNITS_PER_CALL, 1), 1)


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
    def ground_item_count(self) -> int:
        """Piles this round will put on the floor. The receiver's pickup budget."""
        return sum(drop.ground_items for drop in self.drops)

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

    def ground_items_for(self, donor_key: str) -> int:
        """Piles this donor alone will put on the floor."""
        return sum(drop.ground_items for drop in self.drops if drop.donor_key == donor_key)

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

    #: What the receiver said it collected, when it said anything. `None` falls
    #: back to inferring it from what the donors dropped and what is still lying
    #: there, which is all this had before the receiver reported at all.
    reported_collected: int | None = None

    @property
    def collected_items(self) -> int:
        if self.reported_collected is not None:
            return max(int(self.reported_collected), 0)
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
    collected_items: int | None = None,
) -> RoundReport:
    """Turn one round's live re-reads into a report and advance the stall guard.

    "Moved" is measured on the receiver, never on the donors: a donor reports
    what it dropped, which is not the same as what the receiver managed to pick
    up, and only the latter earns the session another round.

    *Which* receiver measurement matters. Pass `collected_items` -- the count in
    the receiver's own `TransferReport` -- and the stall guard uses that. Without
    it the guard falls back to the free-slot delta, which under-counts every
    merge: an item that lands in an existing stack costs no slot, so a round that
    moved a dozen of them looks empty and two of those in a row would stall a
    session that is working perfectly. The fallback is kept because this function
    predates the receiver's report and a caller without one is still better off
    with an imperfect signal than none.
    """
    dropped = sum(max(int(count), 0) for count in dropped_by_donor.values())
    slots_used = max(int(receiver_free_before) - int(receiver_free_after), 0)
    on_ground = max(int(ground_items_left), 0)

    guard = tracker if tracker is not None else StallTracker()
    stalled = guard.record(slots_used if collected_items is None else max(int(collected_items), 0))

    return RoundReport(
        round_id=plan.round_id,
        planned_items=plan.item_count,
        dropped_items=dropped,
        slots_used=slots_used,
        on_ground=on_ground,
        stalled=stalled,
        reported_collected=None if collected_items is None else max(int(collected_items), 0),
    )


# --- Round status codes ----------------------------------------------------

# `TransferReport` carries its status in `Params[3]`, which is a c_float: keep
# these small integers so the float round-trip is exact. They live here rather
# than in the message handler because the coordinator and the participant have
# to read the same vocabulary, and this module is the one both sides already
# import.

STATUS_OK = 0
STATUS_BUSY = 1
STATUS_NOT_EXPLORABLE = 2
STATUS_NOTHING_ELIGIBLE = 3
STATUS_RALLY_FAILED = 4
STATUS_ERROR = 5
STATUS_NO_BUDGET = 6
STATUS_INVENTORY_FULL = 7

STATUS_NAMES: dict[int, str] = {
    STATUS_OK: "ok",
    STATUS_BUSY: "another transfer is already running",
    STATUS_NOT_EXPLORABLE: "not in an explorable area",
    STATUS_NOTHING_ELIGIBLE: "nothing eligible to move",
    STATUS_RALLY_FAILED: "could not reach the rally point",
    STATUS_ERROR: "failed with an error",
    STATUS_NO_BUDGET: "no budget was granted",
    STATUS_INVENTORY_FULL: "receiver inventory is full",
}


def status_name(status: int) -> str:
    """Human-readable form of a report status, for the UI and the console."""
    return STATUS_NAMES.get(int(status), "unknown status %d" % int(status))


# --- Round reports ---------------------------------------------------------

# The coordinator files every `TransferReport` it receives and the widget reads
# them back to decide a round is over. Those are two different files -- the
# message handler in `Widgets/System/Messaging.py` and the widget in
# `Widgets/Guild Wars/Items & Loot/InventoryTransfer.py` -- and widgets are not
# importable from one another, so the store has to sit somewhere they share.
#
# It lives here rather than as a `setattr` on `GLOBAL_CACHE` because this module
# is already imported by both and is loaded once for the process: a module-level
# dict in `Py4GWCoreLib` outlives a widget reload for exactly the same reason
# `GLOBAL_CACHE` itself does, and it has one declared owner instead of an
# undeclared attribute and two readers.


@dataclass(frozen=True)
class TransferReportEntry:
    """One participant's result for one round, as the coordinator files it."""

    session_id: str
    round_id: int
    reporter_email: str
    items_moved: int
    slots_used: int
    status: int
    timestamp: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def status_text(self) -> str:
        return status_name(self.status)


#: `(session_id, round_id, reporter_email) -> entry`. Keyed by reporter so a
#: participant that answers twice for one round overwrites rather than
#: double-counting -- which matters, because a report is how the coordinator
#: decides a round is finished.
_ROUND_REPORTS: dict[tuple[str, int, str], TransferReportEntry] = {}

#: Entries kept before the oldest are dropped. A coordinator clears each round as
#: it reconciles it, but nothing is *obliged* to: an aborted session, a recall, or
#: a client that never ran the widget all leave entries with no owner to remove
#: them. A round costs at most one entry per participant, so this holds many
#: sessions' worth and still cannot grow without bound over a long play session.
MAX_ROUND_REPORTS = 512


def file_transfer_report(entry: TransferReportEntry) -> None:
    """Record one participant's answer. Called by the message handler."""
    _ROUND_REPORTS[(entry.session_id, entry.round_id, entry.reporter_email)] = entry
    # Insertion-ordered, so the front of the dict is the oldest report filed.
    while len(_ROUND_REPORTS) > MAX_ROUND_REPORTS:
        _ROUND_REPORTS.pop(next(iter(_ROUND_REPORTS)))


def get_transfer_reports(session_id: str, round_id: int | None = None) -> tuple[TransferReportEntry, ...]:
    """Every report filed for a session, optionally narrowed to one round."""
    session = str(session_id or "").strip()
    entries = [
        entry
        for (entry_session, entry_round, _reporter), entry in _ROUND_REPORTS.items()
        if entry_session == session and (round_id is None or entry_round == int(round_id))
    ]
    entries.sort(key=lambda entry: (entry.round_id, entry.reporter_email))
    return tuple(entries)


def clear_transfer_reports(session_id: str, round_id: int | None = None) -> None:
    """Forget a session's reports once the coordinator has reconciled them."""
    session = str(session_id or "").strip()
    stale = [key for key in _ROUND_REPORTS if key[0] == session and (round_id is None or key[1] == int(round_id))]
    for key in stale:
        _ROUND_REPORTS.pop(key, None)


@dataclass(frozen=True)
class RoundOutcome:
    """What one round's reports add up to, from the coordinator's side.

    The live session's two waits -- "have all the donors answered?" and "has the
    receiver answered?" -- are the part of phase 5 most worth proving offline,
    so the arithmetic is here and only the timing is in the widget.
    """

    round_id: int
    receiver_key: str
    donor_keys: tuple[str, ...]
    reports: tuple[TransferReportEntry, ...] = ()

    def _by(self, key: str) -> TransferReportEntry | None:
        for entry in self.reports:
            if entry.reporter_email == key:
                return entry
        return None

    @property
    def missing_donors(self) -> tuple[str, ...]:
        return tuple(key for key in self.donor_keys if self._by(key) is None)

    @property
    def drops_complete(self) -> bool:
        """Every donor asked has answered, however it answered."""
        return not self.missing_donors

    @property
    def receiver_report(self) -> TransferReportEntry | None:
        return self._by(self.receiver_key)

    @property
    def receiver_reported(self) -> bool:
        return self.receiver_report is not None

    @property
    def dropped(self) -> int:
        """Items the donors say left their bags."""
        return sum(entry.items_moved for entry in self.reports if entry.reporter_email in self.donor_keys)

    @property
    def collected(self) -> int:
        """Ground piles the receiver says it picked up."""
        entry = self.receiver_report
        return entry.items_moved if entry is not None else 0

    @property
    def failures(self) -> tuple[TransferReportEntry, ...]:
        """Reports that came back with anything other than STATUS_OK.

        Not necessarily an error the session should stop for: a donor with
        nothing eligible left reports `STATUS_NOTHING_ELIGIBLE`, which is how a
        transfer ends normally.
        """
        return tuple(entry for entry in self.reports if not entry.ok)


def summarize_round_reports(
    round_id: int,
    session_id: str,
    receiver_key: str,
    donor_keys: Sequence[str],
    reports: Iterable[TransferReportEntry] | None = None,
) -> RoundOutcome:
    """Collect one round's filed reports into a verdict.

    `reports` is injectable so the offline fixture can prove this without
    touching the module-level store; the live caller leaves it `None` and gets
    whatever the message handler has filed.
    """
    filed = tuple(reports) if reports is not None else get_transfer_reports(session_id, round_id)
    return RoundOutcome(
        round_id=int(round_id),
        receiver_key=str(receiver_key or ""),
        donor_keys=tuple(donor_keys),
        reports=tuple(entry for entry in filed if entry.round_id == int(round_id)),
    )


# --- Session precheck ------------------------------------------------------

# Plan section 6.2. The coordinator has to answer "may this session run at all?"
# before it computes a budget, and the answer has to be reported check by check
# rather than as one silent boolean -- a user whose transfer refuses to start
# deserves to know which of the conditions failed.
#
# Everything here reads the shared-memory snapshot, which is up to
# SHMEM_PLAYER_INVENTORY_UPDATE_THROTTLE_MS stale (1.5 s). That is good enough
# to refuse a session and never good enough to authorise one: each participant
# re-checks its own state before acting, which is why the donor handler carries
# its own explorable gate.

CHECK_PASS = "pass"
CHECK_FAIL = "fail"

#: The snapshot cannot answer this one. Not a pass: a live session has to
#: resolve it before it is allowed to send anything.
CHECK_UNKNOWN = "unknown"

CHECK_ROLES = "one receiver and at least one donor"
CHECK_EXPLORABLE = "every participant is in an explorable area"
CHECK_SAME_INSTANCE = "every participant shares one instance and party"
CHECK_CAN_COMMUNICATE = "the coordinator can message every participant"
CHECK_COMBAT_FREE = "nobody is in aggro and nobody is dead"
CHECK_RECEIVER_SPACE = "the receiver has usable free slots"
CHECK_SESSION_FREE = "no other transfer session is running"


@dataclass(frozen=True)
class PrecheckResult:
    """One named precheck and how it came out."""

    name: str
    state: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.state == CHECK_PASS

    @property
    def failed(self) -> bool:
        return self.state == CHECK_FAIL

    @property
    def unresolved(self) -> bool:
        return self.state == CHECK_UNKNOWN


@dataclass(frozen=True)
class ParticipantState:
    """One account as the coordinator's shared-memory snapshot sees it.

    Flat rather than a view onto `AccountStruct`, because this module may not
    import the client. :func:`participant_from_shared_account` is the one
    adapter, so a widget builds these and never grows a second conversion.
    """

    key: str
    name: str = ""

    map_id: int = 0
    region: int = 0
    district: int = 0
    language: int = 0
    party_id: int = 0

    isolation_group: int = 0
    isolated: bool = False

    in_aggro: bool = False

    #: Fraction of maximum health, matching `HealthStruct.Current`. Zero is
    #: dead, and is also what an unpopulated slot reads, so a session refuses
    #: rather than guesses.
    health: float = 1.0

    #: `None` when the caller could not tell. The snapshot carries a map id and
    #: no instance type, so only the local client can answer this for certain;
    #: a coordinator infers it for peers from the map id and says so.
    explorable: bool | None = None

    inventory: InventorySnapshot = field(default_factory=InventorySnapshot)

    @property
    def label(self) -> str:
        """What the UI should call this account."""
        return self.name or self.key

    @property
    def instance_key(self) -> tuple[int, int, int, int, int]:
        """The tuple two accounts must share to be in one explorable instance.

        Map id, region, district and language pin the instance; the party id
        pins the copy of it, because explorable instances are party-scoped.
        Same predicate as `on_same_map_and_party` in `HeroAI/commands.py`.
        """
        return (self.map_id, self.region, self.district, self.language, self.party_id)

    @property
    def alive(self) -> bool:
        return self.health > 0.0

    def as_donor(self) -> DonorSnapshot:
        """The planner's view of this account as a source of items."""
        return DonorSnapshot(key=self.key, inventory=self.inventory)


class SharedMapLike(Protocol):
    """Structural view of `shared_memory_src.MapStruct`."""

    MapID: int
    Region: int
    District: int
    Language: int


class SharedHealthLike(Protocol):
    """Structural view of `shared_memory_src.HealthStruct`."""

    Current: float


class SharedAgentLike(Protocol):
    """The parts of `shared_memory_src.AgentDataStruct` a precheck reads."""

    CharacterName: str

    @property
    def Map(self) -> SharedMapLike: ...

    @property
    def Health(self) -> SharedHealthLike: ...


class SharedPartyLike(Protocol):
    """Structural view of `shared_memory_src.AgentPartyStruct`."""

    PartyID: int


class SharedBagsLike(Protocol):
    """Structural view of `shared_memory_src.InventoryBagsStruct`."""

    def iter_bags(self) -> Iterable[SharedBagLike]: ...


class SharedAccountLike(Protocol):
    """The parts of `shared_memory_src.AccountStruct` a precheck reads."""

    AccountEmail: str
    IsolationGroupID: int
    IsIsolated: bool
    InAggro: bool

    @property
    def AgentData(self) -> SharedAgentLike: ...

    @property
    def AgentPartyData(self) -> SharedPartyLike: ...

    @property
    def InventoryBags(self) -> SharedBagsLike: ...


def participant_from_shared_account(account: SharedAccountLike, explorable: bool | None = None) -> ParticipantState:
    """Adapt one `AccountStruct` into the flat state the precheck reads.

    `explorable` stays a caller's answer on purpose: the struct carries a map id
    and no instance type, and the map-id-to-instance-type table lives in the
    client enums this module may not import.
    """
    return ParticipantState(
        key=str(account.AccountEmail or ""),
        name=str(account.AgentData.CharacterName or ""),
        map_id=int(account.AgentData.Map.MapID),
        region=int(account.AgentData.Map.Region),
        district=int(account.AgentData.Map.District),
        language=int(account.AgentData.Map.Language),
        party_id=int(account.AgentPartyData.PartyID),
        isolation_group=int(account.IsolationGroupID),
        isolated=bool(account.IsIsolated),
        in_aggro=bool(account.InAggro),
        health=float(account.AgentData.Health.Current),
        explorable=explorable,
        inventory=snapshot_from_shared_bags(account.InventoryBags.iter_bags()),
    )


def can_communicate(sender: ParticipantState, receiver: ParticipantState) -> bool:
    """Mirror of `AllAccounts._can_communicate`, for the precheck only.

    The transport stays the authority; this copy exists so a preview can fail
    early and legibly instead of watching a message vanish. It is deliberately
    identical in shape to the owner: self always, party members always, then
    isolation groups, then the ungrouped legacy rule. If the owner's rule
    changes this one is wrong and the precheck lies, so keep the two together.
    """
    if sender.key == receiver.key:
        return True
    if sender.party_id > 0 and sender.party_id == receiver.party_id:
        return True
    if sender.isolation_group > 0 and receiver.isolation_group > 0:
        return sender.isolation_group == receiver.isolation_group
    if sender.isolation_group > 0 or receiver.isolation_group > 0:
        return False
    return not sender.isolated and not receiver.isolated


def accounts_sharing_instance(
    receiver: ParticipantState, everyone: Iterable[ParticipantState]
) -> tuple[str, ...]:
    """Keys of every account in the receiver's instance, the receiver included.

    This is the set a transfer has to quieten, and it is deliberately wider than the
    set it talks to. A party member that is neither donor nor receiver still has
    HeroAI `Looting` armed, is standing in the same instance, and will happily take
    a pile it was never told about -- which is exactly what a live run showed.
    Selecting donors decides whose items move; it does not decide who can interfere.

    An account with no party is excluded even if its map matches: explorable
    instances are party-scoped, so it is not in the same copy of the map and cannot
    reach the pile.
    """
    wanted = receiver.instance_key
    if receiver.party_id <= 0:
        return ()

    keys: list[str] = []
    for account in everyone:
        if not account.key or account.party_id <= 0:
            continue
        if account.instance_key != wanted:
            continue
        if account.key not in keys:
            keys.append(account.key)
    return tuple(keys)


def _name_list(participants: Iterable[ParticipantState]) -> str:
    return ", ".join(participant.label for participant in participants)


def _check_roles(receiver: ParticipantState, donors: Sequence[ParticipantState]) -> PrecheckResult:
    if not receiver.key:
        return PrecheckResult(CHECK_ROLES, CHECK_FAIL, "no receiver selected")
    if not donors:
        return PrecheckResult(CHECK_ROLES, CHECK_FAIL, "no donor selected")

    if any(donor.key == receiver.key for donor in donors):
        return PrecheckResult(CHECK_ROLES, CHECK_FAIL, "the receiver is also selected as a donor")

    keys = [donor.key for donor in donors]
    if len(set(keys)) != len(keys):
        return PrecheckResult(CHECK_ROLES, CHECK_FAIL, "the same donor is selected twice")

    return PrecheckResult(CHECK_ROLES, CHECK_PASS, "%d donor(s) -> %s" % (len(donors), receiver.label))


def _check_explorable(participants: Sequence[ParticipantState]) -> PrecheckResult:
    refused = [one for one in participants if one.explorable is False]
    if refused:
        return PrecheckResult(CHECK_EXPLORABLE, CHECK_FAIL, "not explorable: %s" % _name_list(refused))

    unresolved = [one for one in participants if one.explorable is None]
    if unresolved:
        return PrecheckResult(CHECK_EXPLORABLE, CHECK_UNKNOWN, "cannot tell for: %s" % _name_list(unresolved))

    return PrecheckResult(CHECK_EXPLORABLE, CHECK_PASS)


def _check_same_instance(participants: Sequence[ParticipantState]) -> PrecheckResult:
    if not participants:
        return PrecheckResult(CHECK_SAME_INSTANCE, CHECK_FAIL, "nobody selected")

    partyless = [one for one in participants if one.party_id == 0]
    if partyless:
        return PrecheckResult(CHECK_SAME_INSTANCE, CHECK_FAIL, "not in a party: %s" % _name_list(partyless))

    expected = participants[0].instance_key
    strays = [one for one in participants[1:] if one.instance_key != expected]
    if strays:
        return PrecheckResult(
            CHECK_SAME_INSTANCE,
            CHECK_FAIL,
            "in a different instance or party: %s" % _name_list(strays),
        )

    return PrecheckResult(CHECK_SAME_INSTANCE, CHECK_PASS, "map %d, party %d" % (expected[0], expected[4]))


def _check_can_communicate(coordinator: ParticipantState, participants: Sequence[ParticipantState]) -> PrecheckResult:
    unreachable = [one for one in participants if not can_communicate(coordinator, one)]
    if unreachable:
        return PrecheckResult(CHECK_CAN_COMMUNICATE, CHECK_FAIL, "isolation blocks: %s" % _name_list(unreachable))
    return PrecheckResult(CHECK_CAN_COMMUNICATE, CHECK_PASS)


def _check_combat_free(participants: Sequence[ParticipantState]) -> PrecheckResult:
    fighting = [one for one in participants if one.in_aggro]
    if fighting:
        return PrecheckResult(CHECK_COMBAT_FREE, CHECK_FAIL, "in aggro: %s" % _name_list(fighting))

    dead = [one for one in participants if not one.alive]
    if dead:
        return PrecheckResult(CHECK_COMBAT_FREE, CHECK_FAIL, "dead or not reporting health: %s" % _name_list(dead))

    return PrecheckResult(CHECK_COMBAT_FREE, CHECK_PASS)


def _check_receiver_space(receiver: ParticipantState, policy: TransferPolicy) -> PrecheckResult:
    free = receiver.inventory.free_slots
    margin = max(int(policy.safety_margin), 0)
    if free <= 0:
        return PrecheckResult(CHECK_RECEIVER_SPACE, CHECK_FAIL, "%s has no free slots" % receiver.label)
    if free - margin <= 0:
        return PrecheckResult(
            CHECK_RECEIVER_SPACE,
            CHECK_FAIL,
            "%s has %d free slot(s), all held back by the safety margin" % (receiver.label, free),
        )
    return PrecheckResult(CHECK_RECEIVER_SPACE, CHECK_PASS, "%d usable free slot(s)" % (free - margin))


def precheck_session(
    coordinator: ParticipantState,
    receiver: ParticipantState,
    donors: Sequence[ParticipantState],
    policy: TransferPolicy,
    session_busy: bool = False,
) -> tuple[PrecheckResult, ...]:
    """Rule on a whole session, one named check at a time, in plan-section order.

    Every check is evaluated rather than short-circuited: a user fixing a
    transfer wants the whole list, not the first thing that went wrong.
    """
    participants = (receiver, *donors)

    return (
        _check_roles(receiver, donors),
        _check_explorable(participants),
        _check_same_instance(participants),
        _check_can_communicate(coordinator, participants),
        _check_combat_free(participants),
        _check_receiver_space(receiver, policy),
        PrecheckResult(CHECK_SESSION_FREE, CHECK_FAIL if session_busy else CHECK_PASS),
    )


def precheck_passed(results: Iterable[PrecheckResult]) -> bool:
    """True only when every check passed. An unresolved check is not a pass."""
    return all(result.passed for result in results)
