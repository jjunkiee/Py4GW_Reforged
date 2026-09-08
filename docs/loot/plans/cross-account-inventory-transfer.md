# Cross-Account Inventory Transfer (Drop-and-Collect Ferry)

**Status:** Phases 1-5 of section 8 (planner, offline tests, yield helpers,
enum, message handlers, widget preview, and the live run) are implemented; phase
6 is still proposed. The widget now composes the whole session, shows it, and on
an explicit Run performs it round by round with abort, per-round progress, a
leftover warning and Recall. The first live run of the phase 5 machine, on
2026-09-07, **found the suspension defect described in section 6.8**: the whole
party fought over the drops, because a per-message suspension ends while the
receiver is still walking and because bystanders were never suspended at all.
The first fix for that -- a leased, instance-wide `TransferHoldHeroAI` held open
by a long-running handler -- was itself defeated by the transport, and the second
live run exposed why: **`SendMessage` silently drops any message to an account
that already has a running message from the same sender**, for a full 60 seconds.
Section 6.9 records that constraint, which applies to every multibox feature in
this repository and is the more valuable finding. The hold is now a per-frame
sweeper, and both round handlers finish their message before reporting. None of
that has been run live yet.

**The stack defect is now bounded rather than open.** A1 is disproved: the
native binding drops one unit per call. Rather than pretend otherwise, the
planner refuses any stack over `TransferPolicy.max_stack_quantity`, which
defaults to **1** -- unstacked items only -- and a donor refuses an oversized
stack *whole and untouched* rather than starting one it cannot finish. Raising
the limit makes a stack move by repeated single drops, at one drop call, one
ground pile and one pickup walk per unit. See section 4.1.

**Phase 5 landed:** the live session in
`Widgets/Guild Wars/Items & Loot/InventoryTransfer.py`, plus the stack limit and
the round-report vocabulary it needed in the phase 1 planner. The widget grew a
Run panel, a `update()` callback that ticks the session while the window is
collapsed, per-round progress, Abort, Recall, a dismissable leftover warning, and
a manual "resume donor widgets" escape hatch. The offline fixture now runs 244
checks, all passing. Pyright: planner and fixture clean at the project
configuration and at `strict`; the widget clean at the project configuration and
carrying five `strict` diagnostics, all upstream `Py4GWCoreLib` typing gaps in
surfaces it merely calls (`ConsoleLog`, `Color.to_tuple_normalized`,
`ImGui.Begin`, and now `ShMem.SendMessage`, whose `params` is a bare `tuple`);
`Messaging.py` reports the same sixteen pre-existing errors as the committed
file and no new diagnostic.

Four decisions phase 5 made that the plan did not anticipate, all recorded in
section 6.8 because each reverses or refines something written earlier:

1. **HeroAI is held for the whole session on every account in the instance**, by
   a new leased `TransferHoldHeroAI` / `TransferReleaseHeroAI` pair. Section
   6.2's proposed answer does not survive the strict-stack semantics of
   `hero_ai_snapshots`, and the first replacement did not survive a live run.
2. **The stall guard reads the receiver's report, not its free-slot delta**, so
   a round that merged everything into existing stacks is not mistaken for a
   round that moved nothing.
3. **The round report store moved into the planner module**, because the widget
   and the message handler both need it and widgets cannot import each other.
4. **The pickup budget counts ground piles, not items**, which is what a stack
   dropped one unit at a time actually produces.

**Phase 1 landed:** `Py4GWCoreLib/py4gwcorelib_src/inventory_transfer.py` and
`Examples and tests/tests/test_inventory_transfer_planner.py` (96 checks, all
passing). Pyright reports zero errors on both files at the project
configuration and at `strict`. No live-client verification has been run, and
none of A1-A7 is confirmed.

**Phase 4 found a live defect on first run:** the shared-memory bag `Size` is
an item count, not a capacity (section 3.3). It was invisible to every offline
check because the fixture encoded the same wrong assumption the reader did. The
widget drew four accounts, one of them reported 5 free slots where the character
plainly had 26, and the arithmetic fell out from there. That is the review gate
earning its place in the plan.

**Phase 4 landed:** `Widgets/Guild Wars/Items & Loot/InventoryTransfer.py`, a
dry-run-only widget, plus the precheck vocabulary, the shared-account adapter
and the policy-text parsers it needed in the phase 1 planner. It reads every
account out of shared memory, lets the user pick one receiver and any number of
donors, names a wire policy, and then shows what a live run would do: the seven
prechecks reported one by one, the forecast rounds with per-item slot costs, the
literal `TransferDropItems` and `TransferPickUpItems` payloads round 1 would
carry, what stays in donor bags, and what policy excluded. There is no
`SendMessage` call in the file; nothing crosses the wire.

