"""Offline test harness for the estimated foe Energy-deficit ledger.

Runnable outside the injected client:

    python tests/test_energy_denial_ledger.py

``Py4GWCoreLib`` cannot be imported outside Guild Wars, so the module under
test is loaded directly from its file path -- the same trick, and the same
alarm, as ``tests/test_skill_helper_registry.py``. That works only because
``_energy_denial.py`` keeps its one ``Py4GWCoreLib`` import (``Map``, for map
change detection) inside a function behind ``try``.

The decay arithmetic and the drain progression are the only real logic in the
ledger, and both are pure. Everything downstream -- whether a drain actually
landed, whether the foe really was emptied -- needs a live client and cannot be
proven here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "Py4GWCoreLib" / "Builds" / "Skills" / "_energy_denial.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("_py4gw_energy_denial_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module spec from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ledger = _load_module()


# region harness
_passed = 0
_failed: list[str] = []


def check(label: str, condition: bool) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def group(title: str) -> None:
    print(f"\n[test] {title}")


class FrozenClock:
    """Drives the ledger's clock so decay can be tested without sleeping."""

    def __init__(self) -> None:
        self.now_ms = 1_000_000.0
        self._original = ledger._now_ms

    def install(self) -> None:
        ledger._now_ms = lambda: self.now_ms

    def restore(self) -> None:
        ledger._now_ms = self._original

    def advance_seconds(self, seconds: float) -> None:
        self.now_ms += seconds * 1000.0


# endregion


# The published Energy Surge / Energy Burn progression, ranks 0-21.
WIKI_DRAIN_PROGRESSION = [1, 2, 2, 3, 3, 4, 5, 5, 6, 6, 7, 8, 8, 9, 9, 10, 11, 11, 12, 12, 13, 14]


def test_drain_progression_matches_published_table() -> None:
    group("linear_drain reproduces the published progression exactly")
    mismatches = [
        (rank, expected, ledger.linear_drain(rank))
        for rank, expected in enumerate(WIKI_DRAIN_PROGRESSION)
        if ledger.linear_drain(rank) != float(expected)
    ]
    check(f"all 22 ranks match (mismatches: {mismatches})", not mismatches)
    check("rank 12 drains 8", ledger.linear_drain(12) == 8.0)
    check("rank 15 drains 10", ledger.linear_drain(15) == 10.0)
    check("negative rank clamps to the rank-0 value", ledger.linear_drain(-5) == 1.0)


def test_aneurysm_adjacent_drain_cap() -> None:
    group("aneurysm_adjacent_drain_cap matches the published maximums")
    # Anchors taken from the skill description itself -- "(Maximum 1...24...30)"
    # -- which is an independent source from the progression table the constant
    # was transcribed from. Agreement between the two is the real check here.
    check("rank 0 caps at 1", ledger.aneurysm_adjacent_drain_cap(0) == 1.0)
    check("rank 12 caps at 24", ledger.aneurysm_adjacent_drain_cap(12) == 24.0)
    check("rank 15 caps at 30", ledger.aneurysm_adjacent_drain_cap(15) == 30.0)

    caps = [ledger.aneurysm_adjacent_drain_cap(rank) for rank in range(22)]
    check("never decreases with rank", all(b >= a for a, b in zip(caps, caps[1:])))
    check("table covers ranks 0-21", len(ledger.ANEURYSM_MAX_ADJACENT_DRAIN_BY_RANK) == 22)

    check("negative rank clamps to rank 0", ledger.aneurysm_adjacent_drain_cap(-3) == 1.0)
    check("over-rank clamps to rank 21", ledger.aneurysm_adjacent_drain_cap(99) == 42.0)


def test_unknown_agent_has_no_deficit() -> None:
    group("an agent nobody drained reports no deficit")
    ledger.clear_all()
    check("unknown agent is 0.0", ledger.estimated_deficit(4321) == 0.0)
    check("agent id 0 is 0.0", ledger.estimated_deficit(0) == 0.0)


