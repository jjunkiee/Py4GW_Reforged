# Cross-Account Inventory Transfer (Drop-and-Collect Ferry)

**Status:** Phases 1-3 of section 8 (planner, offline tests, yield helpers,
enum, message handlers) are implemented; phases 4-6 are still proposed. The
three commands now exist and can be driven by hand from the Messaging window's
send-message section, but no widget composes them into a session, so a transfer
is still a manual, one-round-at-a-time exercise with no precheck, no rally, no
reconciliation, and no settle warning.
Every game-behavior claim marked *inferred* below remains unverified against a
live injected client and must be confirmed before the corresponding code is
trusted. The planner encodes those assumptions as constants and defaults to the
conservative branch, so it is arithmetically proven and behaviorally unproven.

**Phase 1 landed:** `Py4GWCoreLib/py4gwcorelib_src/inventory_transfer.py` and
`Examples and tests/tests/test_inventory_transfer_planner.py` (96 checks, all
passing). Pyright reports zero errors on both files at the project
configuration and at `strict`. No live-client verification has been run, and
none of A1-A7 is confirmed.

**Phase 3 landed:** `TransferDropItems`, `TransferPickUpItems` and
`TransferReport` appended to `SharedCommandType`, their handlers and dispatch
cases in `Widgets/System/Messaging.py`, and the shared protocol vocabulary
(named policies, round status codes) in the phase 1 planner. Pyright at the
project configuration reports the same sixteen pre-existing errors on
`Messaging.py` as the committed file and no new diagnostic; the planner is
still clean at the project configuration and at `strict`; the offline fixture
now runs 113 checks, all passing. No live-client verification has been run, so
A1-A7 all remain unconfirmed and no message in this protocol has ever been
sent between two real clients.

**Phase 2 landed:** `Items.DropItems` and `Items.LootGroundItems` in
`Py4GWCoreLib/routines_src/yield_src/items.py`. Pyright at the project
configuration reports the same two pre-existing `UIManager` errors as the
unmodified file and no new diagnostics; the phase 1 planner test still passes.
Neither helper has been run against a live client, so A1, A2, A3, and A5 remain
unconfirmed.

**Topic owner:** item movement across multiboxed accounts.
**Related:** `docs/loot/plans/inventory-plus-to-system-settings.md`,
`docs/ui/widget-manager/widget-manager-and-catalog.md`,
`docs/architecture/records/whiteboard-architecture-cross-hero-cast-coordination.md`.

## 1. Problem

Consolidating inventory across several multiboxed accounts currently means
running a trade window per pair of characters. In an explorable area the game
allows a cheaper route: every donor drops its items on the ground at a rally
point, and one receiver walks the pile and picks everything up. One pass,
no trade UI, no per-item confirmation.

The automation has to be smarter than "drop everything":

- the receiver must never be handed more items than it has slots for;
- stackable items must move as whole stacks, and stack merging on the
  receiver must be accounted for when budgeting slots;
- nothing may be left orphaned on the ground when the instance closes.

## 2. Prior-art audit: does this already exist?

No. Searched `Widgets/`, `Bots/`, `Sources/`, and `Py4GWCoreLib/` for
transfer/mule/stash features and for every `DropItem` caller.

Existing `DropItem` callers and what they do:

| Caller | Purpose |
|---|---|
| `Py4GWCoreLib/Inventory.py:1271` | thin static wrapper over the binding |
| `Py4GWCoreLib/GlobalCache/InventoryCache.py:519` | queued-action wrapper |
| `Sources/frenkeyLib/ItemHandling/BTNodes.py:547` | private lib, drops a BT-selected list |
| `Sources/frenkeyLib/LootEx/inventory_handling.py:440` | private lib, single-item drop |
| `Py4GWCoreLib/routines_src/behaviourtrees_src/party.py:606` | `ControlAction_DropItem` keybind, for carried bundles (flags/ashes), not inventory |

Adjacent features that exist and are *not* this:

- `Widgets/Guild Wars/Items & Loot/TeamInventoryViewer.py` - read-only
  cross-account inventory browser. No actions.
- `Widgets/Guild Wars/Items & Loot/Xunlaimanager.py`, `InventoryPlus.py`,
  `MerchantRules.py` - single-account item handling, storage, and selling.
