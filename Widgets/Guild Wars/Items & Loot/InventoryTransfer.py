"""Cross-account inventory transfer -- participant selection, policy, preview, and the live run.

Phases 4 and 5 of `docs/loot/plans/cross-account-inventory-transfer.md`. The
widget composes the shared-memory snapshot, the phase 1 planner and the phase 3
message vocabulary into a session it first shows and then, on an explicit Run,
performs.

The preview is still the review gate and it still comes first: the slot budget,
the per-donor drop counts, the ground piles, the leftovers and the precheck
verdicts are all readable before a single item leaves a bag, and the arithmetic
behind them is proved offline in
`Examples and tests/tests/test_inventory_transfer_planner.py` rather than on the
user's inventory. Only the Run panel sends anything.

Four things the UI states out loud rather than hiding:

* **The coordinator is the receiver.** Run refuses unless this client is the
  selected receiver, because the rally point is wherever this character stands
  (plan section 11, question 1).
* **A drop message carries a policy name, not a policy.** A donor resolves that
  name through `inventory_transfer.resolve_wire_policy()`, so the
  coordinator-side refinements -- protected models, quantity floors -- shape this
  preview's budget but are not what a donor enforces. The one exception is the
  stack limit, which is a value on the wire rather than a name, and which donors
  do honour.
* **Stacks move one unit per drop call**, because the native binding ignores the
  quantity it is handed. A stack over the limit is refused whole; a stack within
  it becomes that many ground piles and that many pickup walks. The default limit
  is 1, which means unstacked items only.
* **The snapshot is up to 1.5 s stale**, so the plan is a forecast. That is good
  enough to refuse a session and never good enough to authorise one; each
  participant re-reads its own bags before acting.
"""

from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import Callable
from typing import Sequence
from uuid import uuid4

import PyImGui

from Py4GWCoreLib import Color
from Py4GWCoreLib import Console
from Py4GWCoreLib import ConsoleLog
from Py4GWCoreLib import GLOBAL_CACHE
from Py4GWCoreLib import ImGui
from Py4GWCoreLib import Map
from Py4GWCoreLib import ModelID
from Py4GWCoreLib import Player
from Py4GWCoreLib import Range
from Py4GWCoreLib import Routines
from Py4GWCoreLib import SharedCommandType
from Py4GWCoreLib import ThrottledTimer
from Py4GWCoreLib.enums import explorables
from Py4GWCoreLib.enums import outposts
from Py4GWCoreLib.py4gwcorelib_src import inventory_transfer as InventoryTransfer
from Py4GWCoreLib.py4gwcorelib_src.Settings import Settings

MODULE_NAME = "Inventory Transfer"

INI_PATH = "Widgets/Guild Wars/Items & Loot/InventoryTransfer"
INI_FILENAME = "InventoryTransfer.ini"

SECTION_SESSION = "Session"
SECTION_POLICY = "Policy"

#: The snapshot itself only refreshes every 1.5 s, so recomputing faster than
#: this would burn frames re-deriving the same forecast.
REFRESH_INTERVAL_MS = 1000

#: An absurd inventory should not turn the preview into an endless wall of rows.
MAX_PLAN_ROWS = 250
MAX_LIST_ROWS = 60

TABLE_FLAGS = int(PyImGui.TableFlags.Borders | PyImGui.TableFlags.RowBg | PyImGui.TableFlags.SizingStretchProp)
HEADER_FLAGS = int(PyImGui.TreeNodeFlags.DefaultOpen)



def _rgba(red: int, green: int, blue: int) -> tuple[float, float, float, float]:
    """A normalized colour tuple, resolved once at import.

    `Color` stays the owner of the conversion. Pinning the result here rather
    than calling `to_tuple_normalized()` at every ImGui call site does two
    things: the widget stops re-deriving five constants on every frame, and the
    upstream method's untyped return is narrowed in one place instead of thirty.
    """
    channels: tuple[float, ...] = Color(red, green, blue, 255).to_tuple_normalized()
    return (float(channels[0]), float(channels[1]), float(channels[2]), float(channels[3]))


COLOR_TITLE = _rgba(255, 200, 100)
COLOR_OK = _rgba(120, 220, 130)
COLOR_WARN = _rgba(240, 200, 90)
COLOR_FAIL = _rgba(235, 120, 120)
COLOR_MUTED = _rgba(165, 165, 165)

#: Ordered so the combo index is stable across sessions; these are the only names
#: a donor on any build can resolve, which is what makes them the wire contract.
WIRE_POLICY_NAMES: tuple[str, ...] = ("default", "stackables", "optimistic")

WIRE_POLICY_SUMMARY: dict[str, str] = {
    "default": "Whole stacks, one receiver slot reserved per stack. Never over-budgets.",
    "stackables": "Default, restricted to items the donor can prove are stackable.",
    "optimistic": "Tops up partial stacks on the receiver first. Rests on unverified A4 and A7.",
}


def _model_name(model_id: int) -> str:
    """Name an item by model id only.

    Never by decoding UI frame text: that native path has crashed the client
    before, and the plan bans it outright (section 4).
    """
    try:
        return ModelID(int(model_id)).name.replace("_", " ")
    except ValueError:
        return "Model %d" % int(model_id)


def _map_name(map_id: int) -> str:
    if map_id in explorables:
        return str(explorables[map_id])
    if map_id in outposts:
        return str(outposts[map_id])
    return "Map %d" % int(map_id)


def _is_explorable(map_id: int, is_local: bool) -> bool | None:
    """Answer the explorable question for one account, or admit it cannot.

    The local client knows for certain. For a peer the snapshot carries a map id
    and no instance type, so the answer is inferred from the map-id tables; a map
    in neither table reports `None`, which the precheck treats as unresolved
    rather than as a pass.
    """
    if is_local:
        return bool(Map.IsExplorable())
    if map_id <= 0:
        return None
    if map_id in explorables:
        return True
    if map_id in outposts:
        return False
    return None


@dataclass(frozen=True)
class AccountRow:
    """One selectable account, flattened out of the shared-memory snapshot.

    Deliberately a copy rather than a live `AccountStruct` reference: the widget
    holds this across frames, and shared memory is rewritten under it.
    """

    key: str
    state: InventoryTransfer.ParticipantState
    position: tuple[float, float]
    map_id: int
    is_local: bool

    #: True only when the free-slot count came from this client's own bag read.
    #: A peer's capacity arrives through a shared-memory field that is currently
    #: publishing its item count instead, so its free slots are not reportable.
    capacity_is_trusted: bool = False

    @property
    def label(self) -> str:
        return self.state.label

    @property
    def map_name(self) -> str:
        return _map_name(self.map_id)


@dataclass
class TransferPreview:
    """Everything the dry run has to say, recomputed on a throttle."""

    rows: tuple[AccountRow, ...] = ()
    prechecks: tuple[InventoryTransfer.PrecheckResult, ...] = ()
    session: InventoryTransfer.SessionPlan | None = None
    excluded: tuple[tuple[str, InventoryTransfer.ItemRecord, InventoryTransfer.EligibilityResult], ...] = ()
    plannable_by_donor: dict[str, int] = field(default_factory=dict[str, int])
    rally: tuple[float, float] = (0.0, 0.0)
    blocker: str = ""


# --- live session ----------------------------------------------------------

# Phase 5. Everything above this line still computes; from here down the widget
# sends. The state machine is deliberately flat -- one phase, one deadline, one
# thing being waited for -- because the interesting failures are all "a
# participant never answered", and a flat machine can say which one.

PHASE_IDLE = "idle"
PHASE_SUSPENDING = "pausing donor widgets"
PHASE_DROPPING = "waiting for donors to drop"
PHASE_COLLECTING = "collecting the pile"
PHASE_RECONCILING = "reconciling"
PHASE_RESUMING = "resuming donor widgets"
PHASE_DONE = "finished"

#: Long enough for a `PauseWidgets` message to be picked up and run on every
#: donor before the first drop is asked for. `ProcessMessages` dispatches once a
#: frame, so this is generous rather than tight.
SUSPEND_SETTLE_MS = 1500

#: A donor's round is a walk to the rally point (the handler allows 20 s for it)
#: plus one confirmed drop call per unit. This only has to catch a donor that
#: never ran the handler at all.
DROP_ROUND_TIMEOUT_MS = 60000

