"""Estimated per-foe Energy deficit, for skills that pay off against a drained foe.

The client does not publish foe Energy. ``Agent.GetEnergy`` and
``Agent.GetMaxEnergy`` are documented as reliable only for the player and their
heroes, and in-client observation confirms it for enemies: ``energy`` reads
``0.000`` with a zeroed pool and never moves, however hard the foe is casting.
Skills whose value scales with a foe's *missing* Energy therefore have no ground
truth to gate on.

This module keeps a deliberately narrow substitute: a ledger of Energy a foe is
believed to be missing, from two sources.

1. :func:`record_drain` -- Energy this build removed on purpose, recorded by the
   skill helper that removed it. Precise: we know the skill and the rank.
2. :func:`observe_enemy_cast` -- Energy a foe spent casting its own skills,
   billed at the skill's base cost the moment the cast is observed starting.

The second source is the larger of the two in practice, because foes cast far
more often than we drain them. It is billed at activation rather than on
completion because Energy in GW1 is spent when activation begins: an interrupted
cast still costs the caster. That also makes it cheap to observe, since the
existing sampler already sees every cast start.

The estimate is dead reckoning, and this docstring would rather say so than let
a caller mistake it for a measurement:

- Entries decay at an assumed enemy regeneration rate and expire outright, so a
  stale drain cannot keep a gate open forever.
- The total is clamped to a plausible pool size. Without a readable pool there
  is nothing to stop accumulated spend claiming a foe is missing more Energy
  than it could ever have held.
- Base costs ignore the foe's own reductions, so per-cast billing is
  approximate.
- GW1 recycles agent ids between zones, so the whole ledger is dropped on a map
  change. Reusing a previous zone's deficit against a fresh agent in the same
  id slot would be worse than having no ledger at all.

Nothing ever corrects the estimate against the foe's real Energy bar, because
there is no readable bar to correct it against. Treat the result as a
"has been spending Energy and we have not seen it recover" signal.
"""

from __future__ import annotations

import time

__all__ = [
    "ANEURYSM_DEFAULT_MIN_DEFICIT",
    "aneurysm_adjacent_drain_cap",
    "clear_agent",
    "clear_all",
    "estimated_deficit",
    "linear_drain",
    "ms_since_last_event",
    "observe_enemy_cast",
    "record_drain",
    "recorded_event_count",
    "tracked_agent_ids",
]


# Enemy Energy regeneration assumed while decaying a recorded drain. Two pips
# (0.33 Energy/second each) is the common baseline; casters often regenerate
# faster. Assuming the low end would keep deficits alive too long and overstate
# the case for casting, so this errs toward forgetting sooner.
ASSUMED_ENEMY_ENERGY_REGEN_PER_SEC: float = 0.66

# Hard age-out. At the assumed regeneration rate even a large drain is long
# repaid by this point; the cap just stops the ledger growing without bound
# when a foe is drained and then never seen again.
DRAIN_MAX_AGE_MS: float = 30 * 1000.0  # 30 seconds

# Ceiling on a reported deficit. A foe cannot be missing more Energy than its
# pool ever held, and with no readable pool the accumulated cast spend would
# otherwise grow without limit over a long fight. Generous on purpose: it is a
# sanity clamp, not a model of any particular foe.
ASSUMED_MAX_ENEMY_POOL: float = 50.0

# A cast is billed once per (agent, skill) within this window. The sampler
# infers casting from a per-frame state read, so a flicker on that read must not
# bill the same cast twice. No real skill re-activates inside this window.
OBSERVED_CAST_DEDUPE_MS: float = 1000.0

# agent_id -> list of (recorded_at_monotonic_ms, energy_points_removed)
_drains: dict[int, list[tuple[float, float]]] = {}

# (agent_id, skill_id) -> monotonic ms that pair was last billed.
_observed_casts: dict[tuple[int, int], float] = {}

# Map the ledger was built on. GW1 reuses agent ids across zones.
_ledger_map_id: int = 0

# Liveness counters for the diagnostic view. Deliberately process-lifetime
# rather than per-map: their job is to separate "nothing has fed this ledger"
# from "foes simply are not spending Energy", and a counter that resets on
# every zone cannot answer the first question.
_recorded_event_count: int = 0
_last_event_ms: float | None = None