def test_recorded_drain_decays_to_zero() -> None:
    group("a recorded drain decays at the assumed regeneration rate")
    ledger.clear_all()
    clock = FrozenClock()
    clock.install()
    try:
        ledger.record_drain(99, 8.0)
        check("full drain visible immediately", abs(ledger.estimated_deficit(99) - 8.0) < 0.001)

        clock.advance_seconds(6.0)
        # 6s at 0.66/s repays ~3.96, leaving ~4.04.
        remaining = ledger.estimated_deficit(99)
        check(f"partially repaid after 6s (got {remaining:.2f})", 3.9 < remaining < 4.2)

        clock.advance_seconds(20.0)
        check("fully repaid after 26s", ledger.estimated_deficit(99) == 0.0)
        check("exhausted entry is dropped from the ledger", 99 not in ledger._drains)
    finally:
        clock.restore()


def test_drains_stack() -> None:
    group("separate drains on one foe stack")
    ledger.clear_all()
    clock = FrozenClock()
    clock.install()
    try:
        ledger.record_drain(7, 8.0)
        ledger.record_drain(7, 8.0)
        check("two 8-point drains read as 16", abs(ledger.estimated_deficit(7) - 16.0) < 0.001)
    finally:
        clock.restore()


def test_hard_age_out() -> None:
    group("a drain larger than regeneration can repay still ages out")
    ledger.clear_all()
    clock = FrozenClock()
    clock.install()
    try:
        ledger.record_drain(11, 500.0)  # absurd, so decay alone never clears it
        clock.advance_seconds(ledger.DRAIN_MAX_AGE_MS / 1000.0 + 1.0)
        check("aged-out entry reports 0.0", ledger.estimated_deficit(11) == 0.0)
    finally:
        clock.restore()


def test_clear_agent_resets_only_that_foe() -> None:
    group("clear_agent resets one foe and leaves the rest alone")
    ledger.clear_all()
    ledger.record_drain(1, 10.0)
    ledger.record_drain(2, 10.0)
    ledger.clear_agent(1)
    check("cleared foe is 0.0", ledger.estimated_deficit(1) == 0.0)
    check("other foe is untouched", ledger.estimated_deficit(2) > 0.0)


def test_non_positive_drains_are_ignored() -> None:
    group("a zero or negative drain is not recorded")
    ledger.clear_all()
    ledger.record_drain(3, 0.0)
    ledger.record_drain(3, -5.0)
    check("nothing recorded", ledger.estimated_deficit(3) == 0.0)


class FakeCosts:
    """Substitutes a skill-cost table so cast billing runs without a client."""

    def __init__(self, costs: dict[int, float]) -> None:
        self.costs = costs
        self._original = ledger._skill_energy_cost

    def install(self) -> None:
        ledger._skill_energy_cost = lambda skill_id: float(self.costs.get(skill_id, 0.0))

    def restore(self) -> None:
        ledger._skill_energy_cost = self._original


def test_observed_cast_bills_its_energy_cost() -> None:
    group("an observed enemy cast is billed at its Energy cost")
    ledger.clear_all()
    clock = FrozenClock()
    clock.install()
    costs = FakeCosts({101: 10.0, 102: 25.0, 103: 0.0})
    costs.install()
    try:
        ledger.observe_enemy_cast(50, 101)
        check("10-energy cast billed", abs(ledger.estimated_deficit(50) - 10.0) < 0.001)

        clock.advance_seconds(2.0)
        ledger.observe_enemy_cast(50, 102)
        # 10 decayed by 2s at 0.66/s = ~8.68, plus a fresh 25.
        total = ledger.estimated_deficit(50)
        check(f"second cast stacks on the decayed first (got {total:.2f})", 33.0 < total < 34.0)

        ledger.observe_enemy_cast(50, 103)
        check("zero-cost skill adds nothing", abs(ledger.estimated_deficit(50) - total) < 0.05)
    finally:
        costs.restore()
        clock.restore()