Two things the review gate exposed and the widget now says out loud. First, a
drop message names a policy rather than carrying one, so coordinator-side
protections and quantity floors change the preview's budget but are not what a
donor enforces -- see open question 4. Second, the explorable check cannot be
answered from a snapshot for a peer, only inferred from its map id, so it
reports `unknown` rather than passing when the map is in neither enum table.

Pyright reports zero errors on the planner, the fixture and the widget at the
project configuration; the planner and the fixture are also clean at `strict`.
The widget carries four `strict` diagnostics, all of them upstream typing gaps
in `Py4GWCoreLib` (`Color.to_tuple_normalized`, `ConsoleLog`, `ImGui.Begin`) and
none of them fixable from the widget. The offline fixture now runs 172 checks,
all passing -- 181 after the bag-`Size` regression tests were added. The widget
has been drawn by an injected client with four accounts in one party, which is
what exposed the `Size` defect; its Settings binding, its precheck output and
its preview tables are still unverified, and A1-A7 all remain unconfirmed.

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
- **`InventoryBagStruct.Size` is not a capacity.** *Verified on a live client,
  2026-09-03, and the first defect the phase 4 review gate caught.* A character
  holding 19 items across 45 slots published a total `Size` of 19. The publisher
  (`AccountStruct._update_inventory_bags`) fills it from
  `ItemArray.GetBag(bag_id).GetSize()`; `Inventory.GetInventorySpace` calls
  `GetSize()` on a `PyInventory.Bag` it constructs directly and gets a real
  capacity, so the two paths disagree and the shared-memory one is wrong. Note
  also that `ItemArray.GetBag` returns `None` for a bag with no items, so a
  completely empty bag contributes no capacity at all.

  Two consequences, both handled in `snapshot_from_shared_bags`. Bounding the
  slot read by `Size` silently *dropped items* that sat past the item count, so
  the read now walks every published slot and treats a zero `ModelID` as the
  only empty marker. And the capacity it reports is floored at the highest
  occupied slot, so it can never claim less space than the bag demonstrably
  holds.

  This does not compromise the round budget, because of the answer to question 1
  in section 11: the receiver is always the local client, and only the
  receiver's free slots set the budget. The widget therefore corrects its own
  row from `Inventory.GetInventorySpace()` through
  `InventorySnapshot.with_capacity()` and does not report a free-slot count for
  peers at all, rather than reporting a wrong one. A donor's capacity is never
  used by the planner - only its item list is.

  Fixing the publisher is the real repair and belongs to
  `AccountStruct`/`ItemArray`, not to this feature. Filed here because anything
  else reading `InventoryBags` is currently reading a wrong capacity too.

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

A4 and A7 are the two that can silently produce a wrong slot budget. Both are
now confirmed (below), but the planner still defaults to the conservative
branch (section 6.3) because `optimistic` remains opt-in.

**Live results, 2026-09-03**, from the hand-driven runs in section 9. Two
accounts in one party in an explorable area, messages sent by hand from the
Messaging window.

| # | Result | Observed |
|---|---|---|
| A1 | **Disproved for our call path** | The *game* drops a whole stack as one ground item: a stack of 10 dragged out by hand produced one pile, and one `TransferPickUpItems` collected all 10. Our call path does not reach that behavior -- `Inventory.DropItem(item_id, quantity)` dropped a single item with `quantity = 10`, logging `Item 4 dropped partially, 9 still in the bags`. Unresolved; see below. |
| A2 | **Confirmed** | Every donor drop was collectable by the receiver, and C3.3 confirmed a donor can retake its own drop, which is what Recall needs. |
| A3 | **Confirmed, and stronger than assumed** | Player drops do not despawn at all. Only mob drops reserved for a player become unassigned after ten minutes. Asserted by the maintainer from game knowledge rather than measured by waiting; the ten-minute run was skipped as unnecessary. |
| A4 | **Confirmed** | A donor stack merged into the receiver's partial stack of the same model on pickup, and a 245 + 10 case spilled correctly to 250 + 5 across two slots. |
| A5 | **Confirmed** | A drop attempted in an outpost dropped nothing and reported `STATUS_NOT_EXPLORABLE`. The refusal is reported, not attempted. |
| A6 | **Disproved for quest items** | The customized weapon, the last ID kit and the last salvage kit were all correctly held back. The quest item was **dropped**: quest items report `is_tradable == True` and drop without complaint, so the tradability proxy never sees them. Fixed by a separate rule; see below. |
| A7 | **Confirmed** | Dyes stack with the same color and refuse to merge across colors, exactly as the planner's non-merge branch assumes. |

Three defects the live runs exposed, all fixed except A1:

- **`item.slot` coerced through `or -1`** in both the donor's live read
  (`Messaging.py`) and the shared-memory publisher (`AccountStruct.py`). Slot 0
  is a real slot and zero is falsy, so the first item of every bag was
  discarded on both sides. They were wrong identically, so the widget preview
  agreed with the drop and nothing looked inconsistent. Fixed.