#: The receiver walks a pile it is standing on and `LootItems` bounds its own
#: per-item wait, so the same reasoning applies.
COLLECT_ROUND_TIMEOUT_MS = 60000

#: How often the machine looks at the world. Reports arrive through shared
#: memory, so polling every frame would re-read it sixty times a second to watch
#: a number that changes every few seconds.
PHASE_POLL_MS = 250

#: How long a HeroAI hold survives without being renewed. Comfortably longer than
#: the worst-case round (a drop timeout plus a collect timeout) so a slow round
#: cannot drop the suspension mid-flight, and short enough that a coordinator that
#: dies outright does not strand a character unable to fight or loot for long.
HERO_AI_HOLD_LEASE_MS = 120000

#: How often the hold is renewed while a session runs. Short relative to the lease
#: on purpose: a renewal can be swallowed by the transport if a round handler is
#: mid-flight on that account, so the session needs several attempts inside one
#: lease rather than one chance per round.
HERO_AI_RENEW_INTERVAL_MS = 30000


@dataclass(frozen=True)
class RoundRecord:
    """One finished round, as the progress table shows it."""

    round_id: int
    planned_items: int
    planned_piles: int
    dropped: int
    collected: int
    on_ground: int
    note: str = ""


class TransferSession:
    """The live half: sends what the preview describes, one round at a time.

    Ticked from the module's `update()` rather than from `draw()`. Per-frame
    non-UI work belongs on the update callback, and it means a session keeps
    advancing while its window is collapsed -- a transfer that stalls because
    the user folded a header would be a memorable way to lose items.

    **On suspending HeroAI.** A `TransferHoldHeroAI` lease goes to every account in
    the receiver's instance for the whole session, and is released on every exit
    path. Neither half of that is decoration.

    *Session-scoped*, because a per-message suspension demonstrably is not enough.
    `TransferDropItems` restores in its own `finally`, which fires the moment a
    donor has finished dropping -- while the receiver is still walking to the pile
    -- so the donor's `Looting` comes back on and it retakes its own drop.

    *Instance-wide*, because selecting donors decides whose items move, not who can
    interfere. A party member that is neither donor nor receiver is never sent a
    transfer message and so never stops looting at all, and it will take the pile.

    `DisableHeroAI` cannot do this job: it finishes immediately, so
    `HealStaleHeroAISnapshot` unwinds it, and re-sending it per round would push a
    snapshot per round onto a stack that pops exactly once. The hold is a
    long-running handler instead, which keeps its message `Active` and therefore
    keeps heal away, and it carries a lease so a coordinator that dies cannot
    strand a character with HeroAI off forever.

    `PauseWidgets` is separate and still sent, because it prevents a different
    problem -- `AutoInventoryHandler` salvaging an item already queued to be
    dropped. It goes to donors only. Sending it to this client would pause this
    widget, which is optional, and the session would stop ticking mid-flight.
    """

    def __init__(self, read_accounts: Callable[[], tuple[AccountRow, ...]]) -> None:
        self._read_accounts = read_accounts

        self.phase: str = PHASE_IDLE
        self.session_id: str = ""
        self.coordinator_key: str = ""
        self.receiver_key: str = ""
        self.donor_keys: tuple[str, ...] = ()
        self.policy: InventoryTransfer.TransferPolicy = InventoryTransfer.TransferPolicy()
        self.wire_policy: str = WIRE_POLICY_NAMES[0]
        self.pickup_radius: int = int(Range.Earshot.value)
        self.rally: tuple[float, float] = (0.0, 0.0)
        self.max_rounds: int = InventoryTransfer.DEFAULT_MAX_ROUNDS

        self.round_id: int = 0
        self.plan: InventoryTransfer.RoundPlan | None = None
        self.round_donors: tuple[str, ...] = ()
        self.history: list[RoundRecord] = []
        self.stop_reason: str = ""
        self.error: str = ""
        self.leftover_on_ground: int = 0

        #: Set when a finished session left a pile behind, cleared only by the
        #: user. Leftovers must never block -- a user escaping a bad state has to
        #: be able to -- but they must not quietly scroll away either, so this
        #: keeps the warning and its Recall on screen until it is acknowledged.
        self.needs_attention: bool = False

        self._paused_donors: tuple[str, ...] = ()
        #: Every account holding a HeroAI lease for this session -- the whole
        #: instance, not just the participants. Kept so the release goes exactly
        #: where the hold went, even if the party has changed since.
        self._held_accounts: tuple[str, ...] = ()
        self._stall: InventoryTransfer.StallTracker = InventoryTransfer.StallTracker()
        self._free_before: int = 0
        self._timer: ThrottledTimer = ThrottledTimer(SUSPEND_SETTLE_MS)
        self._poll: ThrottledTimer = ThrottledTimer(PHASE_POLL_MS)
        self._renew: ThrottledTimer = ThrottledTimer(HERO_AI_RENEW_INTERVAL_MS)
        self._renew_nonce: int = 0

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.phase not in (PHASE_IDLE, PHASE_DONE)

    @property
    def finished(self) -> bool:
        return self.phase == PHASE_DONE

    @property
    def items_moved(self) -> int:
        return sum(record.collected for record in self.history)

    def _enter(self, phase: str) -> None:
        self.phase = phase
        self._timer.Reset()

    def _elapsed(self) -> int:
        return int(self._timer.GetTimeElapsed())

    def _send(
        self,
        to: str,
        command: SharedCommandType,
        params: tuple[float, float, float, float],
        extra: tuple[str, str, str, str],
    ) -> bool:
        slot = GLOBAL_CACHE.ShMem.SendMessage(self.coordinator_key, to, command, params, extra)
        if slot == -1:
            ConsoleLog(MODULE_NAME, "Could not send %s to %s." % (command.name, to), Console.MessageType.Error, False)
            return False
        return True

    def start(
        self,
        session_id: str,
        coordinator_key: str,
        receiver_key: str,
        donor_keys: Sequence[str],
        policy: InventoryTransfer.TransferPolicy,
        wire_policy: str,
        pickup_radius: int,
        rally: tuple[float, float],
        max_rounds: int,
        pause_donor_widgets: bool,
    ) -> None:
        self.session_id = session_id
        self.coordinator_key = coordinator_key
        self.receiver_key = receiver_key
        self.donor_keys = tuple(donor_keys)
        self.policy = policy
        self.wire_policy = wire_policy
        self.pickup_radius = pickup_radius
        self.rally = rally
        self.max_rounds = max_rounds

        self.round_id = 0
        self.plan = None
        self.round_donors = ()
        self.history = []
        self.stop_reason = ""
        self.error = ""
        self.leftover_on_ground = 0
        self.needs_attention = False
        self._stall = InventoryTransfer.StallTracker()

        # A session's reports are keyed by its own id, but an aborted predecessor
        # that reused the id would otherwise satisfy round 1's wait instantly.
        InventoryTransfer.clear_transfer_reports(session_id)

        # Quieten the whole instance before anything is dropped. Everyone here, not
        # just the participants: a party member nobody messaged still loots.
        rows = self._read_accounts()
        receiver_row = next((row for row in rows if row.key == receiver_key), None)
        cohort: tuple[str, ...] = ()
        if receiver_row is not None:
            cohort = InventoryTransfer.accounts_sharing_instance(
                receiver_row.state, [row.state for row in rows]
            )
        self._held_accounts = ()
        self._renew_nonce = 0
        self._renew.Reset()
        self._renew_hero_ai_hold(cohort, nonce=self._renew_nonce)

        self._paused_donors = ()
        if pause_donor_widgets:
            paused: list[str] = []
            for key in self.donor_keys:
                if self._send(key, SharedCommandType.PauseWidgets, (0.0, 0.0, 0.0, 0.0), ("", "", "", "")):
                    paused.append(key)
            self._paused_donors = tuple(paused)

        ConsoleLog(
            MODULE_NAME,
            "Transfer %s started: %d donor(s) -> %s, policy %s, stacks up to %d."
            % (session_id, len(self.donor_keys), receiver_key, wire_policy, policy.stack_limit),
            Console.MessageType.Info,
            True,
        )
        self._enter(PHASE_SUSPENDING)

    def _renew_hero_ai_hold(self, accounts: Sequence[str], nonce: int) -> None:
        """Take or extend the instance-wide HeroAI hold.

        The nonce is the round id, and it is not decoration: `SendMessage`
        deduplicates an identical still-pending message, so renewing with the same
        params every round would silently reuse one slot and the lease would never
        move. On the receiving side a repeat for a session already held extends the
        lease rather than pushing a second snapshot.
        """
        held = list(self._held_accounts)
        for key in accounts:
            if self._send(
                key,
                SharedCommandType.TransferHoldHeroAI,
                (float(nonce), float(HERO_AI_HOLD_LEASE_MS), 0.0, 0.0),
                (self.session_id, "", "", ""),
            ):
                if key not in held:
                    held.append(key)
        self._held_accounts = tuple(held)

    def _release_hero_ai_hold(self) -> None:
        """Give every held account its HeroAI back. Safe to call more than once."""
        for key in self._held_accounts:
            self._send(
                key,
                SharedCommandType.TransferReleaseHeroAI,
                (0.0, 0.0, 0.0, 0.0),
                (self.session_id, "", "", ""),
            )
        self._held_accounts = ()

    def abort(self, reason: str = "aborted by the user") -> None:
        if not self.running:
            return
        ConsoleLog(MODULE_NAME, "Transfer aborted: %s." % reason, Console.MessageType.Warning, True)
        self._finish(reason)

    def _fail(self, reason: str) -> None:
        self.error = reason
        ConsoleLog(MODULE_NAME, "Transfer failed: %s." % reason, Console.MessageType.Error, True)
        self._finish(reason)

    def _finish(self, reason: str) -> None:
        """Every exit runs through here, so the resume can never be skipped."""
        self.stop_reason = reason
        self.leftover_on_ground = self._ground_count()
        self._enter(PHASE_RESUMING)

    def _ground_count(self) -> int:
        """Items still collectable at the rally point. Counts, never collects."""
        try:
            return len(
                Routines.Yield.Items.GetGroundItemIds(
                    radius=float(self.pickup_radius or Range.Earshot.value),
                    center=self.rally,
                )
            )
        except Exception as exc:
            ConsoleLog(MODULE_NAME, "Could not count ground items: %s" % exc, Console.MessageType.Warning, False)
            return 0

    # -- the machine --------------------------------------------------------

    def tick(self) -> None:
        if not self.running:
            return

        # Renewed on a clock rather than only per round, because a round can outlast
        # the gap between renewals and because any single renewal may be swallowed by
        # the transport while a handler is running on that account.
        if self._renew.IsExpired():
            self._renew.Reset()
            self._renew_nonce += 1
            self._renew_hero_ai_hold(self._held_accounts, nonce=self._renew_nonce)

        if not self._poll.IsExpired():
            return
        self._poll.Reset()

        try:
            if self.phase == PHASE_SUSPENDING:
                if self._elapsed() >= SUSPEND_SETTLE_MS:
                    self._begin_round()
            elif self.phase == PHASE_DROPPING:
                self._await_drops()
            elif self.phase == PHASE_COLLECTING:
                self._await_collect()
            elif self.phase == PHASE_RECONCILING:
                self._reconcile()
            elif self.phase == PHASE_RESUMING:
                self._resume()
        except Exception as exc:
            # A raised tick must still put the donors back, so this routes through
            # the resume rather than stopping where it stands.
            if self.phase == PHASE_RESUMING:
                self._enter(PHASE_DONE)
                self.error = str(exc)
            else:
                self._fail(str(exc))

    def _begin_round(self) -> None:
        self.round_id += 1
        if self.round_id > self.max_rounds:
            self._finish(InventoryTransfer.STOP_ROUND_LIMIT)
            return

        rows = {row.key: row for row in self._read_accounts()}
        receiver_row = rows.get(self.receiver_key)
        if receiver_row is None:
            self._fail("the receiver stopped publishing to shared memory")
            return

        donors = [rows[key].state.as_donor() for key in self.donor_keys if key in rows]
        if not donors:
            self._finish("no donor is still reachable")
            return

        # The snapshot supplies the receiver's stack contents, which is all the
        # merge arithmetic needs; the free-slot count comes from a live local read
        # because that is the whole round budget and the snapshot lags by 1.5 s.
        # This client is the receiver, so the authoritative read is available.
        projection = InventoryTransfer.ReceiverProjection.from_snapshot(receiver_row.state.inventory)
        self._free_before = int(GLOBAL_CACHE.Inventory.GetFreeSlotCount())
        projection.free_slots = self._free_before

        plan = InventoryTransfer.plan_round(self.round_id, projection, donors, self.policy)
        if plan.item_count == 0:
            usable = self._free_before - max(self.policy.safety_margin, 0)
            self._finish(
                InventoryTransfer.STOP_RECEIVER_FULL if usable <= 0 else InventoryTransfer.STOP_DONORS_EXHAUSTED
            )
            return

        self.plan = plan
        InventoryTransfer.clear_transfer_reports(self.session_id, self.round_id)

        # Renew before asking anyone to drop, on top of the clock in `tick`. Cheap, and
        # it puts a fresh lease in place at exactly the moment a round is about to spend
        # the longest stretch without another chance to renew.
        self._renew.Reset()
        self._renew_nonce += 1
        self._renew_hero_ai_hold(self._held_accounts, nonce=self._renew_nonce)

        rally_x, rally_y = self.rally
        stack_field = InventoryTransfer.format_stack_limit(self.policy.stack_limit)

        # Only donors the transport accepted. Waiting on a donor whose message was
        # never queued would cost the round its whole timeout to learn nothing.
        asked: list[str] = []
        for donor_key in plan.donor_keys:
            if self._send(
                donor_key,
                SharedCommandType.TransferDropItems,
                (float(self.round_id), float(plan.budget_for(donor_key)), float(rally_x), float(rally_y)),
                (self.wire_policy, self.session_id, stack_field, ""),
            ):
                asked.append(donor_key)
        self.round_donors = tuple(asked)

        if not asked:
            self._fail("no donor could be reached")
            return

        ConsoleLog(
            MODULE_NAME,
            "Round %d: asked %d donor(s) for %d item(s), expecting %d ground pile(s)."
            % (self.round_id, len(asked), plan.item_count, plan.ground_item_count),
            Console.MessageType.Info,
            True,
        )
        self._enter(PHASE_DROPPING)

    def _outcome(self) -> InventoryTransfer.RoundOutcome:
        return InventoryTransfer.summarize_round_reports(
            self.round_id, self.session_id, self.receiver_key, self.round_donors
        )

    def _await_drops(self) -> None:
        outcome = self._outcome()
        timed_out = self._elapsed() >= DROP_ROUND_TIMEOUT_MS
        if not outcome.drops_complete and not timed_out:
            return

        if timed_out and not outcome.drops_complete:
            # Collect anyway. Whatever did reach the floor is better collected than
            # left, and a donor that answers late simply contributes to a later
            # round or to the leftover count.
            ConsoleLog(
                MODULE_NAME,
                "Round %d: no answer from %s; collecting what is on the ground."
                % (self.round_id, ", ".join(outcome.missing_donors)),
                Console.MessageType.Warning,
                True,
            )

        # Every donor answered, none of them dropped anything, and the floor is
        # clean. That is how a transfer ends normally -- the donors have nothing
        # eligible left -- so skip the walk rather than suspending this client's
        # HeroAI to collect an empty patch of ground.
        if outcome.drops_complete and outcome.dropped == 0 and self._ground_count() == 0:
            self._enter(PHASE_RECONCILING)
            return

        plan = self.plan
        budget = plan.ground_item_count if plan is not None else 0
        self._send(
            self.receiver_key,
            SharedCommandType.TransferPickUpItems,
            (float(self.round_id), float(budget), float(self.pickup_radius), 0.0),
            (self.session_id, "", "", ""),
        )
        self._enter(PHASE_COLLECTING)

    def _await_collect(self) -> None:
        outcome = self._outcome()
        if not outcome.receiver_reported and self._elapsed() < COLLECT_ROUND_TIMEOUT_MS:
            return
        if not outcome.receiver_reported:
            ConsoleLog(
                MODULE_NAME,
                "Round %d: the receiver never reported; reconciling from the ground instead."
                % self.round_id,
                Console.MessageType.Warning,
                True,
            )
        self._enter(PHASE_RECONCILING)

    def _reconcile(self) -> None:
        plan = self.plan
        if plan is None:
            self._fail("a round finished without a plan")
            return

        outcome = self._outcome()
        dropped_by_donor = {
            entry.reporter_email: entry.items_moved
            for entry in outcome.reports
            if entry.reporter_email in self.round_donors
        }
        free_after = int(GLOBAL_CACHE.Inventory.GetFreeSlotCount())
        on_ground = self._ground_count()

        report = InventoryTransfer.reconcile_round(
            plan,
            dropped_by_donor,
            self._free_before,
            free_after,
            on_ground,
            tracker=self._stall,
            # Piles the receiver actually took, which is the honest "did this round
            # move anything" signal: a collected item that merged into an existing
            # stack costs no slot at all, and the free-slot delta would call that
            # round empty and stall the session on a transfer that is working.
            collected_items=outcome.collected,
        )

        note = ""
        failures = [entry for entry in outcome.failures if entry.status != InventoryTransfer.STATUS_NOTHING_ELIGIBLE]
        if failures:
            note = "; ".join("%s: %s" % (entry.reporter_email, entry.status_text) for entry in failures)

        self.history.append(
            RoundRecord(
                round_id=self.round_id,
                planned_items=plan.item_count,
                planned_piles=plan.ground_item_count,
                dropped=report.dropped_items,
                collected=outcome.collected,
                on_ground=on_ground,
                note=note,
            )
        )
        InventoryTransfer.clear_transfer_reports(self.session_id, self.round_id)

        receiver_entry = outcome.receiver_report
        if receiver_entry is not None and receiver_entry.status == InventoryTransfer.STATUS_INVENTORY_FULL:
            self._finish(InventoryTransfer.STOP_RECEIVER_FULL)
            return
        if report.stalled:
            self._finish(InventoryTransfer.STOP_STALLED)
            return

        self._begin_round()

    def _resume(self) -> None:
        # Counted while the instance is still quiet, and before anything is handed
        # back. Recounted rather than trusting the count taken at `_finish` because a
        # donor that answered late may have dropped in between; taken *first* because
        # the moment HeroAI comes back the party starts eating the evidence.
        self.leftover_on_ground = self._ground_count()
        self.needs_attention = self.leftover_on_ground > 0

        # Released even when there are leftovers. Holding the suspension open until a
        # user acknowledges a warning is the one thing this must never do -- "warn,
        # never block" -- and Recall still works afterwards, because the pickup
        # handler suspends HeroAI around its own collect.
        self._release_hero_ai_hold()

        for key in self._paused_donors:
            self._send(key, SharedCommandType.ResumeWidgets, (0.0, 0.0, 0.0, 0.0), ("", "", "", ""))
        self._paused_donors = ()

        ConsoleLog(
            MODULE_NAME,
            "Transfer %s finished after %d round(s): %d item(s) collected, %d left on the ground (%s)."
            % (self.session_id, len(self.history), self.items_moved, self.leftover_on_ground, self.stop_reason),
            Console.MessageType.Info,
            True,
        )
        self._enter(PHASE_DONE)

    # -- recovery -----------------------------------------------------------

    def resume_donors(self) -> None:
        """Manual escape hatch: hand HeroAI and widgets back without a running session.

        A session that died with this widget disabled, or a client restarted
        mid-transfer, leaves accounts suspended with nothing left to release them.
        The lease expires on its own eventually; this is the button for someone who
        does not want to wait for it. Cheap to send and harmless when unneeded.

        Sent to everyone this session ever held, not just the donors, because the
        hold covers the whole instance.
        """
        for key in set(self._held_accounts) | set(self.donor_keys or ()) | {self.receiver_key}:
            if not key:
                continue
            # Empty session id: release whatever is held there. Someone reaching for
            # this button does not know which session stranded them, and should not
            # have to.
            self._send(
                key,
                SharedCommandType.TransferReleaseHeroAI,
                (0.0, 0.0, 0.0, 0.0),
                ("", "", "", ""),
            )
            self._send(key, SharedCommandType.ResumeWidgets, (0.0, 0.0, 0.0, 0.0), ("", "", "", ""))
        self._held_accounts = ()
        self._paused_donors = ()

    def recall(self) -> int:
        """Ask the donors to retake what is still on the floor.

        Sent to donors rather than to the receiver on purpose: the receiver either
        could not take these items or ran out of room, so asking it again is the
        one thing certain not to help. A donor may retake its own drop (verified
        live), and `LootGroundItems` includes items still reserved for the caller,
        which is exactly that case.

        Each donor collects around wherever it is standing, because the handler
        uses its own position as the centre. That is the rally point as long as
        nothing walked it away, which is the normal state right after a round.
        """
        sent = 0
        recall_round = self.round_id + 1000  # never collides with a real round id
        for key in self.donor_keys:
            if self._send(
                key,
                SharedCommandType.TransferPickUpItems,
                (float(recall_round), 0.0, float(self.pickup_radius), 0.0),
                (self.session_id, "", "", ""),
            ):
                sent += 1
        ConsoleLog(MODULE_NAME, "Recall sent to %d donor(s)." % sent, Console.MessageType.Info, True)
        return sent