- `Py4GWCoreLib/HeroAI/commands.py:177` `PickUpLoot` - broadcast "loot the
  area", filtered by the loot profile, not by a transfer manifest.

Conclusion: this is a new feature, but almost every primitive it needs is
already owned somewhere. The plan below composes existing owners rather than
adding a parallel stack.

## 3. What gets reused (verified surfaces)

### 3.1 Native item actions

`stubs/PyInventory.pyi` is the exact counterpart of the native bindings:

```text
def PickUpItem(self, item_id: int, call_target: bool = False) -> None: ...
def DropItem(self, item_id: int, quantity: int = 1) -> None: ...
def DropGold(self, amount: int) -> None: ...
```

Every `PyInventory` action is queued on the game thread and returns `None`,
so all confirmation is by polling state, never by return value.

Python owners: `Py4GWCoreLib/Inventory.py:1260` (`PickUpItem`), `:1271`
(`DropItem`), `:1360` (`DropGold`), and the queued
`Py4GWCoreLib/GlobalCache/InventoryCache.py:519`.

### 3.2 Inventory accounting

- `Py4GWCoreLib/Inventory.py:59` `GetInventorySpace()` returns
  `(total_items, total_capacity)` over bags 1-4 only (Backpack, Belt Pouch,
  Bag1, Bag2). Equipment Pack (bag 5) and equipped items (bag 22) are out of
  scope by construction, which is exactly right for a transfer feature.
- `Py4GWCoreLib/Inventory.py:137` `GetFreeSlotCount()`.
- `Py4GWCoreLib/Item.py:414` `GetQuantity`, `:509` `IsStackable`,
  `:545` `IsTradable`, `:404` `IsCustomized`.

### 3.3 Cross-account inventory snapshot (the key enabler)

Every account already publishes its four regular bags into shared memory:

- `Py4GWCoreLib/GlobalCache/shared_memory_src/AccountStruct.py` -
  `("InventoryBags", InventoryBagsStruct)`.
- `InventoryBagsStruct` carries `Backpack`, `BeltPouch`, `Bag1`, `Bag2`.
- `InventoryBagStruct` carries `BagID`, `Size`, and
  `Slots[SHMEM_MAX_INVENTORY_BAG_SLOTS]`.
- `InventorySlotStruct` carries `BagID`, `Slot`, `ModelID`, `Quantity`.

So the coordinating client can compute every participant's free slots,
model counts, and partial stacks **without a single round trip**.

Two constraints come with it:

- `SHMEM_MAX_INVENTORY_BAG_SLOTS = 20`
  (`Py4GWCoreLib/GlobalCache/shared_memory_src/Globals.py:15`) - fine, the
  Backpack is the largest regular bag at 20.
- `SHMEM_PLAYER_INVENTORY_UPDATE_THROTTLE_MS = 1500` (`Globals.py:32`) - the
  snapshot can be up to 1.5 s stale. It is good enough to *plan* a round; it
  is never good enough to *commit* one. Each client re-checks locally before
  acting.

Note: `TeamInventoryViewer.py` maintains a second, independent cross-account
inventory snapshot through a shared `JsonFactory` document. Two parallel
sources of the same truth is a smell, but consolidating them is out of scope
here. The planner uses the shared-memory structs because they are fresher and
already typed. Flagged for a future cleanup, not for this change.

### 3.4 Cross-account command transport

`Py4GWCoreLib/GlobalCache/shared_memory_src/AllAccounts.py:928` `SendMessage`,
dispatched by `Widgets/System/Messaging.py:3528` `ProcessMessages()`.

Hard limits that shape the protocol:

- `SharedMessageStruct.Params` is `c_float * 4`. Integers are exact only to
  2^24; model ids, agent ids, and item ids are comfortably below that, and
  existing commands already pass agent ids this way
  (`Py4GWCoreLib/HeroAI/commands.py:288`).
- `SharedMessageStruct.ExtraData` is `4 x (c_wchar * 64)`, i.e. four short
  strings. **Item manifests do not fit.** Do not attempt to ship item lists
  as strings.
- The inbox is a **single global pool of 64 slots shared by every account and
  every subsystem** (`AllAccounts.py:51`,
  `Inbox: SharedMessageStruct * SHMEM_MAX_PLAYERS`). One message per item
  would exhaust it. The protocol must stay at O(participants) messages per
  round.