# --- Aneurysm ------------------------------------------------------------
#
# Aneurysm's numbers live here rather than beside its helper for two reasons:
# this module is already where "how much Energy does skill X move" is answered
# (see linear_drain), and it is the one place both the skill helper and the
# in-client debug view can reach -- including the offline harness, which cannot
# import anything that pulls in Py4GWCoreLib.

# Deficit at which Aneurysm is worth casting, in Energy points. Tuning, not a
# game fact. Single-sourced so the debug view cannot drift from the gate.
ANEURYSM_DEFAULT_MIN_DEFICIT: float = 15.0

# Ceiling on the Energy each adjacent foe loses, by Domination Magic rank.
# The published progression is not a clean formula -- rank 8 breaks the
# 2*rank+1 run that covers ranks 0-7 -- so this is the literal table rather
# than an invented expression. Cross-checks against the skill description's
# own "(Maximum 1...24...30)": rank 0 -> 1, rank 12 -> 24, rank 15 -> 30.
ANEURYSM_MAX_ADJACENT_DRAIN_BY_RANK: tuple[int, ...] = (
    1,
    3,
    5,
    7,
    9,
    11,
    13,
    15,
    16,
    18,
    20,
    22,
    24,
    26,
    28,
    30,
    32,
    34,
    36,
    38,
    40,
    42,
)


def aneurysm_adjacent_drain_cap(domination_rank: int) -> float:
    """Most Energy one adjacent foe can lose to Aneurysm at this rank.

    Ranks outside the published 0-21 range clamp to the nearest end rather
    than raising: a bad attribute read should cost accuracy, not a cast.
    """
    rank = max(0, min(int(domination_rank), len(ANEURYSM_MAX_ADJACENT_DRAIN_BY_RANK) - 1))
    return float(ANEURYSM_MAX_ADJACENT_DRAIN_BY_RANK[rank])


def linear_drain(attribute_rank: int) -> float:
    """Energy removed by a ``1...8...10`` drain skill at ``attribute_rank``.

    Energy Surge and Energy Burn publish the identical progression, and
    ``round(1 + 0.6 * rank)`` reproduces it exactly for every rank 0-21
    (1 at rank 0, 8 at 12, 10 at 15, 14 at 21).

    Verify any further skill against its own progression table before reusing
    this; a shared shape is a coincidence worth checking, not a rule.
    """
    return float(round(1 + 0.6 * max(0, int(attribute_rank))))


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _note_event(at_ms: float) -> None:
    """Record that something was actually billed, for the liveness readout."""
    global _recorded_event_count, _last_event_ms

    _recorded_event_count += 1
    _last_event_ms = at_ms


def _invalidate_on_map_change() -> None:
    """Drop the ledger when the map changed, mirroring the role-cache rule.

    A failed map read leaves the ledger alone: losing a little accuracy beats
    clearing real entries because one lookup misbehaved.
    """
    global _ledger_map_id

    try:
        from Py4GWCoreLib.Map import Map

        current_map_id = int(Map.GetMapID())
    except Exception:
        return

    if current_map_id != _ledger_map_id:
        _ledger_map_id = current_map_id
        _drains.clear()
        _observed_casts.clear()


def record_drain(agent_id: int, energy_points: float) -> None:
    """Record that ``energy_points`` were removed from ``agent_id`` just now."""
    if not agent_id or energy_points <= 0.0:
        return

    _invalidate_on_map_change()
    now_ms = _now_ms()
    _drains.setdefault(int(agent_id), []).append((now_ms, float(energy_points)))
    _note_event(now_ms)


def _skill_energy_cost(skill_id: int) -> float:
    """Base Energy cost of ``skill_id``, or 0.0 when it cannot be resolved.

    Split out so the offline harness can substitute a cost table without a
    live client.
    """
    try:
        from Py4GWCoreLib import GLOBAL_CACHE

        return float(GLOBAL_CACHE.Skill.Data.GetEnergyCost(skill_id) or 0)
    except Exception:
        return 0.0


