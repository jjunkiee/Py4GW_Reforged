"""Cross-account inventory transfer -- participant selection, policy, and dry-run preview.

Phase 4 of `docs/loot/plans/cross-account-inventory-transfer.md`. This widget is
the review gate: it composes the shared-memory snapshot, the phase 1 planner and
the phase 3 message vocabulary into the exact session a live run would perform,
and then does not perform it.

**Nothing here sends a message.** There is deliberately no `SendMessage` call in
this file, and no `SharedCommandType` is ever handed to the transport -- the
command names appear only as text in the message preview. Phase 5 adds run,
abort, recall, progress and the settle warning on top of what this shows.

What that buys: the slot budget, the per-donor drop counts, the leftover pile and
the precheck verdicts are all readable before a single item leaves a bag, and the
arithmetic behind them is proved offline in
`Examples and tests/tests/test_inventory_transfer_planner.py` rather than on the
user's inventory.

Two limitations the UI states out loud rather than hiding:

* The drop message carries a policy *name*, not a policy. A donor resolves that
  name through `inventory_transfer.resolve_policy()` against its own build, so
  the coordinator-side refinements below -- protected models, quantity floors --
  shape this preview's budget but are not what a donor enforces. Until the
  protocol grows a way to distribute a policy they are planning aids, not
  protection.
* The snapshot is up to `SHMEM_PLAYER_INVENTORY_UPDATE_THROTTLE_MS` (1.5 s)
  stale, so the preview is a forecast. That is good enough to refuse a session
  and never good enough to authorise one; each participant re-reads its own
  state before acting.
"""

from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
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
        self._protected_text: str = ""
        self._floors_text: str = ""

        self._session_id: str = uuid4().hex[:8]
        self._preview: TransferPreview = TransferPreview()
        self._refresh_timer: ThrottledTimer = ThrottledTimer(REFRESH_INTERVAL_MS)
        self._dirty: bool = True

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
        cfg.set(SECTION_POLICY, "protected_models", self._protected_text)
        cfg.set(SECTION_POLICY, "model_floors", self._floors_text)
        self._dirty = True

    # -- planning inputs ----------------------------------------------------

    def _policy(self) -> InventoryTransfer.TransferPolicy:
        """The policy this preview budgets with.

        The wire policy is the part a donor honours; the rest is coordinator-side
        and is labelled as such in the UI. `safety_margin` is honestly local --
        it shapes the budget and never crosses the wire at all.
        """
        base = InventoryTransfer.resolve_policy(self._wire_policy)
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
            if PyImGui.collapsing_header("Preview", HEADER_FLAGS):
                self._draw_preview()
        ImGui.End(self._ini_key)

    def _draw_header(self) -> None:
        PyImGui.text_colored("Dry run only: this widget never sends a message.", COLOR_TITLE)
        PyImGui.text_wrapped(
            "Everything below is computed from the shared-memory snapshot, which lags live inventories "
            "by up to 1.5 seconds. Treat it as a forecast, not a receipt."
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

    def _draw_preview(self) -> None:
        session = self._preview.session
        if session is None:
            PyImGui.text_colored(self._preview.blocker or "No plan yet.", COLOR_MUTED)
            return

        slots = sum(plan.slots_committed for plan in session.rounds)
        PyImGui.text(
            "%d round(s), %d item(s), %d receiver slot(s)." % (session.round_count, session.item_count, slots)
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
        PyImGui.text_colored("Round 1 messages (not sent)", COLOR_TITLE)

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
            PyImGui.text('("%s", "%s", "", "")' % (self._wire_policy, self._session_id))

        PyImGui.table_next_row()
        PyImGui.table_set_column_index(0)
        PyImGui.text(self._receiver_key)
        PyImGui.table_set_column_index(1)
        PyImGui.text("TransferPickUpItems")
        PyImGui.table_set_column_index(2)
        PyImGui.text("(%d, %d, %d, 0)" % (first.round_id, first.item_count, self._pickup_radius))
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
        "Plans a drop-and-collect item ferry between multiboxed accounts: donors drop whole stacks at a "
        "rally point and one receiver walks the pile. This is the dry-run half."
    )
    PyImGui.spacing()
    PyImGui.text_colored("Features:", COLOR_TITLE)
    PyImGui.bullet_text("Receiver picker and donor selection across every account in shared memory.")
    PyImGui.bullet_text("Named wire policy plus coordinator-side budget settings.")
    PyImGui.bullet_text("Precheck panel reporting each session condition separately.")
    PyImGui.bullet_text("Round-by-round preview, the exact round 1 messages, leftovers and exclusions.")
    PyImGui.bullet_text("Sends nothing: no message leaves this client.")
    PyImGui.end_tooltip()


if __name__ == "__main__":
    main()