- **`Items.LootItems` waited for an item to leave the ground in an unbounded
  loop.** A pickup into a full inventory never terminated, so the calling
  coroutine never returned, its `finally` never ran, and the receiver was left
  with HeroAI suspended and `_transfer_busy` stuck. Observed directly in C3.2.
  The wait is now bounded by `pickup_timeout`, and `LootGroundItems` uses
  `LootItemsWithMaxAttempts` so a round can report what it failed to collect.
  The first fix traded a hang for a retry storm: the bounded version tries every
  remaining item `max_attempts` times before returning, so a full receiver spent
  minutes failing item by item with HeroAI still suspended. `LootGroundItems`
  now passes `stop_on_failure=True`, which is new and defaults off so the six
  farming callers keep skipping and continuing. For a pile at a single point the
  first refusal is decisive: the cause is a full inventory, not a bad item.
- **`GetFreeSlotCount()` deltas are not trustworthy.** A round that dropped one
  item reported `freeing 32 slot(s)`. The drop report now counts slots freed
  from the items that left the bags (drops are whole stacks, so it is exactly
  one each), and the pickup report clamps its delta to the number of items it
  actually collected.

**A1 is a native defect, and the packet capture shows how to route around it.** The game can
drop a whole stack -- dragging one out by hand produces a single ground pile
that one `TransferPickUpItems` collects whole. No Python binding can reach that
behavior. Verified live 2026-09-03 with `Examples and tests/drop_stack_diagnostic.py`,
which drops the same stack through each candidate in turn:

- `GLOBAL_CACHE.Inventory.DropItem` -> action queue ->
  `PyInventory.Inventory.DropItem(item_id, quantity)`: drops one item.
- `PyItem.drop_item_by_id(item_id, quantity)`, called directly with no queue in
  the way: drops one item.

Both Python entry points converge on one native function, which is why they
behaved identically -- they were never two experiments. Reading
`Py4GW_Reforged_Native` (2026-09-04) shows the C++ is a clean pass-through:
`PyInventory::DropItem` and `drop_item_by_id` hand the quantity to
`GW::item::DropItem`, which calls `g_drop_item_func(item->item_id, quantity)`
with no clamping -- unlike `MoveItem` beside it, which clamps to the live stack
size. The pattern (`Ä@j j` at `-0x4E`) is marked
parity in that repo's `pattern_parity_audit.md`.

**The CToS capture settles what reading could not.** Both a manual drag and the
binding emit the same packet, so the game's own drop path is reachable and the
quantity word in it is honored:

| Drop | header | word 1 | word 2 |
|---|---|---|---|
| Manual drag of a stack of 22 | 44 `DROP_ITEM`, size 12 | item id | **22** |
| `Inventory.DropItem(item, 21)` | 44 `DROP_ITEM`, size 12 | item id | **1** |

The game honors the quantity; `g_drop_item_func` never carries it. Whatever the
pattern resolves to, it is not the two-argument function the typedef declares,
so the second argument is dropped before the packet is built. That is a defect
in `Py4GW_Reforged_Native` affecting every consumer of `GW::item::DropItem`, and
it should be fixed there.

**The obvious workaround does not currently work either.** `PyCtoS.SendPacket`
takes the packet as dwords, so `[44, item_id, quantity]` should reproduce the
drag exactly. Sent live 2026-09-04 with a stack of 22: the call returned `True`
and nothing was dropped. `True` is not a send -- `PyCtoS.SendPacket` is
`GW::CToS::QueuePacket`, which validates only the word count and then enqueues.
The real send runs later on the game thread, behind four gates that return
`false` silently into a lambda that discards the result:

- `IsSendable()` -- map loaded, not observing, not in a loading screen.
- `ReadConnection` -- dereferences the resolved game server object.
- `IsConnectionReady` -- hardcoded `connection + 0x60 == 2` and `+ 0x38 != 0`.
- `IsValidSendHeader` -- walks `connection + 0x8` to a channel, then a message
  count at `+ 0x24` and a format table at `+ 0x1C`.

`Py4GW_injection_log.txt` confirms `[ctos] CToS sender initialized.`, so the
send target and game server object both resolved and the module is live. That
leaves the three state checks, whose struct offsets are hardcoded rather than
pattern-resolved. Nothing else in `Py4GW_Reforged` imports `PyCtoS` -- this
diagnostic is its only consumer -- so the sender has never been exercised and a
stale offset here would have gone unnoticed.

Next step is to localise it, which the packet sniffer can do without a rebuild:
send the CToS packet and look for a header 44 line. A captured line means the
packet reached the game's send routine and the server ignored it; no line means
CToS rejected it first, and the fix is native either way.

Two native defects are now on the table for `Py4GW_Reforged_Native`, both found
by this feature and neither owned by it:

1. `g_drop_item_func` does not carry its quantity argument, so
   `GW::item::DropItem` is broken for every consumer.