- `SendMessage` **deduplicates**: an identical
  `(sender, receiver, command, params, extra_data)` tuple that is still
  pending reuses the existing slot instead of queuing a second copy
  (`AllAccounts.py:953-985`). A round counter in `Params[0]` is therefore
  mandatory, or round 2 silently collapses into round 1.
- `_can_communicate` gates on isolation groups, but party membership is an
  explicit escape hatch: two accounts sharing a non-zero `PartyID` may always
  exchange coordination messages, whatever their isolation groups say
  (`AllAccounts.py:303-322`, verified in source). An account may also always
  message itself, which is what lets a coordinator that is also the receiver
  route its own round result through the same report path. Since this feature
  requires one party anyway, isolation groups are effectively a non-issue for
  it; the precheck in section 6.2 keeps the test only to fail early and
  legibly.

### 3.5 Coordination, safety, and movement primitives

- `Py4GWCoreLib/GlobalCache/WhiteboardLocks.py:616` `post_loot_lock` /
  `:658` `clear_loot_lock` - stops other bot accounts from stealing a ground
  item that the receiver has claimed. Already applied inside
  `Routines.Yield.Items.LootItems`
  (`Py4GWCoreLib/routines_src/yield_src/items.py:432-437`).
- `Widgets/System/Messaging.py:722` `SnapshotHeroAIOptions`, `:852`
  `DisableHeroAIOptions`, `:744` `RestoreHeroAISnapshot` - the established
  suspend/restore pattern, used by `PickUpLoot`
  (`Messaging.py:2118`, restore at `:2261`). Snapshot and restore are a
  strict stack: exactly one restore per snapshot.
- `SharedCommandType.DisableHeroAI` / `EnableHeroAI` handlers at
  `Messaging.py:2266` / `:2277` - session-scoped suspension.
- `SharedCommandType.PauseWidgets` / `ResumeWidgets` at `Messaging.py:2788` /
  `:2801` - optional, for suspending `AutoInventoryHandler`, LootEx, and
  similar item-touching widgets on donors.
- `SharedCommandType.PixelStack` (`commands.py:280`) - already moves other
  accounts to a coordinate. Reuse it as the rally step.
- `Routines.Yield.Movement.FollowPath([pos], timeout=...)` - the movement
  idiom used by `PickUpLoot`.
- `Routines.Yield.Items.LootItems(item_array, ...)`
  (`yield_src/items.py:412`) - takes an explicit list of ground item agent
  ids, posts loot locks, walks to each, interacts, waits for disappearance,
  and bails when `GetFreeSlotCount() <= 0`. This is the receiver's engine,
  unmodified.
- `Py4GWCoreLib/AgentArray.py:133` `GetItemArray()` - ground item agents.
  `Py4GWCoreLib/Agent.py:1565` `GetItemAgentOwnerID()` - `0` means unowned.
- Same-instance/party predicate: reuse the `on_same_map_and_party` idiom at
  `Py4GWCoreLib/HeroAI/commands.py:304`.

## 4. Game-behavior assumptions (must be verified live)

These carry the design. None is verified in this repository.

| # | Assumption | Why it matters | How to verify |
|---|---|---|---|
| A1 | `DropItem(item_id, quantity)` with `quantity == stack size` drops the whole stack as one ground item | "move whole stacks" requirement | Drop a 250 stack, count ground agents and the picked-up quantity |
| A2 | A player-dropped item reports `owner == 0` immediately and any party member may take it | receiver pickup and loot locks | `Agent.GetItemAgentOwnerID` on a freshly dropped item |
| A3 | Player-dropped items persist for the instance lifetime (no decay) | round-based batching is safe | Drop, wait 10 min, confirm the agent still exists |
| A4 | Stack cap is 250 for all stackables, and picking up a ground stack tops off partial stacks first, spilling the remainder into a free slot | slot budgeting | Receiver holds 200 iron, picks up a 250 stack, inspect resulting slots |
| A5 | Dropping is refused in outposts and towns | the explorable gate | Attempt a drop in an outpost, observe the failure mode |
| A6 | `is_tradable == False` is a sound proxy for "cannot be dropped" (quest items and similar) | protection filter | Test with a known quest item and a customized weapon |
| A7 | Dyes share `ModelID.Vial_Of_Dye = 146` but do not stack across colors | merge prediction | Two different dye colors in one inventory |