class InventoryTransferWidget:
    """Session state for the dry run. Owns nothing the game can observe."""

    def __init__(self) -> None:
        self._settings: Settings | None = None
        self._ini_key: str = ""
        self._loaded: bool = False

        self._receiver_key: str = ""
        self._donor_keys: list[str] = []

        self._wire_policy: str = WIRE_POLICY_NAMES[0]
        self._safety_margin: int = 1
        self._max_rounds: int = InventoryTransfer.DEFAULT_MAX_ROUNDS
        self._pickup_radius: int = int(Range.Earshot.value)
        self._max_stack: int = InventoryTransfer.DEFAULT_MAX_STACK_QUANTITY
        self._pause_donor_widgets: bool = True
        self._protected_text: str = ""
        self._floors_text: str = ""

        self._session_id: str = uuid4().hex[:8]
        self._preview: TransferPreview = TransferPreview()
        self._refresh_timer: ThrottledTimer = ThrottledTimer(REFRESH_INTERVAL_MS)
        self._dirty: bool = True

        self._session: TransferSession = TransferSession(self._read_accounts)

    # -- persistence --------------------------------------------------------

    def _config(self) -> Settings | None:
        if self._settings is None:
            self._settings = Settings("%s/%s" % (INI_PATH, INI_FILENAME), "account")
        if not self._settings.is_ready():
            return None
        if not self._ini_key:
            self._ini_key = self._settings.name
        return self._settings

    def _load(self, cfg: Settings) -> None:
        self._receiver_key = cfg.get_str(SECTION_SESSION, "receiver", "")
        self._donor_keys = [key.strip() for key in cfg.get_str(SECTION_SESSION, "donors", "").split(",") if key.strip()]

        wire = cfg.get_str(SECTION_POLICY, "wire_policy", WIRE_POLICY_NAMES[0]).strip().lower()
        self._wire_policy = wire if wire in WIRE_POLICY_NAMES else WIRE_POLICY_NAMES[0]
        self._safety_margin = max(0, min(int(cfg.get_int(SECTION_POLICY, "safety_margin", 1)), 20))
        stored_rounds = cfg.get_int(SECTION_POLICY, "max_rounds", InventoryTransfer.DEFAULT_MAX_ROUNDS)
        self._max_rounds = max(1, min(int(stored_rounds), 64))
        self._pickup_radius = max(0, int(cfg.get_int(SECTION_POLICY, "pickup_radius", int(Range.Earshot.value))))
        stored_stack = cfg.get_int(SECTION_POLICY, "max_stack", InventoryTransfer.DEFAULT_MAX_STACK_QUANTITY)
        self._max_stack = max(1, min(int(stored_stack), InventoryTransfer.MAX_STACK_QUANTITY))
        self._pause_donor_widgets = bool(cfg.get_bool(SECTION_POLICY, "pause_donor_widgets", True))
        self._protected_text = cfg.get_str(SECTION_POLICY, "protected_models", "")
        self._floors_text = cfg.get_str(SECTION_POLICY, "model_floors", "")

        self._loaded = True
        self._dirty = True

    def _store(self, cfg: Settings) -> None:
        cfg.set(SECTION_SESSION, "receiver", self._receiver_key)
        cfg.set(SECTION_SESSION, "donors", ",".join(self._donor_keys))
        cfg.set(SECTION_POLICY, "wire_policy", self._wire_policy)
        cfg.set(SECTION_POLICY, "safety_margin", self._safety_margin)
        cfg.set(SECTION_POLICY, "max_rounds", self._max_rounds)
        cfg.set(SECTION_POLICY, "pickup_radius", self._pickup_radius)
        cfg.set(SECTION_POLICY, "max_stack", self._max_stack)
        cfg.set(SECTION_POLICY, "pause_donor_widgets", self._pause_donor_widgets)
        cfg.set(SECTION_POLICY, "protected_models", self._protected_text)
        cfg.set(SECTION_POLICY, "model_floors", self._floors_text)
        self._dirty = True

    # -- planning inputs ----------------------------------------------------

    def _policy(self) -> InventoryTransfer.TransferPolicy:
        """The policy this preview budgets with.

        Built through `resolve_wire_policy` from exactly what the drop message
        will carry -- the policy name and the stack limit field -- so the preview
        cannot budget against rules the donor will not apply. The fields added on
        top are coordinator-side and are labelled as such in the UI:
        `safety_margin` shapes the budget and never crosses the wire, and the
        protected models and floors have no room on it.
        """
        base = InventoryTransfer.resolve_wire_policy(
            self._wire_policy, InventoryTransfer.format_stack_limit(self._max_stack)
        )
        return replace(
            base,
            safety_margin=self._safety_margin,
            protected_models=InventoryTransfer.parse_model_list(self._protected_text),
            model_floors=InventoryTransfer.parse_model_floors(self._floors_text),
        )

    def _read_accounts(self) -> tuple[AccountRow, ...]:
        """Flatten every visible account out of shared memory.

        `GetAllAccountData` already drops heroes, pets and isolated peers, so an
        account missing from this list is one the transport would not reach
        anyway. The precheck's isolation test stays as a second line of defence.
        """
        local_email = str(Player.GetAccountEmail() or "")
        rows: list[AccountRow] = []

        # Shared memory does not publish a capacity worth trusting, so this
        # client corrects its own row from the API that owns the answer. It
        # matters more than a cosmetic column: the receiver is always this
        # client, and the receiver's free slots are the whole round budget.
        local_capacity = -1
        try:
            local_capacity = int(GLOBAL_CACHE.Inventory.GetInventorySpace()[1])
        except Exception as exc:
            ConsoleLog(MODULE_NAME, "Local inventory space unreadable: %s" % exc, Console.MessageType.Warning, False)

        for account in GLOBAL_CACHE.ShMem.GetAllAccountData():
            key = str(account.AccountEmail or "")
            if not key:
                continue
            map_id = int(account.AgentData.Map.MapID)
            is_local = key == local_email
            state = InventoryTransfer.participant_from_shared_account(
                account, explorable=_is_explorable(map_id, is_local)
            )
            if is_local and local_capacity >= 0:
                state = replace(state, inventory=state.inventory.with_capacity(local_capacity))
            rows.append(
                AccountRow(
                    key=key,
                    state=state,
                    position=(float(account.AgentData.Pos.x), float(account.AgentData.Pos.y)),
                    map_id=map_id,
                    is_local=is_local,
                    capacity_is_trusted=is_local and local_capacity >= 0,
                )
            )
        return tuple(rows)

    def _compute(self) -> TransferPreview:
        rows = self._read_accounts()
        preview = TransferPreview(rows=rows)
        if not rows:
            preview.blocker = "No accounts are publishing to shared memory."
            return preview

        by_key = {row.key: row for row in rows}
        receiver_row = by_key.get(self._receiver_key)
        donor_rows = [by_key[key] for key in self._donor_keys if key in by_key and key != self._receiver_key]

        if receiver_row is None:
            preview.blocker = "Pick a receiver."
            return preview
        if not donor_rows:
            preview.blocker = "Select at least one donor."
            return preview

        policy = self._policy()
        # This client coordinates. Falling back to the receiver only matters if the
        # local account is missing from its own shared memory, which would make the
        # reachability check vacuous -- but by then nothing else here works either.
        coordinator_row = next((row for row in rows if row.is_local), receiver_row)

        preview.rally = receiver_row.position
        preview.prechecks = InventoryTransfer.precheck_session(
            coordinator_row.state,
            receiver_row.state,
            [row.state for row in donor_rows],
            policy,
        )

        preview.session = InventoryTransfer.plan_session(
            receiver_row.state.inventory,
            [row.state.as_donor() for row in donor_rows],
            policy,
            max_rounds=self._max_rounds,
        )

        excluded: list[tuple[str, InventoryTransfer.ItemRecord, InventoryTransfer.EligibilityResult]] = []
        for row in donor_rows:
            items = row.state.inventory.items()
            preview.plannable_by_donor[row.key] = len(InventoryTransfer.select_plannable(items, policy))
            for item, result in InventoryTransfer.explain_exclusions(items, policy):
                excluded.append((row.label, item, result))
        preview.excluded = tuple(excluded)

        return preview

    def _refresh(self, force: bool = False) -> None:
        if not (force or self._dirty or self._refresh_timer.IsExpired()):
            return
        self._refresh_timer.Reset()
        self._dirty = False
        try:
            self._preview = self._compute()
        except Exception as exc:
            # Keep the last account list: losing the forecast is survivable, losing
            # the picker leaves the user unable to change the selection that broke it.
            self._preview = TransferPreview(
                rows=self._preview.rows, blocker="Could not build a preview: %s" % exc
            )
            ConsoleLog(MODULE_NAME, "Preview failed: %s" % exc, Console.MessageType.Error, False)

    # -- selection ----------------------------------------------------------

    def _set_receiver(self, key: str, cfg: Settings) -> None:
        self._receiver_key = key
        if key in self._donor_keys:
            self._donor_keys.remove(key)
        self._store(cfg)

    def _toggle_donor(self, key: str, wanted: bool, cfg: Settings) -> None:
        if wanted and key not in self._donor_keys:
            self._donor_keys.append(key)
        elif not wanted and key in self._donor_keys:
            self._donor_keys.remove(key)
        self._store(cfg)

    # -- live session -------------------------------------------------------

    def tick(self) -> None:
        """Advance a running transfer. Called from the module's `update()`."""
        self._session.tick()

    def _blocked_reason(self) -> str:
        """Why Run is refused, or an empty string when it is allowed."""
        if self._session.running:
            return "a transfer is already running"

        preview = self._preview
        if preview.session is None or not preview.prechecks:
            return preview.blocker or "there is no plan yet"
        if not InventoryTransfer.precheck_passed(preview.prechecks):
            return "the precheck has not passed"
        if preview.session.item_count == 0:
            return "the plan would move nothing"

        receiver = next((row for row in preview.rows if row.key == self._receiver_key), None)
        if receiver is None:
            return "the receiver is not publishing to shared memory"
        if not receiver.is_local:
            # Pinned deliberately (plan section 11, question 1). A free-floating
            # coordinator bought a second set of states and a second way for the
            # rally point to be wrong, and paid for neither.
            return "the receiver must be this client: the rally point is where this character stands"
        return ""

    def _start_session(self) -> None:
        donors = [key for key in self._donor_keys if any(row.key == key for row in self._preview.rows)]
        self._session_id = uuid4().hex[:8]
        self._session.start(
            session_id=self._session_id,
            coordinator_key=str(Player.GetAccountEmail() or ""),
            receiver_key=self._receiver_key,
            donor_keys=donors,
            policy=self._policy(),
            wire_policy=self._wire_policy,
            pickup_radius=self._pickup_radius,
            rally=Player.GetXY(),
            max_rounds=self._max_rounds,
            pause_donor_widgets=self._pause_donor_widgets,
        )

    # -- drawing ------------------------------------------------------------

    def draw(self) -> None:
        cfg = self._config()
        if cfg is None:
            return
        if not self._loaded:
            self._load(cfg)

        self._refresh()

        if ImGui.Begin(self._ini_key, MODULE_NAME, flags=PyImGui.WindowFlags.NoFlag):
            self._draw_header()
            if PyImGui.collapsing_header("Participants", HEADER_FLAGS):
                self._draw_participants(cfg)
            if PyImGui.collapsing_header("Policy", HEADER_FLAGS):
                self._draw_policy(cfg)
            if PyImGui.collapsing_header("Precheck", HEADER_FLAGS):
                self._draw_precheck()
            if PyImGui.collapsing_header("Run", HEADER_FLAGS):
                self._draw_run()
            if PyImGui.collapsing_header("Preview", HEADER_FLAGS):
                self._draw_preview()
        ImGui.End(self._ini_key)

    def _draw_header(self) -> None:
        if self._session.running:
            PyImGui.text_colored(
                "Transfer running -- round %d, %s." % (self._session.round_id, self._session.phase),
                COLOR_WARN,
            )
        else:
            PyImGui.text_colored("Everything below the Run panel is a forecast, not a receipt.", COLOR_TITLE)
        PyImGui.text_wrapped(
            "The plan is computed from the shared-memory snapshot, which lags live inventories by up to "
            "1.5 seconds. Only the Run panel sends anything; each participant re-reads its own bags "
            "before acting on what it is asked to do."
        )
        PyImGui.separator()
        PyImGui.text("Session id: %s" % self._session_id)
        PyImGui.same_line(0, 10)
        if PyImGui.small_button("New id##inventory_transfer_new_session"):
            self._session_id = uuid4().hex[:8]
        PyImGui.same_line(0, 10)
        if PyImGui.small_button("Recompute##inventory_transfer_recompute"):
            self._refresh(force=True)
        PyImGui.separator()

    def _draw_participants(self, cfg: Settings) -> None:
        rows = self._preview.rows
        if not rows:
            # Carries the refresh failure too, so a broken snapshot read says why
            # rather than looking like an empty multibox.
            PyImGui.text_colored(self._preview.blocker or "No accounts are publishing to shared memory.", COLOR_WARN)
            return

        if PyImGui.small_button("All as donors##inventory_transfer_all_donors"):
            self._donor_keys = [row.key for row in rows if row.key != self._receiver_key]
            self._store(cfg)
        PyImGui.same_line(0, 8)
        if PyImGui.small_button("No donors##inventory_transfer_no_donors"):
            self._donor_keys = []
            self._store(cfg)

        receiver_index = next((index for index, row in enumerate(rows) if row.key == self._receiver_key), -1)

        if not PyImGui.begin_table("##inventory_transfer_participants", 9, TABLE_FLAGS):
            return

        PyImGui.table_setup_column("Recv")
        PyImGui.table_setup_column("Give")
        PyImGui.table_setup_column("Character")
        PyImGui.table_setup_column("Account")
        PyImGui.table_setup_column("Map")
        PyImGui.table_setup_column("Party")
        PyImGui.table_setup_column("Items held")
        PyImGui.table_setup_column("Free slots")
        PyImGui.table_setup_column("State")
        PyImGui.table_headers_row()

        for index, row in enumerate(rows):
            state = row.state
            PyImGui.table_next_row()

            PyImGui.table_set_column_index(0)
            chosen = PyImGui.radio_button("##inventory_transfer_receiver_%d" % index, receiver_index, index)
            if chosen != receiver_index:
                self._set_receiver(rows[chosen].key, cfg)

            PyImGui.table_set_column_index(1)
            is_receiver = row.key == self._receiver_key
            PyImGui.begin_disabled(is_receiver)
            selected = PyImGui.checkbox("##inventory_transfer_donor_%d" % index, row.key in self._donor_keys)
            PyImGui.end_disabled()
            if not is_receiver and selected != (row.key in self._donor_keys):
                self._toggle_donor(row.key, selected, cfg)

            PyImGui.table_set_column_index(2)
            PyImGui.text("%s%s" % (state.name or "-", " (this client)" if row.is_local else ""))

            PyImGui.table_set_column_index(3)
            PyImGui.text(row.key)

            PyImGui.table_set_column_index(4)
            explorable = state.explorable
            if explorable is True:
                PyImGui.text(row.map_name)
            elif explorable is False:
                PyImGui.text_colored("%s (outpost)" % row.map_name, COLOR_FAIL)
            else:
                PyImGui.text_colored("%s (unknown type)" % row.map_name, COLOR_WARN)

            PyImGui.table_set_column_index(5)
            PyImGui.text(str(state.party_id))

            PyImGui.table_set_column_index(6)
            PyImGui.text(str(state.inventory.used_slots))

            PyImGui.table_set_column_index(7)
            if row.capacity_is_trusted:
                PyImGui.text("%d of %d" % (state.inventory.free_slots, state.inventory.capacity))
            else:
                PyImGui.text_colored("not published", COLOR_MUTED)

            PyImGui.table_set_column_index(8)
            if not state.alive:
                PyImGui.text_colored("dead", COLOR_FAIL)
            elif state.in_aggro:
                PyImGui.text_colored("in aggro", COLOR_FAIL)
            else:
                plannable = self._preview.plannable_by_donor.get(row.key)
                if plannable is None:
                    PyImGui.text_colored("ok", COLOR_MUTED)
                else:
                    PyImGui.text("%d eligible" % plannable)

        PyImGui.end_table()

        PyImGui.text_colored(
            "Free slots are read directly from this client's bags. Shared memory does not publish a "
            "usable bag capacity for other accounts, so theirs are not shown -- and are not needed: "
            "the receiver is always this client, and only its free slots set the round budget.",
            COLOR_MUTED,
        )

    def _draw_policy(self, cfg: Settings) -> None:
        PyImGui.text_colored("Wire policy: what the donors enforce", COLOR_TITLE)
        current = WIRE_POLICY_NAMES.index(self._wire_policy) if self._wire_policy in WIRE_POLICY_NAMES else 0
        chosen = PyImGui.combo("##inventory_transfer_wire_policy", current, list(WIRE_POLICY_NAMES))
        if chosen != current and 0 <= chosen < len(WIRE_POLICY_NAMES):
            self._wire_policy = WIRE_POLICY_NAMES[chosen]
            self._store(cfg)
        PyImGui.text_wrapped(WIRE_POLICY_SUMMARY.get(self._wire_policy, ""))
        if self._wire_policy == "optimistic":
            PyImGui.text_colored(
                "Optimistic merging rests on stack behaviour nobody has confirmed on a live client yet.",
                COLOR_WARN,
            )

        PyImGui.separator()
        PyImGui.text_colored("Coordinator budget", COLOR_TITLE)

        margin = PyImGui.input_int("Safety margin (slots)##inventory_transfer_margin", self._safety_margin)
        margin = max(0, min(margin, 20))
        if margin != self._safety_margin:
            self._safety_margin = margin
            self._store(cfg)

        rounds = PyImGui.input_int("Forecast round limit##inventory_transfer_rounds", self._max_rounds)
        rounds = max(1, min(rounds, 64))
        if rounds != self._max_rounds:
            self._max_rounds = rounds
            self._store(cfg)

        radius = PyImGui.input_int("Pickup radius##inventory_transfer_radius", self._pickup_radius)
        radius = max(0, radius)
        if radius != self._pickup_radius:
            self._pickup_radius = radius
            self._store(cfg)

        PyImGui.text_colored(
            "Safety margin and round limit are coordinator-side only; neither crosses the wire.",
            COLOR_MUTED,
        )

        PyImGui.separator()
        PyImGui.text_colored("Stacks", COLOR_TITLE)

        stack = PyImGui.input_int("Largest stack to move##inventory_transfer_stack", self._max_stack)
        stack = max(1, min(stack, InventoryTransfer.MAX_STACK_QUANTITY))
        if stack != self._max_stack:
            self._max_stack = stack
            self._store(cfg)

        if self._max_stack <= 1:
            PyImGui.text_colored("Unstacked items only. Any stack of two or more stays put.", COLOR_MUTED)
        else:
            PyImGui.text_colored(
                "A stack of %d costs %d drop call(s) and lands as %d separate ground pile(s)."
                % (self._max_stack, self._max_stack, self._max_stack),
                COLOR_WARN,
            )
        PyImGui.text_wrapped(
            "The native drop binding moves one unit per call whatever quantity it is given, so a stack "
            "has to be dropped one item at a time. A donor refuses any stack over this limit whole and "
            "untouched rather than starting one it cannot finish. This is the one policy value that "
            "does reach the donors."
        )

        PyImGui.separator()
        PyImGui.text_colored("Live run", COLOR_TITLE)

        pause = PyImGui.checkbox("Pause optional widgets on donors##inventory_transfer_pause", self._pause_donor_widgets)
        if pause != self._pause_donor_widgets:
            self._pause_donor_widgets = pause
            self._store(cfg)
        PyImGui.text_colored(
            "Keeps AutoInventoryHandler and LootEx from salvaging or depositing an item already queued "
            "to be dropped. Donors only -- pausing this client would stop the session mid-flight.",
            COLOR_MUTED,
        )
        PyImGui.text_wrapped(
            "Hero AI is suspended for the whole run on every account in this instance, not just the "
            "donors: a party member nobody selected still loots, and would race you to the pile. It is "
            "restored when the run ends, aborts or fails, and lapses on its own if this client dies."
        )

        PyImGui.separator()
        PyImGui.text_colored("Preview-only refinements", COLOR_TITLE)
        PyImGui.text_wrapped(
            "A drop message carries a policy name, not a policy. These two lists change the budget this "
            "preview computes, but a donor filters by the wire policy above and will not honour them. "
            "Treat them as planning aids until the protocol can distribute a policy."
        )

        protected = PyImGui.input_text("Protected model ids##inventory_transfer_protected", self._protected_text)
        if protected != self._protected_text:
            self._protected_text = protected
            self._store(cfg)
        models = InventoryTransfer.parse_model_list(self._protected_text)
        if models:
            PyImGui.text_colored(
                "Protected: %s" % ", ".join(_model_name(model) for model in models),
                COLOR_MUTED,
            )

        floors = PyImGui.input_text("Quantity floors (model:keep)##inventory_transfer_floors", self._floors_text)
        if floors != self._floors_text:
            self._floors_text = floors
            self._store(cfg)
        parsed_floors = InventoryTransfer.parse_model_floors(self._floors_text)
        if parsed_floors:
            PyImGui.text_colored(
                "Floors: %s" % ", ".join("%s >= %d" % (_model_name(model), floor) for model, floor in parsed_floors),
                COLOR_MUTED,
            )

    def _draw_precheck(self) -> None:
        results = self._preview.prechecks
        if not results:
            PyImGui.text_colored(
                self._preview.blocker or "Nothing to check yet.", COLOR_MUTED
            )
            return

        if InventoryTransfer.precheck_passed(results):
            PyImGui.text_colored("A live run would be allowed to start.", COLOR_OK)
        else:
            PyImGui.text_colored("A live run would refuse to start.", COLOR_FAIL)

        if not PyImGui.begin_table("##inventory_transfer_precheck", 3, TABLE_FLAGS):
            return

        PyImGui.table_setup_column("State")
        PyImGui.table_setup_column("Check")
        PyImGui.table_setup_column("Detail")
        PyImGui.table_headers_row()

        for result in results:
            PyImGui.table_next_row()
            PyImGui.table_set_column_index(0)
            if result.passed:
                PyImGui.text_colored("pass", COLOR_OK)
            elif result.unresolved:
                PyImGui.text_colored("unknown", COLOR_WARN)
            else:
                PyImGui.text_colored("fail", COLOR_FAIL)

            PyImGui.table_set_column_index(1)
            PyImGui.text(result.name)

            PyImGui.table_set_column_index(2)
            PyImGui.text(result.detail or "-")

        PyImGui.end_table()

    def _draw_run(self) -> None:
        """The one panel in this widget that puts messages on the wire."""
        session = self._session
        blocked = self._blocked_reason()

        if session.running:
            PyImGui.text_colored("Round %d -- %s" % (session.round_id, session.phase), COLOR_TITLE)
        elif session.finished:
            colour = COLOR_FAIL if session.error else COLOR_OK
            PyImGui.text_colored(
                "Finished after %d round(s): %d item(s) collected. Stopped because %s."
                % (len(session.history), session.items_moved, session.stop_reason or "the plan ran out"),
                colour,
            )
        else:
            PyImGui.text_colored("Idle.", COLOR_MUTED)

        PyImGui.begin_disabled(bool(blocked))
        if PyImGui.button("Run transfer##inventory_transfer_run"):
            self._start_session()
        PyImGui.end_disabled()

        PyImGui.same_line(0, 8)
        PyImGui.begin_disabled(not session.running)
        if PyImGui.button("Abort##inventory_transfer_abort"):
            session.abort()
        PyImGui.end_disabled()

        PyImGui.same_line(0, 8)
        if PyImGui.button("Force restore hero AI##inventory_transfer_resume_donors"):
            # Escape hatch for a session that died with this widget disabled, or a
            # client restarted mid-transfer: the lease would expire eventually, and
            # this is for someone who does not want to wait for it.
            session.resume_donors()

        if blocked:
            PyImGui.text_colored("Cannot run: %s." % blocked, COLOR_WARN)
        elif not session.running:
            plan = self._preview.session
            first = plan.rounds[0] if plan is not None and plan.rounds else None
            if first is not None:
                PyImGui.text_wrapped(
                    "Ready: %d donor(s) will drop %d item(s) as %d ground pile(s) at this character's "
                    "position, under policy '%s' with stacks up to %d."
                    % (
                        len(first.donor_keys),
                        first.item_count,
                        first.ground_item_count,
                        self._wire_policy,
                        self._max_stack,
                    )
                )

        PyImGui.text_colored(
            "Donors enforce the wire policy and the stack limit from their own live bag read. The "
            "protected models and quantity floors below shape this preview only.",
            COLOR_MUTED,
        )

        if session.error:
            PyImGui.text_colored("Error: %s" % session.error, COLOR_FAIL)

        if session.needs_attention:
            PyImGui.separator()
            PyImGui.text_colored(
                "%d item(s) are still on the ground at the rally point." % session.leftover_on_ground,
                COLOR_FAIL,
            )
            PyImGui.text_wrapped(
                "They will die with the instance if nobody takes them. Recall asks each donor to collect "
                "what is at its feet, which works while the donors are still standing at the rally point. "
                "Walking this character over the pile also works."
            )
            if PyImGui.button("Recall to donors##inventory_transfer_recall"):
                session.recall()
            PyImGui.same_line(0, 8)
            if PyImGui.button("Dismiss##inventory_transfer_dismiss"):
                session.needs_attention = False

        if session.history:
            self._draw_progress(session)

    def _draw_progress(self, session: TransferSession) -> None:
        PyImGui.separator()
        PyImGui.text_colored("Rounds run", COLOR_TITLE)

        if not PyImGui.begin_table("##inventory_transfer_progress", 6, TABLE_FLAGS):
            return

        PyImGui.table_setup_column("Round")
        PyImGui.table_setup_column("Planned")
        PyImGui.table_setup_column("Piles")
        PyImGui.table_setup_column("Dropped")
        PyImGui.table_setup_column("Collected")
        PyImGui.table_setup_column("Left")
        PyImGui.table_headers_row()

        for record in session.history[-MAX_LIST_ROWS:]:
            PyImGui.table_next_row()
            PyImGui.table_set_column_index(0)
            PyImGui.text(str(record.round_id))
            PyImGui.table_set_column_index(1)
            PyImGui.text(str(record.planned_items))
            PyImGui.table_set_column_index(2)
            PyImGui.text(str(record.planned_piles))
            PyImGui.table_set_column_index(3)
            PyImGui.text(str(record.dropped))
            PyImGui.table_set_column_index(4)
            PyImGui.text(str(record.collected))
            PyImGui.table_set_column_index(5)
            if record.on_ground:
                PyImGui.text_colored(str(record.on_ground), COLOR_WARN)
            else:
                PyImGui.text("0")

        PyImGui.end_table()

        notes = [record for record in session.history if record.note]
        for record in notes[-MAX_LIST_ROWS:]:
            PyImGui.text_colored("Round %d: %s" % (record.round_id, record.note), COLOR_WARN)

    def _draw_preview(self) -> None:
        session = self._preview.session
        if session is None:
            PyImGui.text_colored(self._preview.blocker or "No plan yet.", COLOR_MUTED)
            return

        slots = sum(plan.slots_committed for plan in session.rounds)
        piles = sum(plan.ground_item_count for plan in session.rounds)
        PyImGui.text(
            "%d round(s), %d item(s), %d ground pile(s), %d receiver slot(s)."
            % (session.round_count, session.item_count, piles, slots)
        )
        if piles > session.item_count:
            PyImGui.text_colored(
                "More piles than items: a stack is dropped one unit at a time, and each unit is its own "
                "pile for the receiver to walk to.",
                COLOR_MUTED,
            )
        PyImGui.text_colored("Stops because: %s." % session.stop_reason, COLOR_MUTED)

        if session.item_count == 0:
            PyImGui.text_colored(
                "Nothing would move. Check the exclusions and the receiver's free slots.",
                COLOR_WARN,
            )

        self._draw_round_messages(session)
        self._draw_round_plan(session)
        self._draw_leftover(session)
        self._draw_exclusions()

    def _draw_round_messages(self, session: InventoryTransfer.SessionPlan) -> None:
        """The exact messages round 1 would put on the wire, as text only.

        This is the review gate's sharpest tool: the per-donor `max_items` here
        is the whole instruction a donor receives, since the coordinator cannot
        address items by id.
        """
        if not session.rounds:
            return

        PyImGui.separator()
        PyImGui.text_colored("Round 1 messages, exactly as Run would send them", COLOR_TITLE)

        first = session.rounds[0]
        rally_x, rally_y = self._preview.rally

        if not PyImGui.begin_table("##inventory_transfer_messages", 4, TABLE_FLAGS):
            return

        PyImGui.table_setup_column("To")
        PyImGui.table_setup_column("Command")
        PyImGui.table_setup_column("Params")
        PyImGui.table_setup_column("ExtraData")
        PyImGui.table_headers_row()

        for donor_key in first.donor_keys:
            PyImGui.table_next_row()
            PyImGui.table_set_column_index(0)
            PyImGui.text(donor_key)
            PyImGui.table_set_column_index(1)
            PyImGui.text("TransferDropItems")
            PyImGui.table_set_column_index(2)
            PyImGui.text(
                "(%d, %d, %.0f, %.0f)" % (first.round_id, first.budget_for(donor_key), rally_x, rally_y)
            )
            PyImGui.table_set_column_index(3)
            PyImGui.text(
                '("%s", "%s", "%s", "")'
                % (
                    self._wire_policy,
                    self._session_id,
                    InventoryTransfer.format_stack_limit(self._max_stack),
                )
            )

        PyImGui.table_next_row()
        PyImGui.table_set_column_index(0)
        PyImGui.text(self._receiver_key)
        PyImGui.table_set_column_index(1)
        PyImGui.text("TransferPickUpItems")
        PyImGui.table_set_column_index(2)
        # Ground piles, not planned items: a stack of three is dropped one unit at
        # a time and lands as three piles, and a budget in items would tell the
        # receiver to collect one of them and walk away.
        PyImGui.text("(%d, %d, %d, 0)" % (first.round_id, first.ground_item_count, self._pickup_radius))
        PyImGui.table_set_column_index(3)
        PyImGui.text('("%s", "", "", "")' % self._session_id)

        PyImGui.end_table()

        PyImGui.text_colored(
            "Cost: %d message(s) out, %d report(s) back." % (len(first.donor_keys) + 1, len(first.donor_keys) + 1),
            COLOR_MUTED,
        )

    def _draw_round_plan(self, session: InventoryTransfer.SessionPlan) -> None:
        if not session.rounds:
            return

        PyImGui.separator()
        PyImGui.text_colored("Planned rounds", COLOR_TITLE)

        if not PyImGui.begin_table("##inventory_transfer_rounds", 5, TABLE_FLAGS):
            return

        PyImGui.table_setup_column("Round")
        PyImGui.table_setup_column("Donor")
        PyImGui.table_setup_column("Item")
        PyImGui.table_setup_column("Qty")
        PyImGui.table_setup_column("Slots")
        PyImGui.table_headers_row()

        shown = 0
        for plan in session.rounds:
            for drop in plan.drops:
                if shown >= MAX_PLAN_ROWS:
                    break
                shown += 1
                PyImGui.table_next_row()
                PyImGui.table_set_column_index(0)
                PyImGui.text(str(plan.round_id))
                PyImGui.table_set_column_index(1)
                PyImGui.text(drop.donor_key)
                PyImGui.table_set_column_index(2)
                PyImGui.text(_model_name(drop.item.model_id))
                PyImGui.table_set_column_index(3)
                PyImGui.text(str(drop.item.effective_quantity))
                PyImGui.table_set_column_index(4)
                PyImGui.text(str(drop.slot_cost))

        PyImGui.end_table()

        if session.item_count > shown:
            PyImGui.text_colored(
                "%d more planned item(s) not listed." % (session.item_count - shown),
                COLOR_MUTED,
            )

    def _draw_leftover(self, session: InventoryTransfer.SessionPlan) -> None:
        if not session.leftover:
            return

        PyImGui.separator()
        PyImGui.text_colored("Left behind after the last round", COLOR_WARN)
        PyImGui.text_wrapped(
            "These stay in their donor's bags because the receiver runs out of room first. They are not "
            "dropped, so nothing is at risk of dying with the instance."
        )

        totals: dict[str, int] = {}
        for donor_key, item in session.leftover:
            label = "%s: %s" % (donor_key, _model_name(item.model_id))
            totals[label] = totals.get(label, 0) + item.effective_quantity

        for index, (label, quantity) in enumerate(sorted(totals.items())):
            if index >= MAX_LIST_ROWS:
                PyImGui.text_colored(
                    "%d more line(s) not listed." % (len(totals) - MAX_LIST_ROWS), COLOR_MUTED
                )
                break
            PyImGui.bullet_text("%s x%d" % (label, quantity))

    def _draw_exclusions(self) -> None:
        excluded = self._preview.excluded
        if not excluded:
            return

        PyImGui.separator()
        PyImGui.text_colored("Excluded by policy", COLOR_TITLE)
        PyImGui.text_wrapped(
            "Only items the coordinator can prove are ineligible. Tradability and customization are "
            "invisible in the snapshot, so a donor's own read will exclude more than this."
        )

        if not PyImGui.begin_table("##inventory_transfer_excluded", 4, TABLE_FLAGS):
            return

        PyImGui.table_setup_column("Donor")
        PyImGui.table_setup_column("Item")
        PyImGui.table_setup_column("Qty")
        PyImGui.table_setup_column("Reason")
        PyImGui.table_headers_row()

        for index, (donor_label, item, result) in enumerate(excluded):
            if index >= MAX_LIST_ROWS:
                break
            PyImGui.table_next_row()
            PyImGui.table_set_column_index(0)
            PyImGui.text(donor_label)
            PyImGui.table_set_column_index(1)
            PyImGui.text(_model_name(item.model_id))
            PyImGui.table_set_column_index(2)
            PyImGui.text(str(item.effective_quantity))
            PyImGui.table_set_column_index(3)
            PyImGui.text(result.reason or "-")

        PyImGui.end_table()

        if len(excluded) > MAX_LIST_ROWS:
            PyImGui.text_colored(
                "%d more excluded item(s) not listed." % (len(excluded) - MAX_LIST_ROWS),
                COLOR_MUTED,
            )