2. `GW::CToS::SendPacket` discards its own failure result inside
   `QueuePacket`'s lambda, so a rejected packet is indistinguishable from a
   sent one at the Python boundary. Whatever the root cause, that is worth
   fixing on its own: it is the reason this took a packet capture to notice.

What this leaves working, and what it does not:

- **Non-stackable items ferry correctly.** Weapons, trophies, armor, kits, dyes:
  one item, one slot, one drop. Every live run above that moved a
  non-stackable behaved as designed.
- **Stacks are refused rather than half-moved.** See section 4.1: the transfer
  now declines any stack larger than a configurable limit instead of dropping one
  unit and orphaning it.
- The quantity is still passed on every drop, so this path becomes correct with
  no change here the moment the native binding honors it.

### 4.1 The stack limit (the A1 workaround)

A1 cannot be fixed from Python, so it is bounded from Python. The rule is one
number, `TransferPolicy.max_stack_quantity`, and it is enforced identically on
both sides because quantity is the one flag that is never tri-state -- the
shared-memory snapshot carries it and so does a live bag read.

| Where | Behavior |
|---|---|
| Planner | `evaluate_item` blocks `quantity > stack_limit` with `REASON_STACK_TOO_LARGE`, sitting with the protections rather than the inclusion filters: it is a capability limit, not a preference |
| Coordinator | Never budgets an oversized stack, so the preview and the donor agree; `RoundPlan.ground_item_count` counts *piles*, one per unit |
| Donor | `Items.DropItems(max_units_per_item=N)` drains a stack by calling the drop once per unit, and **refuses an oversized stack before the first call** |
| Wire | `TransferDropItems` `ExtraData[2]`, as `stack=N`; absent or unreadable resolves to the default |

**Default 1, ceiling 20.** One means "unstacked items only", which is the honest
default while one call moves one unit: a stack of ten is ten chances to be
interrupted, and an interrupted stack ends up split between the donor's bags and
the floor. The ceiling bounds what a user or a message may *ask* for; raise it
when `DROP_UNITS_PER_CALL` changes, and not before. `TransferPolicy` itself
floors the value at 1 but does not cap it, because clamping inside a value object
would make a policy that does not do what it says -- validation belongs at the
untrusted edges, which are the widget input and `parse_stack_limit`.

**Why this value crosses the wire when no other policy field does.** Question 4
in section 11 settled that user policies do not reach donors, and that still
holds: protected models and quantity floors have no room and no carrier. The
stack limit is different in kind. It is not a preference about what to give
away; it is a per-session decision about how many interruptible steps one
inventory slot may become, the coordinator is the only party that knows it, and
it is one small integer in a 63-character field that was already empty. Both
sides rebuild the policy through one function, `resolve_wire_policy`, so a stack
the preview refused is refused by the donor for the same stated reason.

**Cost of raising it.** A stack of N costs N drop calls, produces N ground piles
and costs the receiver N pickup walks. It still costs only one receiver slot,
because the units merge back together on pickup (A4, confirmed). The widget says
this out loud next to the input rather than letting a user discover it by
watching a character drop cupcakes one at a time for a minute.

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

Phase 4 implemented these as `inventory_transfer.precheck_session`, which
returns one named `PrecheckResult` per condition so the widget can show all of
them rather than the first failure. Two refinements the implementation forced:

- A seventh check came first, because the other six assume it: one receiver,
  at least one donor, and the receiver not also listed as a donor.
- Verdicts are three-valued, not boolean. `Map.IsExplorable()` answers only for
  the local client; a peer's instance type is not in the snapshot, so the
  widget infers it from the map id through the `explorables` / `outposts`
  tables and reports `unknown` when the map is in neither. `precheck_passed`
  treats `unknown` as a failure, so an unresolved check can never authorise a
  session -- it just says which account it could not resolve.

The isolation check is the one that almost never fires through the widget:
`GetAllAccountData` already omits isolated peers, so an account the transport
could not reach is usually not on screen to be selected. It stays as the second
line of defence the plan intended.

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
The flip side, which phase 4 made concrete: a name is the *whole* contract. A
coordinator can plan against a policy carrying protected models and quantity
floors, but the donor rebuilds the policy from the name alone, so those fields
never reach it. The dry-run widget therefore labels them preview-only, and
question 4 in section 11 settles what phase 5 does about it: nothing. Only the
three built-in names go on the wire, and the Run control says out loud that a
donor will not honour anything else. The refusals that carry real risk --
untradeable, customized, last ID kit, last salvage kit -- are already in
`default` and are enforced by the donor's own live read.
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

### 6.8 What phase 5 changed about the above

Four things the implementation settled differently. Recorded here rather than
edited into the sections above, so the reasoning survives.

**1. HeroAI is held for the whole session, on every account in the instance, by
a new leased command pair.** Section 6.2's answer -- re-send `DisableHeroAI` at
the head of each round -- is withdrawn, and so is the first attempt at replacing
it.