A4 and A7 are the two that can silently produce a wrong slot budget. Until
they are confirmed, the planner defaults to the conservative branch
(section 6.3).

Caution carried over from a known native defect: do **not** identify items by
decoding UI frame text. The decoded-text-label path has crashed the client
before. Identify items by `model_id` and by `Item.RequestName` /
`Item.GetName` only.

## 5. Architecture and ownership

Four layers, each with one owner, no new parallel stack.

```text
Widgets/Guild Wars/Items & Loot/InventoryTransfer.py     UI + session state machine
        |  reads ShMem InventoryBags, sends SharedCommandType messages
        v
Py4GWCoreLib/py4gwcorelib_src/inventory_transfer.py      pure planner (offline-testable)
        |
Widgets/System/Messaging.py                              new command handlers (donor/receiver)
        |
Py4GWCoreLib/routines_src/yield_src/items.py             new Items.DropItems / Items.LootGroundItems
        |
Py4GWCoreLib/Inventory.py -> PyInventory bindings        native actions
```

Why the planner is a separate, dependency-light module: it must be unit
testable without an injected client. Precedent:
`Examples and tests/tests/test_salvage_upgrade_dialog.py`, added in commit
`6366d4c3`, runs offline with `python "<path>"` and exits 1 on failure.

## 6. The transfer algorithm

### 6.1 Roles

- **Coordinator** - the client running the widget. Usually the receiver's
  client, but it does not have to be. It owns the session state machine.
- **Receiver** - exactly one account, the destination.
- **Donors** - one or more accounts giving items away.

### 6.2 Session shape

```text
PRECHECK -> SUSPEND -> RALLY -> [ ROUND: PLAN -> DROP -> COLLECT -> RECONCILE ]* -> SETTLE -> RESUME
```

`PRECHECK` (all must pass, all reported individually in the UI):

1. Every participant is in an explorable area
   (`Py4GWCoreLib/Map.py:80` `IsExplorable`, or
   `routines_src/Checks.py:370`).
2. Every participant shares one map id / region / district / language **and**
   one `PartyID` - the `on_same_map_and_party` predicate at
   `commands.py:304`. Explorable instances are party-scoped, so this is the
   real "same instance" test.
3. Every participant shares an isolation group, or is in the same party,
   which `_can_communicate` accepts as equivalent (section 3.4).
4. Nobody is `InAggro` (`AccountStruct.InAggro`) and nobody is dead.
5. Receiver free slots > 0.
6. No other transfer session is running - a module-level busy flag, following
   the `_merchant_busy` precedent at `Widgets/System/Messaging.py:53`.

`SUSPEND`: coordinator sends `DisableHeroAI` to every participant, held for
the whole session rather than per round. Per-round suspension has a real gap:
between rounds a donor's `Looting` comes back on and it re-collects its own
drop. Optionally also `PauseWidgets` on donors to keep
`AutoInventoryHandler` (`Py4GWCoreLib/py4gwcorelib_src/AutoInventoryHandler.py`)
and LootEx from salvaging or depositing items mid-flight.

**Unresolved, and phase 5 has to answer it.** A session-scoped suspension is
not actually safe today. `HealStaleHeroAISnapshot` runs every frame from
`Messaging.main()` and restores any outstanding snapshot as soon as the account
has no *active* HeroAI-suspending message and all five options read disabled
(`Messaging.py:822-848`). `DisableHeroAI` finishes its message immediately, so
a suspension held across rounds is exactly the state that heal treats as stale
and unwinds. Phase 3 registered `TransferDropItems` and `TransferPickUpItems`
in `_HERO_AI_SUSPENDING_COMMANDS`, which protects the window while a round is
in flight and makes each handler self-sufficient - it snapshots and restores
around its own step, and that nests correctly inside a session suspension
because the snapshot stack is strict. It does **not** protect the gap between
rounds. Phase 5 either re-sends `DisableHeroAI` at the head of each round,
keeps a suspending message active for the session's duration, or teaches heal
about an owned session. Do not assume the current suspend survives a round
boundary.

`RALLY`: coordinator sends `PixelStack` with the receiver's coordinates so
donors cluster, then each donor drops at the rally point. A tight pile means
the receiver barely moves and pickup stays fast.

### 6.3 Slot budgeting and stack prediction (the core logic)