def observe_enemy_cast(agent_id: int, skill_id: int) -> None:
    """Bill ``agent_id`` the base Energy cost of a cast just seen starting.

    Energy is spent when activation begins, so an interrupted cast is billed
    exactly like a completed one -- which is correct, and conveniently means the
    caller only has to notice the cast starting.

    Zero-cost skills (attacks, signets) resolve to no charge and fall out
    naturally. Repeat calls for the same ``(agent_id, skill_id)`` inside
    :data:`OBSERVED_CAST_DEDUPE_MS` are ignored.
    """
    if not agent_id or not skill_id:
        return

    energy_cost = _skill_energy_cost(skill_id)
    if energy_cost <= 0.0:
        return

    _invalidate_on_map_change()

    now_ms = _now_ms()
    key = (int(agent_id), int(skill_id))
    last_billed_ms = _observed_casts.get(key)
    if last_billed_ms is not None and (now_ms - last_billed_ms) < OBSERVED_CAST_DEDUPE_MS:
        return

    _observed_casts[key] = now_ms
    _drains.setdefault(int(agent_id), []).append((now_ms, energy_cost))
    _note_event(now_ms)

    # The dedupe map is keyed per (agent, skill) and would otherwise grow for
    # the whole time we stay on one map. Entries older than a drain's lifetime
    # can never suppress anything, so drop them.
    if len(_observed_casts) > 256:
        cutoff = now_ms - DRAIN_MAX_AGE_MS
        for stale_key in [k for k, ts in _observed_casts.items() if ts < cutoff]:
            _observed_casts.pop(stale_key, None)


def estimated_deficit(agent_id: int) -> float:
    """Energy this build believes ``agent_id`` is still missing.

    Each recorded drain is repaid at :data:`ASSUMED_ENEMY_ENERGY_REGEN_PER_SEC`
    and drops out once fully repaid or aged out. Returns ``0.0`` for an unknown
    agent, which is the correct default: no evidence of a drain is not evidence
    of one.
    """
    if not agent_id:
        return 0.0

    _invalidate_on_map_change()

    entries = _drains.get(int(agent_id))
    if not entries:
        return 0.0

    now_ms = _now_ms()
    surviving: list[tuple[float, float]] = []
    total = 0.0

    for recorded_at_ms, points in entries:
        age_ms = now_ms - recorded_at_ms
        if age_ms >= DRAIN_MAX_AGE_MS:
            continue
        remaining = points - (ASSUMED_ENEMY_ENERGY_REGEN_PER_SEC * age_ms / 1000.0)
        if remaining <= 0.0:
            continue
        surviving.append((recorded_at_ms, points))
        total += remaining

    if surviving:
        _drains[int(agent_id)] = surviving
    else:
        _drains.pop(int(agent_id), None)

    return min(total, ASSUMED_MAX_ENEMY_POOL)


def clear_agent(agent_id: int) -> None:
    """Forget every recorded drain on ``agent_id``.

    Call this when something is known to have refilled the foe -- Aneurysm
    restores its target's Energy pool outright, so the ledger must reset rather
    than keep claiming a deficit that was just handed back.
    """
    if agent_id:
        _drains.pop(int(agent_id), None)


def clear_all() -> None:
    """Drop every entry. Exposed for tests and explicit resets."""
    global _recorded_event_count, _last_event_ms

    _drains.clear()
    _observed_casts.clear()
    _recorded_event_count = 0
    _last_event_ms = None


def tracked_agent_ids() -> list[int]:
    """Agents currently carrying at least one un-expired drain entry.

    Diagnostic only. Entries are pruned lazily by :func:`estimated_deficit`, so
    this can name an agent whose deficit has already decayed to nothing.
    """
    return list(_drains.keys())


def recorded_event_count() -> int:
    """How many drains and observed casts have been billed this process.

    Zero means nothing is feeding the ledger at all -- which usually means the
    cast sampler in ``HeroAI/interrupt.py`` is not running -- rather than that
    foes are being frugal with their Energy.
    """
    return _recorded_event_count


def ms_since_last_event() -> float | None:
    """Age of the most recent billing, or ``None`` if nothing was ever billed."""
    if _last_event_ms is None:
        return None
    return _now_ms() - _last_event_ms