*Why the plan's answer fails.* `hero_ai_snapshots` is a strict stack: every
`DisableHeroAI` pushes a snapshot and one `EnableHeroAI` pops exactly one.
Re-sending per round has two outcomes and both are bad. If
`HealStaleHeroAISnapshot` does not fire between rounds, the stack grows by one per
round and the single closing `EnableHeroAI` restores a *disabled* snapshot,
leaving every donor with HeroAI off and nothing left to turn it back on. If heal
does fire, it drains the stack between rounds, which is precisely the gap the
re-send existed to close. The proposal assumed it could have both.

*Why the first fix also failed, found by a live run 2026-09-07.* The initial
implementation sent no `DisableHeroAI` at all and leaned on the round handlers,
which each snapshot and restore around their own work. The claim was that only
the gap *between* rounds was left open, and that its worst case was benign. Both
halves were wrong, and the run showed the whole party fighting over the drops:

- The unprotected window is *inside* every round, not between them.
  `TransferDropItems` restores in its own `finally`, which fires the moment a
  donor finishes dropping -- while the receiver is still walking to the pile. The
  donor's `Looting` comes straight back on and it retakes its own drop.
- Only accounts that are sent a transfer message are ever suspended. A party
  member that is neither donor nor receiver is sent nothing, so it never stops
  looting at all. Selecting donors decides whose items *move*; it never decided
  who can interfere.

*The fix.* Two commands appended to `SharedCommandType`:

| Command | Direction | Params | ExtraData |
|---|---|---|---|
| `TransferHoldHeroAI` | coordinator -> every account in the instance | `(nonce, lease_ms, 0, 0)` | `(session_id, "", "", "")` |
| `TransferReleaseHeroAI` | coordinator -> the same accounts | `(0, 0, 0, 0)` | `(session_id, "", "", "")` |

`TransferHoldHeroAI` returns immediately and registers a hold in `_hero_ai_holds`;
`_tick_hero_ai_holds`, called every frame from `main()`, re-asserts the disabled
options and restores them when the lease runs out. The hold keeps its **own**
captured copy of the options and never touches `hero_ai_snapshots`, which is what
keeps `HealStaleHeroAISnapshot` out of the way -- heal only drains accounts that
have entries on that stack. The command is deliberately **not** in
`_HERO_AI_SUSPENDING_COMMANDS`, because there is nothing there for heal to drain.

Putting the restore in the per-frame sweeper rather than in the handler that took
the hold is the point of the design: a coroutine can die, a widget can be
disabled, a session can be aborted without ever sending its release, and none of
those may leave a character permanently unable to fight or loot. The sweeper is
the thing that always runs, so it is the thing that owns giving the options back.

**This is the second attempt, and section 6.9 records why the first one failed.**

The **lease** is what makes holding one safe, and it is not optional. A
suspension with no expiry is the single worst thing this feature could cause: a
character unable to fight or loot with nothing alive to restore it. The
coordinator releases on every exit path -- finish, abort, exception -- and renews
at the head of each round with the round id as the nonce, because `SendMessage`
would otherwise deduplicate an identical renewal and the lease would never move.
A repeat for a session already held extends the deadline rather than pushing a
second snapshot. If the coordinator dies anyway, the hold lapses on its own, and
the Run panel's **Force restore hero AI** button is there for anyone unwilling to
wait it out.

The cohort is `inventory_transfer.accounts_sharing_instance`, which is the whole
party in the receiver's instance including bystanders, and which excludes a
partyless account even on a matching map, because explorable instances are
party-scoped and it is not in the same copy of the map.

`PauseWidgets` is unchanged and still donor-only: it prevents a different problem
-- `AutoInventoryHandler` salvaging an item already queued to be dropped -- and
pausing this client would pause this widget, which is optional, stopping the
session mid-flight.
Because a paused donor has no way to unpause itself if the coordinator dies, the
Run panel carries a manual "resume donor widgets" button.

**2. The stall guard reads the receiver's report, not its free-slot delta.**
`reconcile_round` gained an optional `collected_items`. The delta under-counts
every merge -- an item landing in an existing stack costs no slot -- so two
merge-only rounds in a row would stall a session that is working perfectly. The
old derivation stays as the fallback for a caller without a receiver report,
which is what this function had before the receiver reported at all.

**3. The round report store moved into the planner module.** It was a `setattr`
on `GLOBAL_CACHE` read by `Messaging.py`; the widget needs it too, and widgets
cannot import one another. `Py4GWCoreLib` is loaded once per process, so a
module-level dict there survives a widget reload for exactly the same reason
`GLOBAL_CACHE` does, with one declared owner instead of an undeclared attribute
and two readers. It is bounded at `MAX_ROUND_REPORTS`, because an aborted session
or a recall leaves entries no coordinator will ever clear.
`Messaging.get_transfer_reports` / `clear_transfer_reports` remain as aliases,
since the section 9 hand-driven check names them.