WIDGET_INSTANCE = InventoryTransferWidget()


def update() -> None:
    """Per-frame non-UI callback: advances a running transfer.

    Separate from `main()` on purpose. A session that only ticked while its
    window was drawn would stall the moment the user collapsed the widget --
    halfway through a round, with items on the ground and donor widgets paused.
    """
    try:
        if not Routines.Checks.Map.MapValid():
            return
        WIDGET_INSTANCE.tick()
    except Exception as exc:
        ConsoleLog(MODULE_NAME, "Session tick failed: %s" % exc, Console.MessageType.Error, False)


def main() -> None:
    """Per-frame UI callback."""
    try:
        if not Routines.Checks.Map.MapValid():
            return
        WIDGET_INSTANCE.draw()
    except Exception as exc:
        ConsoleLog(MODULE_NAME, "Draw failed: %s" % exc, Console.MessageType.Error, False)


def tooltip() -> None:
    PyImGui.begin_tooltip()
    ImGui.push_font("Regular", 20)
    PyImGui.text_colored(MODULE_NAME, COLOR_TITLE)
    ImGui.pop_font()
    PyImGui.spacing()
    PyImGui.separator()
    PyImGui.text_wrapped(
        "Runs a drop-and-collect item ferry between multiboxed accounts: donors drop at a rally point "
        "and this character walks the pile. Plans the whole session first, then runs it round by round."
    )
    PyImGui.spacing()
    PyImGui.text_colored("Features:", COLOR_TITLE)
    PyImGui.bullet_text("Receiver picker and donor selection across every account in shared memory.")
    PyImGui.bullet_text("Named wire policy, a stack limit the donors honour, and budget settings.")
    PyImGui.bullet_text("Precheck panel reporting each session condition separately.")
    PyImGui.bullet_text("Round-by-round preview, the exact round 1 messages, leftovers and exclusions.")
    PyImGui.bullet_text("Live run with abort, per-round progress, and Recall for anything left behind.")
    PyImGui.end_tooltip()


if __name__ == "__main__":
    main()