Pure function, no runtime dependencies, operating on plain slot records taken
from `InventoryBagsStruct` or from a local bag read.

```python
STACK_MAX = 250                     # A4
NON_MERGING_MODELS = {146}          # Vial_Of_Dye; A7

def predict_slot_cost(receiver_slots, model_id, quantity, stackable, optimistic):
    """Slots the receiver must have free to absorb one dropped ground stack."""
    if not stackable or model_id in NON_MERGING_MODELS or not optimistic:
        return 1
    remaining = quantity
    for slot in receiver_slots:
        if slot.model_id != model_id or slot.quantity >= STACK_MAX:
            continue
        remaining -= (STACK_MAX - slot.quantity)
        if remaining <= 0:
            return 0
    return ceil(remaining / STACK_MAX)
```

`optimistic` is a user setting, default **off**. With it off every ground
stack reserves one slot: never wrong, occasionally one round slower. With it
on the planner exploits partial stacks on the receiver and moves more per
round. Getting it wrong is not destructive - the surplus simply stays on the
ground for the next round (A3) - but the conservative default keeps the
common path boring.

Round planning:

```text
budget = receiver_free_slots - safety_margin        # safety_margin default 1
plan = []
for donor in donors_in_priority_order:
    for item in donor_eligible_items_in_deterministic_order:
        cost = predict_slot_cost(projected_receiver_slots, item.model_id,
                                 item.quantity, item.stackable, optimistic)
        if cost > budget:
            continue          # try the next item; a 0-cost merge may still fit
        budget -= cost
        apply_to_projection(projected_receiver_slots, item)
        plan.append((donor, item))
        if budget <= 0:
            break
```

`safety_margin` defaults to 1 so a mispredicted merge cannot wedge the round.

One deliberate difference in the implementation: the pseudocode's
`if budget <= 0: break` is omitted. The `cost > budget` test already blocks
everything that costs a slot while still admitting a merge that costs none, so
breaking early would strand free items for another round. With `optimistic`
off no cost is ever zero and the two forms are identical.

### 6.4 Who selects the items

The coordinator owns the **budget**; the donor owns the **selection**.

The coordinator cannot address items by id - the shared snapshot carries
`ModelID` and `Quantity` but no `item_id`, and `ExtraData` is far too small
for a manifest. So a drop command carries a *count budget plus a policy*, and
the donor resolves the actual `item_id`s locally:

- donor iterates bags 1 -> 4, slot 0 -> n (deterministic, matching the
  coordinator's projection order);
- applies the shared eligibility filter (section 6.6);
- drops at most `N` items, each with its **full** quantity
  (`DropItem(item_id, Item.Properties.GetQuantity(item_id))` - A1);
- reports back how many it actually dropped.

Snapshot staleness (1.5 s) can make the coordinator's projection differ from
what the donor really holds. That is tolerable because the design is
self-correcting: the donor never drops more than `N`, the receiver never
picks up more than its live free-slot count
(`LootItems` bails at `yield_src/items.py:437`), and anything left over stays
on the ground for the next round.

### 6.5 Message protocol

Three new members appended to the **end** of `SharedCommandType` in
`Py4GWCoreLib/enums_src/Multiboxing_enums.py`. That file carries explicit
append-only warnings ("persisted/shared enum values must never shift"); honor
them.

| Command | Direction | Params | ExtraData |
|---|---|---|---|
| `TransferDropItems` | coordinator -> donor | `(round_id, max_items, rally_x, rally_y)` | `(policy_name, session_id, "", "")` |
| `TransferPickUpItems` | coordinator -> receiver | `(round_id, max_items, radius, 0)` | `(session_id, "", "", "")` |
| `TransferReport` | participant -> coordinator | `(round_id, items_moved, slots_used, status_code)` | `(session_id, reporter_email, "", "")` |

`round_id` in `Params[0]` defeats the `SendMessage` dedup described in
section 3.4. `session_id` in `ExtraData` keeps two overlapping sessions from
reconciling each other's reports; it is a short token, well inside the
63-character field.

Two refinements the implementation made to this table, both additive:

- **The receiver reports too.** The plan originally had only donors reporting.
  But the receiver's report is the one edge that says a round is *over*;
  without it the coordinator has to infer completion from a shared-memory
  snapshot that is up to 1.5 s stale. It reuses `TransferReport` rather than
  adding a command, so `Params[1]` is "items this participant moved" - dropped
  by a donor, collected by the receiver. Message cost per round becomes
  `2N + 2`, still `O(participants)`.
- **`status_code` is a named vocabulary**, not an ad-hoc integer.
  `Py4GWCoreLib/py4gwcorelib_src/inventory_transfer.py` owns it, because both
  the coordinator and the participant must read the same one and that module is
  the only one both sides already import: `STATUS_OK`, `STATUS_BUSY`,
  `STATUS_NOT_EXPLORABLE`, `STATUS_NOTHING_ELIGIBLE`, `STATUS_RALLY_FAILED`,
  `STATUS_ERROR`, `STATUS_NO_BUDGET`, `STATUS_INVENTORY_FULL`, with
  `status_name()` for the UI. They are small integers so the `c_float` round
  trip is exact, and `STATUS_OK` is `0` so a plain truth test cannot invert it.

`policy_name` resolves through `inventory_transfer.resolve_policy()`, which
falls back to the conservative `default` policy for an unknown or empty name.
That fallback direction matters: a donor on an older build than the coordinator
drops *fewer* items than budgeted, never more. Built-in names are `default`,
`stackables` and `optimistic`; a widget passes user-defined policies through the
`extra` argument rather than mutating a global.

Message cost per round: `N` drops + 1 pickup + `N + 1` reports = `2N + 2`. With
8 accounts that is 18 of the 64 global inbox slots, and only while a round is
in flight.

Handler shape follows the existing convention exactly - a generator appended
to `GLOBAL_CACHE.Coroutines` by `ProcessMessages()`, `MarkMessageAsRunning`
at entry, and `MarkMessageAsFinished` in a `finally` so a raised exception can
never wedge the slot. `PickUpLoot` (`Messaging.py:2118-2262`) is the model to
copy, including its comment explaining why the whole body lives inside the
`try`. `TransferReport` is the one exception: it has nothing to wait for, so it
is a plain function called straight from `ProcessMessages()`, following
`AccountSettingsSyncResult`.

Each participant's report is sent from that same `finally`, so a handler that
dies mid-round still answers and the coordinator never waits out a round that
is already over. A module-level `_transfer_busy` flag serialises transfer steps
on one client - `ProcessMessages()` dispatches a fresh coroutine every frame,
and two overlapping sessions would otherwise have two coroutines walking and
dropping at once. A refused step reports `STATUS_BUSY` rather than queueing.

### 6.6 Eligibility filter (never drop these)

Evaluated on the donor, from a policy the coordinator names by string:

- not tradable - `Item.Properties.IsTradable` (A6);
- customized - `Item.Properties.Rarity.IsCustomized`, useless to the
  receiver; skipped by default, overridable;
- the last ID kit and the last salvage kit, so a donor is not stranded
  mid-farm (`Inventory.GetFirstIDKit` / `GetFirstSalvageKit`);
- a user-maintained protected-model list, plus per-model quantity floors
  ("keep 5 Cupcakes");
- optional inclusion filters: stackables only, materials only, rarity >= X,
  explicit model whitelist.

Bags 5 (Equipment Pack) and 22 (equipped) are never read, so equipped gear is
structurally out of reach rather than filtered out.

### 6.7 Reconcile and settle

After each round the coordinator:

- re-reads free slots and model counts for receiver and donors;
- counts unowned ground items still within the rally radius;
- stops when donors have nothing eligible left, or the receiver has no usable
  free slots, or a round moved zero items (stall guard, hard stop after 2).

`SETTLE` is the part that matters most. If items remain on the ground the
widget must **not** report success. It shows a blocking warning with a
**Recall** action that sends `TransferPickUpItems` to the original donors so
their items go home rather than dying with the instance. Only after the
ground is clear, or the user explicitly dismisses, does the session send
`EnableHeroAI` (exactly one restore per suspend) and clear the busy flag.

## 7. Files to add or change

| File | Change |
|---|---|
| `Py4GWCoreLib/enums_src/Multiboxing_enums.py` | append `TransferDropItems`, `TransferPickUpItems`, `TransferReport` |
| `Py4GWCoreLib/py4gwcorelib_src/inventory_transfer.py` | **new** - pure planner: eligibility, `predict_slot_cost`, round decomposition, reconciliation. No `Py4GW` imports |
| `Py4GWCoreLib/routines_src/yield_src/items.py` | add `Items.DropItems(item_ids)` and `Items.LootGroundItems(radius, max_items)`; the latter builds an unowned-item array and delegates to the existing `LootItems` |
| `Widgets/System/Messaging.py` | add the three handlers and their `ProcessMessages()` cases |
| `Widgets/Guild Wars/Items & Loot/InventoryTransfer.py` | **new** widget: account selection, receiver picker, policy editor, preview, run/abort/recall, progress |
| `Examples and tests/tests/test_inventory_transfer_planner.py` | **new** offline planner tests |
| `docs/loot/plans/cross-account-inventory-transfer.md` | this document |
| `docs/loot/plans/README.md` | index entry |

The widget folder `Widgets/Guild Wars/Items & Loot/` already has its
`.widget` marker, so no discovery change is needed. `MODULE_CATEGORY` and
`MODULE_TAGS` derive from the path; `OPTIONAL` defaults to `True` outside
`System`/`Py4GW` (`docs/ui/widget-manager/widget-manager-and-catalog.md`).

## 8. Implementation order

Each phase is independently reviewable and leaves the tree buildable.

1. **Planner + tests.** *Done.* `inventory_transfer.py` and its offline test.
   No runtime dependency, no client needed. Locks the slot/stack math down
   first, because that is where a silent bug costs the user real items.
   Eligibility came out three-valued rather than boolean: the coordinator
   budgets from a snapshot that cannot see tradability, so an unresolved flag
   reports `unverified` -- plannable, never droppable -- and only a donor's
   live read can produce the `allowed` verdict a real `DropItem` requires.
   The module also owns the shared-memory-to-record adapter, so the widget
   does not grow a second conversion.
2. **Yield helpers.** *Implemented, live verification outstanding.*
   `Items.DropItems` drops whole stacks where the character stands and confirms
   each one by polling, because the queued native action cannot report success;
   it returns the ids observed leaving the bags so a donor can report a real
   count rather than an intention. It owns no eligibility opinion, exactly like
   `LootItems`, and refuses to run outside an explorable area (A5).
   `Items.LootGroundItems` builds the nearest-first candidate array -- unowned
   items, plus items still reserved for this character, which is what Recall
   needs -- and delegates to `LootItems` for locks, movement, and the free-slot
   bail-out. Still to do: verify by hand on a single account with a throwaway
   item.
3. **Enum + handlers.** *Implemented, live verification outstanding.* The
   three commands are appended to `SharedCommandType`; the handlers live in
   `Widgets/System/Messaging.py` in the `PickUpLoot` suspend/restore/finally
   shape, each reporting from its `finally` so a dead round still answers.
   The donor resolves item ids from a live bag read
   (`_read_local_transfer_items`, which fills every tri-state flag because only
   a live read can) and drops through `Items.DropItems`; the receiver counts
   the pile before and after and collects through `Items.LootGroundItems`. The
   coordinator side files reports into a `GLOBAL_CACHE`-parked cache, readable
   with `get_transfer_reports` / `clear_transfer_reports`, which is the seam
   phase 4 plugs into. The new commands appear automatically in the Messaging
   window's send-message section (`_MESSAGE_TYPE_OPTIONS` enumerates
   `SharedCommandType`), so the two-account hand-driven check needs no UI work.
   Still to do: run that check, in an explorable area, with a throwaway item.