**4. `SETTLE` warns and resumes; it does not hold the suspension open.** Section
6.7 wanted the session to wait for the ground to clear before resuming. That
conflicts with question 3's "warn, never block", and holding a snapshot open
across an indefinite user wait is exactly the state heal treats as stale. So the
session resumes immediately -- exactly one resume per pause, on every exit path
including abort and an exception -- and the leftover warning plus Recall stay on
screen as a dismissable notice that outlives the session that raised it.

**5. `PixelStack` is not used.** The rally step it was proposed for is already
inside `TransferDropItems`, which carries the rally coordinates and walks the
donor there itself. A separate rally command would be a second way for the rally
point to be wrong.

### 6.9 The transport serialises one message per sender-receiver pair

*Found by the live run of 2026-09-07, and this is the most transferable thing
this feature has learned. It is not a transfer rule; it constrains every
multibox feature that sends more than one message to the same account.*

`AllAccounts.SendMessage` scans the inbox for a slot to reuse before it queues.
That scan contains this, at `AllAccounts.py:964`:

```python
if message.Running:
    if int(PySystem.get_tick_count64() - message.Timestamp) < _MESSAGE_RUNNING_STALE_MS:
        return i          # returns a slot index, having queued nothing
    continue
```

It runs **before** the command, params and `ExtraData` comparisons. So *any*
running message between a given sender and receiver makes every further send to
that receiver a silent no-op for up to `_MESSAGE_RUNNING_STALE_MS`, which is
60 000. The caller cannot detect it: the return value is a valid slot index,
indistinguishable from a successful queue.

Two defects in this feature followed directly, and the log reads as a clock:

| Time | Line | Cause |
|---|---|---|
| 13:54:45 | round 1 asked | drop message swallowed by the running hold |
| 13:55:45 | "no answer from the donor" | exactly 60 s -- the hold went stale |
| 13:55:45 | receiver's pickup ran, logged, and reported nothing | its report was swallowed by its own still-running message |
| 13:56:45 | "the receiver never reported" | exactly 60 s again |
| 13:56:45 | round 2 asked; donor drops 7 items in one second | channel finally clear |

1. **A long-running handler cannot be used to hold state open.** The first
   implementation of `TransferHoldHeroAI` was a coroutine that stayed `Running`
   for the whole session precisely so its message stayed `Active` and heal would
   leave it alone. That worked, and it also jammed the coordinator's channel to
   every held account for 60 seconds -- including the drop commands. The
   replacement registers a hold and returns; a per-frame sweeper enforces and
   ends it. See 6.8.
2. **Report before finishing and the report is lost.** Both round handlers sent
   their `TransferReport` from the `finally` *before* `MarkMessageAsFinished`.
   For a donor that is harmless -- the report travels donor to coordinator, a
   different pair. For the receiver it is fatal, because the widget pins the
   coordinator to the receiver, so sender and receiver are one account and the
   still-running pickup message swallowed its own report. Every round therefore
   burned the full collect timeout and reconciled `collected = 0`, which is why
   a run that visibly picked up seven items reported "0 item(s) collected" and
   stalled. The two lines are now the other way round.

**The rule for anything else in this repository:** do not send a second message
to an account while your first one is still running, and do not send anything
from inside a handler before that handler's message is marked finished. If you
need a long-lived per-account state, own it in a module-level record swept from
`main()`; do not hold it open with a running message.

Whether `SendMessage` should compare the command before short-circuiting on a
running message is a real question for that owner. It looks like a bug -- the
enclosing loop is a *deduplicator*, and this branch deduplicates messages that
are not duplicates -- but existing callers may lean on the accidental
serialisation, so it is filed here rather than changed as part of this feature.

## 7. Files to add or change

| File | Change |
|---|---|
| `Py4GWCoreLib/enums_src/Multiboxing_enums.py` | append `TransferDropItems`, `TransferPickUpItems`, `TransferReport`, then `TransferHoldHeroAI`, `TransferReleaseHeroAI` |
| `Py4GWCoreLib/py4gwcorelib_src/inventory_transfer.py` | **new** - pure planner: eligibility, `predict_slot_cost`, round decomposition, reconciliation. No `Py4GW` imports |
| `Py4GWCoreLib/routines_src/yield_src/items.py` | add `Items.DropItems(item_ids)` and `Items.LootGroundItems(radius, max_items)`; the latter builds an unowned-item array and delegates to the existing `LootItems` |
| `Widgets/System/Messaging.py` | add the three handlers and their `ProcessMessages()` cases |
| `Widgets/Guild Wars/Items & Loot/InventoryTransfer.py` | **new** widget: account selection, receiver picker, policy editor, precheck, preview (phase 4), and the live session with run/abort/recall/progress (phase 5) |
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
4. **Widget, dry-run only.** *Done.*
   `Widgets/Guild Wars/Items & Loot/InventoryTransfer.py`. Account selection, a
   receiver picker, a wire-policy chooser, the precheck reported check by check,
   the forecast rounds, the literal round 1 payloads, the leftovers and the
   policy exclusions -- and no packet sent. The review gate did its job before a
   single item moved: it is what surfaced that a policy *name* is the whole
   contract a donor honours (open question 4), and that a peer's explorable
   state can only be inferred from its map id, which is why the precheck is
   three-valued rather than boolean.
   Three things landed in the phase 1 planner rather than the widget, because
   they are pure and had to stay offline-testable: the precheck vocabulary with
   its `ParticipantState` and `participant_from_shared_account` adapter, a
   `can_communicate` mirror of the transport's private rule, and
   `parse_model_list` / `parse_model_floors` for the policy text boxes.
   `explain_exclusions` joined them as the coordinator's counterpart to
   `explain_rejections`: the snapshot cannot see tradability, so listing what
   `select_droppable` refused would report "tradability unknown" for nearly
   every item and tell the user nothing.