def test_repeat_observation_is_deduped() -> None:
    group("a flickering sampler cannot bill the same cast twice")
    ledger.clear_all()
    clock = FrozenClock()
    clock.install()
    costs = FakeCosts({101: 10.0})
    costs.install()
    try:
        ledger.observe_enemy_cast(60, 101)
        ledger.observe_enemy_cast(60, 101)
        ledger.observe_enemy_cast(60, 101)
        check("still billed once", abs(ledger.estimated_deficit(60) - 10.0) < 0.001)

        clock.advance_seconds(ledger.OBSERVED_CAST_DEDUPE_MS / 1000.0 + 0.1)
        ledger.observe_enemy_cast(60, 101)
        check("a genuine re-cast past the window bills again", ledger.estimated_deficit(60) > 15.0)
    finally:
        costs.restore()
        clock.restore()


def test_deficit_is_clamped_to_a_plausible_pool() -> None:
    group("accumulated spend cannot exceed a plausible Energy pool")
    ledger.clear_all()
    clock = FrozenClock()
    clock.install()
    costs = FakeCosts({101: 25.0})
    costs.install()
    try:
        for _ in range(10):
            ledger.observe_enemy_cast(70, 101)
            clock.advance_seconds(ledger.OBSERVED_CAST_DEDUPE_MS / 1000.0 + 0.1)
        check(
            f"clamped to ASSUMED_MAX_ENEMY_POOL (got {ledger.estimated_deficit(70):.1f})",
            ledger.estimated_deficit(70) == ledger.ASSUMED_MAX_ENEMY_POOL,
        )
    finally:
        costs.restore()
        clock.restore()


def test_liveness_counters_separate_silence_from_starvation() -> None:
    group("liveness counters distinguish 'no events' from 'no deficit'")
    ledger.clear_all()
    clock = FrozenClock()
    clock.install()
    costs = FakeCosts({101: 10.0})
    costs.install()
    try:
        check("starts with no events", ledger.recorded_event_count() == 0)
        check("no last-event age before anything happens", ledger.ms_since_last_event() is None)
        check("nothing tracked", ledger.tracked_agent_ids() == [])

        ledger.observe_enemy_cast(80, 101)
        ledger.record_drain(81, 8.0)
        check("both sources counted", ledger.recorded_event_count() == 2)
        check("agents tracked", sorted(ledger.tracked_agent_ids()) == [80, 81])

        clock.advance_seconds(3.0)
        age_ms = ledger.ms_since_last_event()
        check(f"last-event age advances (got {age_ms})", age_ms is not None and abs(age_ms - 3000.0) < 1.0)

        # The deficit decays to nothing, but the counter still proves the
        # ledger was being fed -- the exact ambiguity the panel must resolve.
        clock.advance_seconds(60.0)
        check("deficit gone", ledger.estimated_deficit(80) == 0.0)
        check("event count still records that it was fed", ledger.recorded_event_count() == 2)
    finally:
        costs.restore()
        clock.restore()


def main() -> int:
    test_drain_progression_matches_published_table()
    test_aneurysm_adjacent_drain_cap()
    test_unknown_agent_has_no_deficit()
    test_recorded_drain_decays_to_zero()
    test_drains_stack()
    test_hard_age_out()
    test_clear_agent_resets_only_that_foe()
    test_non_positive_drains_are_ignored()
    test_observed_cast_bills_its_energy_cost()
    test_repeat_observation_is_deduped()
    test_deficit_is_clamped_to_a_plausible_pool()
    test_liveness_counters_separate_silence_from_starvation()

    print("\n" + "=" * 60)
    if _failed:
        print(f"FAILED - {len(_failed)} check(s) failed, {_passed} passed")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print(f"OK - {_passed} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