4. **Widget, dry-run only.** Account selection, preview of the computed plan,
   no packets sent. This is the review gate: the plan must read correctly
   before it is allowed to move anything.
5. **Widget, live.** Run, abort, recall, progress, settle warnings.
6. **Optional extras.** Gold transfer, `PauseWidgets` integration, chained
   receivers.

## 9. Verification

**Offline (must pass before any live run):**

```text
python "Examples and tests/tests/test_inventory_transfer_planner.py"
```

Cases: empty donor; single non-stackable; full-stack move; partial-stack merge
(optimistic on and off); dye non-merge; receiver at zero free slots; receiver
at exactly one free slot with a 250 stack pending; protected model excluded;
last-kit protection; multi-round decomposition; stall detection.

**Static:** the project's strict Pyright/Pylance check over every changed
file. Typing failures are defects, per `AGENTS.md`. `Messaging.py` carries
sixteen pre-existing errors at the project configuration; the bar for a change
to it is that the count and the list are unchanged, checked against
`git show HEAD:Widgets/System/Messaging.py`, not that the file is clean.

**Hand-driven protocol check (phase 3, two accounts, no widget):** send the
messages from the Messaging window's send-message section. The commands appear
in its dropdown on their own.

1. Both accounts in the same explorable area and the same party. Donor holds
   one throwaway tradable item; receiver has free slots.