5. **Widget, live.** *Implemented, live verification outstanding.* Run, abort,
   recall, progress and the leftover warning, driven by a flat state machine
   ticked from the widget's `update()` callback so a collapsed window cannot
   stall a round mid-flight. Shaped by the four answers in section 11: the
   coordinator is pinned to the receiver (Run refuses otherwise, because the
   rally point is where this character stands), donors are paused by default,
   leftovers warn without blocking, and only built-in policy names go on the
   wire -- with the stack limit of section 4.1 as the one deliberate scalar
   exception. The suspend gap is **not** closed by re-sending `DisableHeroAI`;
   that answer is withdrawn in section 6.8 for reasons the stack semantics of
   `hero_ai_snapshots` make unavoidable.
   Still to do: run it. The machine is proved offline and by inspection only.
   Start with the two-account, one-throwaway-item case in section 9.
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
last-kit protection; multi-round decomposition; stall detection. Phase 4 added:
the shared-account adapter; the `can_communicate` mirror against every branch of
the transport's rule; each precheck failing for its own reason and an unresolved
check not counting as a pass; the coordinator's exclusion explainer listing only
proven-ineligible items; and the policy text parsers, including the half-typed
input that must not blank a list.

Phase 5 added: the stack gate, reached identically from a snapshot record and a
live one; the ground-pile accounting a stack produces; the stack-limit wire field
including every malformed input; the report store's overwrite rule and its bound;
and the round-outcome logic that decides a round is over -- including that a
donor reporting "nothing eligible" *completes* a round rather than hanging it.

The stack gate forced one change to the existing fixtures: every policy in the
file now opts into whole stacks explicitly, because the shipped default is
"unstacked items only" and leaving them at it would have silently gutted the
merge and budget coverage rather than testing it. That is a fixture change worth
noticing in review -- it is exactly the shape of edit that makes a suite go green
by asking easier questions.

The widget itself is not covered here, and cannot be: it imports `PyImGui` and
`Py4GWCoreLib`, so it needs an injected client to load at all. That is the
reason every pure decision it makes lives in the planner instead -- the widget
is composition and drawing, and holds no arithmetic of its own.

**Static:** the project's strict Pyright/Pylance check over every changed
file. Typing failures are defects, per `AGENTS.md`. `Messaging.py` carries
sixteen pre-existing errors at the project configuration; the bar for a change
to it is that the count and the list are unchanged, checked against
`git show HEAD:Widgets/System/Messaging.py`, not that the file is clean.

The widget has a different bar than the planner, and it is worth stating so a
later reader does not "fix" it. At the project configuration it is clean. At
`strict` it reports five diagnostics, all of them the return or parameter types
of `Py4GWCoreLib` surfaces it merely calls -- `Color.to_tuple_normalized`,
`ConsoleLog`, `ImGui.Begin`, and since phase 5 `ShMem.SendMessage`, whose
`params` and `ExtraData` are declared as a bare `tuple`. Those are upstream gaps
shared by every widget in
the tree; narrowing them belongs to those owners, not to a caller. Hoisting the
five colour constants to module level already collapsed thirty-three of those
call sites into one, which was worth doing for the per-frame work it saves as
much as for the diagnostics.

**Widget dry run (phase 4, no live risk):** enable
`Inventory Transfer` in the widget manager with at least two accounts running.
Everything outside the Run panel is computation and drawing, so this much is
safe to do anywhere; do not press Run while checking it.

1. Every account publishing to shared memory appears in Participants, with its
   map, party, free slots and state.
2. Picking a receiver clears it from the donor list; selection and policy
   survive a widget reload, through the widget's account-scoped `Settings`
   document.
3. In an outpost, the explorable check fails and names the accounts. In an
   explorable area with everyone in one party, all seven checks pass.
4. The round 1 message preview matches what section 6.5 specifies: one
   `TransferDropItems` per donor carrying that donor's `max_items`, one
   `TransferPickUpItems` to the receiver, `2N + 2` messages for the round.
5. Nothing is sent while Run is untouched. The only `SendMessage` call in the
   widget is `TransferSession._send`, reachable only from `start`, `tick`,
   `recall` and `resume_donors`.