2. Coordinator -> donor, `TransferDropItems`, params
   `(1, 1, 0, 0)`, extra `("default", "smoke", "", "")`. Rally coordinates of
   `0, 0` mean "drop where you stand", which keeps the first check to one
   moving part.
3. Expect one item on the ground and one `TransferReport` back with
   `items_moved = 1`, `status = 0`. The coordinator's console logs the report;
   `get_transfer_reports("smoke")` is what a widget would read.
4. Coordinator -> receiver, `TransferPickUpItems`, params `(1, 0, 1012, 0)` (1012 is `Range.Earshot`, and `0` means Earshot too),
   extra `("smoke", "", "", "")`. Expect the item collected and a second
   report from the receiver.
5. Repeat step 2 with `round_id = 2` to prove the dedup escape works: an
   identical round 1 message would silently reuse the pending slot instead of
   queueing a second drop.
6. Repeat step 2 in an outpost. Expect `status = 2` (not explorable) and
   nothing dropped - that is the A5 gate, reported rather than attempted.

Confirms the protocol, the report path, the busy flag and the dedup escape. It
confirms none of A1-A7 beyond A5's failure mode; those need the live runs
below.

**Live, in this order, each confirming the assumptions it depends on:**

1. Two accounts, one non-stackable item, empty receiver. Confirms A1, A2, A5.
2. Same, with the receiver holding a partial stack. Confirms A4.
3. Receiver with exactly two free slots and a donor holding ten items.
   Confirms the budget, the leftover warning, and Recall (A3).
4. Two different dye colors. Confirms A7.
5. A quest item and a customized weapon in a donor bag. Confirms A6.
6. Four or more donors. Confirms round message cost and dedup handling.

Record which assumptions were confirmed, with the observed values, in this
document's status section. Do not mark a check passed that was not run.

## 10. Nice-to-haves, ranked

Worth building:

1. **Preview / dry run** - compute and display the full plan without sending
   a packet. Cheap, and the natural review gate. Precedent: MerchantRules'
   "Preview Plan".
2. **Abort and Recall** - the difference between a bad round and lost items.
3. **Protected models and kit protection** - one shared policy, edited once.
4. **Progress and summary UI** - per-donor status, items moved, slots left,
   round counter, explicit leftover list.
5. **Auto-rally via `PixelStack`** - pure reuse of an existing command.
6. **Gold transfer** - `DropGold`, capped so the receiver stays under the
   100,000 character limit. Small, self-contained, genuinely useful.

Deliberately deferred:

7. **Chained receivers** (fill account A, overflow to B) - useful, but it
   doubles the state machine. Revisit after the single-receiver path is
   proven.

Deliberately rejected, with reasons:

- **"Deposit to Xunlai between rounds to make room"** - storage is not
  reachable from an explorable area, and the transfer only works in an
  explorable area. The two requirements are mutually exclusive.
- **Trade-window fallback** - automating the trade UI is exactly the cost this
  feature exists to avoid.
- **Cross-map orchestration** - already covered by
  `SharedCommandType.TravelToMap` and the existing
  `Travel Alts to Leader's Map` command. Getting everyone into one instance is
  a separate, solved problem; this feature should precheck it, not reimplement
  it.
- **Per-item checkboxes across every account** - a large UI for what
  model-level protection lists already express.

## 11. Open questions for the maintainer

1. Should the coordinator be pinned to the receiver's client, or may any
   account drive a transfer between two others? The plan supports either; the
   UI is simpler if pinned.
2. Is `PauseWidgets` on donors acceptable as a default, or should it be
   opt-in? It is the reliable way to keep `AutoInventoryHandler` from
   salvaging an item that is queued to be dropped, but it is a broad hammer.
3. Should the leftover-on-ground state block the widget from being disabled,
   or only warn? Blocking is safer and more annoying.