6. With the stack limit at 1, a donor holding a stack shows it under exclusions
   as "stack larger than the drop limit" and it never appears in a round. Raising
   the limit past the stack size moves it into the plan, and the preview's pile
   count rises by the stack size while its slot cost stays at one.

**Live run check (phase 5, two accounts, one throwaway item):** this moves a real
item. Do it in that order and stop at the first surprise.

1. Receiver is *this* client, donor is the other, both in one explorable area and
   one party, stack limit 1. Run. Expect: one round, the donor walks to this
   character, drops one item, this character collects it, progress shows
   `1 / 1 / 0`, and the session ends with "donors have nothing eligible left".
2. Abort mid-round. Expect the session to stop sending, donor widgets to resume,
   and the phase to reach `finished` rather than sticking.
3. Receiver with exactly one free slot and a donor holding three items. Expect
   the safety margin to hold the round to nothing, or one item moved and a clean
   stop -- not a pile on the floor.
4. Force a leftover: fill the receiver, then Run. Expect the leftover warning
   with a count, and Recall to send the donors after their own drops.
5. Raise the stack limit to 3 with a donor holding a stack of 3. Expect three
   drop calls, three ground piles, one receiver slot used, and the donor's bag
   slot emptied. **This is the case that has never worked**, so confirm the stack
   is fully gone from the donor rather than partially dropped.
6. Confirm no donor is left with HeroAI disabled or widgets paused after any of
   the above, including after an abort.
7. **The suspension case, which the 2026-09-07 run failed.** Run with a third
   account in the party that is neither donor nor receiver. Expect every account
   in the instance -- including that bystander -- to log `Hero AI held for
   session <id>`, to stop following, fighting and looting for the whole run, and
   to log `Hero AI hold ... released` at the end. Nobody but the receiver should
   touch the pile. Then confirm all five options are back exactly as they were on
   every account, bystander included.
8. Kill the coordinator mid-run (disable the widget) and confirm the hold lapses
   on its own within the lease rather than stranding anyone, and that **Force
   restore hero AI** ends it immediately from a fresh session.

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

## 11. Questions for the maintainer

All four were answered on 2026-09-03, before phase 5 was written. The answers
are binding on phase 5 and are recorded here because a decision that only ever
lived in a conversation is a decision that gets relitigated.

1. **Should the coordinator be pinned to the receiver's client?**
   *Answered: pinned.* A session only runs where this client is the receiver,
   and the rally point is wherever this character stands. The plan supported
   either, but the free-floating coordinator bought a second set of states and
   a second way for the rally point to be wrong, and paid for neither.
2. **Is `PauseWidgets` on donors acceptable as a default?**
   *Answered: default on, with a toggle.* It is a broad hammer, but what it
   prevents is `AutoInventoryHandler` salvaging an item already queued to be
   dropped. An interrupted widget is cheaper than a destroyed item.
3. **Should leftovers on the ground block the widget from being disabled?**
   *Answered: warn, never block.* Leftovers stop the session reporting success
   and keep Recall live, but a user trying to escape a bad state must always be
   able to. A widget that will not turn off is a worse failure than a pile that
   needs collecting.
4. **How should a user-defined policy reach the donors?**
   *Answered: it does not, in phase 5.* Live runs name one of the three
   built-in policies, which is the only thing a donor can resolve. The
   protected-model and quantity-floor fields stay in the widget as forecasting
   aids and the Run control says plainly that a donor will not honour them.

   *Refined during phase 5:* one scalar does cross, and section 4.1 explains
   why the stack limit is not an exception to this rule so much as a different
   question. It is not a preference about what to give away but a bound on how
   many interruptible steps a slot may become; only the coordinator knows it;
   and it is one integer in an `ExtraData` field that was already empty. Both
   sides rebuild the policy through `resolve_wire_policy`, so nothing else about
   the "name is the whole contract" rule changes. Note also that the maintainer
   ruled the version-skew worry moot: every account is launched from one
   launcher against one DLL, so a donor on an older build is not a real case.

   The reasoning, since this one looks like a hole and mostly is not: the
   refusals that actually matter -- untradeable items, customized gear, the last
   ID kit, the last salvage kit -- all live in `default` and are enforced by the
   donor from its own live read, which is the only read that can see them. What
   is missing is *user additions on top of those*. Building the distribution
   channel in the same phase as the first live item movement would mean
   debugging two new things at once the first time something ends up on the
   ground, so it waits.

   Still open, for a later phase: the shared `JsonFactory` document that
   `TeamInventoryViewer` already uses for inventories is the most promising
   carrier -- write the policy there, name it by hash in `ExtraData`, have the
   donor read it back. The two rejected alternatives were registering user
   policies per client through Settings (silently falls back to `default` on any
   account that missed the memo) and moving the editor to the donor entirely
   (correct about ownership, but per-account setup across eight boxes).
