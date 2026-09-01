"""Offline fixture for the BT path-following geometry (Layers 1 and 3).

Guards the three defects that made right-click-to-move stall on its own route:

  * A waypoint the character could not physically enter (a tree, a terrain
    edge) was a permanent stall, because the only way to advance the path
    index was to enter its arrival disc.

  * After a combat or loot pause the character had usually walked past the
    node it was still tracking, so it backtracked before continuing.

  * Intermediate nodes are route hints spaced up to 500 units apart, yet they
    were held to the caller's *destination* precision.

  * A route was planned once at click time and followed to the end no matter
    how stale it had become, so a long cross-map path never improved and a
    detour that left the corridor entirely was walked all the way back.

The module under test has no game-runtime imports, so every assertion below
runs with plain `python` outside the injected client. That also means none of
it proves injected behavior.

Run:  python "Examples and tests/tests/test_path_following_geometry.py"
Exit code 1 on any failure.
"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "Py4GWCoreLib" / "routines_src" / "behaviourtrees_src" / "path_following.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def section(title: str) -> None:
    print("\n== %s" % title)


def _load(name: str, path: pathlib.Path):
    # sys.modules registration has to happen before exec_module, or a
    # slots=True dataclass dies on 'NoneType' object has no attribute
    # '__dict__' during a standalone importlib load.
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None, "no module spec for %s" % path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pf = _load("path_following", MODULE_PATH)

TOLERANCE = 150.0
CORRIDOR = 225.0
LOOKAHEAD = 3


def passed(position, waypoint, next_waypoint, tolerance=TOLERANCE, corridor=CORRIDOR) -> bool:
    return pf.waypoint_passed(
        position,
        waypoint,
        next_waypoint,
        tolerance=tolerance,
        corridor_half_width=corridor,
    )


def select(position, path, current_index, lookahead=LOOKAHEAD, is_reachable=None):
    return pf.select_target(
        position,
        path,
        current_index,
        tolerance=TOLERANCE,
        corridor_half_width=CORRIDOR,
        lookahead_nodes=lookahead,
        is_reachable=is_reachable,
    )


# ---------------------------------------------------------------------------
# 1. waypoint_passed
# ---------------------------------------------------------------------------


def test_waypoint_passed() -> None:
    section("waypoint_passed")

    # T1 / T2: the final node keeps the disc test and nothing else.
    check("T1 inside the disc with no next waypoint is passed", passed((100.0, 40.0), (100.0, 0.0), None))
    check(
        "T2 outside the disc with no next waypoint is not passed",
        not passed((100.0, 400.0), (100.0, 0.0), None),
    )

    # T3: exactly on the outgoing plane, dead centre of the corridor, with the
    # disc shrunk to nothing so only the plane test can answer.
    check(
        "T3 exactly on the outgoing plane inside the corridor is passed",
        passed((1000.0, 0.0), (1000.0, 0.0), (2000.0, 0.0), tolerance=0.0),
    )

    # T4: this is S3 - the loot handler left the character 400 units past the
    # node it was still tracking.
    check(
        "T4 past the plane by 400 inside the corridor is passed",
        passed((400.0, 0.0), (0.0, 0.0), (1000.0, 0.0)),
    )

    # T5: past the plane but far off to the side. A wild detour must not
    # teleport the index forward.
    check(
        "T5 past the plane but 900 off to the side is not passed",
        not passed((400.0, 900.0), (0.0, 0.0), (1000.0, 0.0)),
    )

    # T6: before the plane and outside the disc, dead centre of the corridor.
    check(
        "T6 before the plane inside the corridor is not passed",
        not passed((-400.0, 0.0), (0.0, 0.0), (1000.0, 0.0)),
    )

    # T7: a zero-length outgoing segment must not divide by zero.
    check(
        "T7 degenerate waypoint == next_waypoint falls back to the disc (near)",
        passed((0.0, 100.0), (0.0, 0.0), (0.0, 0.0)),
    )
    check(
        "T7 degenerate waypoint == next_waypoint falls back to the disc (far)",
        not passed((0.0, 900.0), (0.0, 0.0), (0.0, 0.0)),
    )

    # The corridor boundary, from both sides of it.
    check(
        "corridor boundary is inclusive",
        passed((400.0, CORRIDOR), (0.0, 0.0), (1000.0, 0.0)),
    )
    check(
        "just outside the corridor boundary is not passed",
        not passed((400.0, CORRIDOR + 1.0), (0.0, 0.0), (1000.0, 0.0)),
    )


# ---------------------------------------------------------------------------
# 2. select_target / select_target_index
# ---------------------------------------------------------------------------

STRAIGHT_PATH = [
    (0.0, 0.0),
    (500.0, 0.0),
    (1000.0, 0.0),
    (1500.0, 0.0),
    (2000.0, 0.0),
    (2500.0, 0.0),
]


def test_select_target_index() -> None:
    section("select_target_index")

    # T8: nothing to select on an empty or single-node path.
    check("T8 empty path returns current_index", select((0.0, 0.0), [], 0).index == 0)
    check(
        "T8 single-node path returns current_index",
        select((0.0, 0.0), [(100.0, 100.0)], 0).index == 0,
    )

    # T9: the final node is the caller's business, never this function's.
    check(
        "T9 already at the last node is unchanged",
        select((2500.0, 0.0), STRAIGHT_PATH, 5).index == 5,
    )
    check(
        "T9 standing on the last node still does not advance past it",
        select((2500.0, 0.0), STRAIGHT_PATH, 4).index == 5,
    )

    # T10: the S1/S2 skip. The character is on the leg into node 3, well past
    # node 2's plane and outside node 3's disc, while the follower still
    # believes it is chasing node 1.
    selection = select((1300.0, 0.0), STRAIGHT_PATH, 1)
    check("T10 look-ahead selects node 3, not node 2", selection.index == 3, repr(selection))
    # Node 1 was the node being tracked, so leaving it is not a skip; node 2 is
    # the one that got bypassed without ever being visited.
    check("T10 reports one bypassed node", selection.skipped == 1, repr(selection))
    check("T10 reports the lookahead reason", selection.reason == pf.REASON_LOOKAHEAD, repr(selection))

    # A single-node advance is reported as a corridor advance, not a lookahead.
    single = select((800.0, 0.0), STRAIGHT_PATH, 1)
    check("single corridor advance selects node 2", single.index == 2, repr(single))
    check("single corridor advance reports corridor", single.reason == pf.REASON_CORRIDOR, repr(single))
    check("single corridor advance reports nothing skipped", single.skipped == 0, repr(single))

    # Standing on a node advances past it: the disc rung of the old follower
    # has to keep working, or the mover orders itself to walk on the spot.
    on_node = select((1000.0, 0.0), STRAIGHT_PATH, 2)
    check("standing on a node advances past it", on_node.index == 3, repr(on_node))
    check("standing on a node reports the disc reason", on_node.reason == pf.REASON_DISC, repr(on_node))

    # T11: reachability gates the corridor skip, and only the corridor skip.
    blocked = select((1300.0, 0.0), STRAIGHT_PATH, 1, is_reachable=lambda start, end: False)
    check("T11 unreachable look-ahead does not advance", blocked.index == 1, repr(blocked))
    check(
        "T11 an unreachable check never blocks a disc advance",
        select((1000.0, 0.0), STRAIGHT_PATH, 2, is_reachable=lambda start, end: False).index == 3,
    )

    probed: list[tuple] = []

    def record(start, end) -> bool:
        probed.append((start, end))
        return True

    select((-900.0, 0.0), STRAIGHT_PATH, 0, is_reachable=record)
    check("reachability is not probed when nothing is passed", probed == [], repr(probed))

    # T12: the window caps the jump even when everything ahead is passed.
    windowed = select((2400.0, 0.0), STRAIGHT_PATH, 0, lookahead=2)
    check("T12 look-ahead window is respected", windowed.index == 2, repr(windowed))
    check(
        "T12 a zero window never advances",
        select((2400.0, 0.0), STRAIGHT_PATH, 0, lookahead=0).index == 0,
    )

    # T13: monotonic. A character behind the start of the path keeps its index.
    check(
        "T13 a position behind the path start never moves the index back",
        select((-2000.0, 0.0), STRAIGHT_PATH, 2).index == 2,
    )
    check(
        "T13 a position far off the corridor never moves the index back",
        select((1300.0, 5000.0), STRAIGHT_PATH, 1).index == 1,
    )

    # A right-angle turn must not let the corridor swallow the corner node.
    corner_path = [(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0)]
    check(
        "an approach along the first leg does not skip the corner",
        select((700.0, 0.0), corner_path, 0).index == 1,
    )

    # select_target_index is the documented surface; it must agree.
    check(
        "select_target_index agrees with select_target",
        pf.select_target_index(
            (1300.0, 0.0),
            STRAIGHT_PATH,
            1,
            tolerance=TOLERANCE,
            corridor_half_width=CORRIDOR,
            lookahead_nodes=LOOKAHEAD,
        )
        == 3,
    )


# ---------------------------------------------------------------------------
# 3. Replan predicates
# ---------------------------------------------------------------------------

# An L: east along y=0 to (1000, 0), then north. Standing on the first leg is
# standing on route only if traversed legs still count, which they must not.
L_PATH = [
    (0.0, 0.0),
    (1000.0, 0.0),
    (1000.0, 3000.0),
]

OFF_PATH_THRESHOLD = 500.0


def off_path(position, path, current_index, threshold=OFF_PATH_THRESHOLD) -> bool:
    return pf.should_replan_off_path(position, path, current_index, threshold=threshold)


def test_replan_predicates() -> None:
    section("replan predicates")

    # T19: a normal wobble off the route is not worth a replan; a real detour is.
    check(
        "T19 200 units off the route does not replan",
        not off_path((750.0, 200.0), STRAIGHT_PATH, 1),
    )
    check(
        "T19 900 units off the route replans",
        off_path((750.0, 900.0), STRAIGHT_PATH, 1),
    )
    check(
        "T19 a threshold of 0 means off",
        not off_path((750.0, 5000.0), STRAIGHT_PATH, 1, threshold=0.0),
    )
    check("T19 an empty path never replans", not off_path((0.0, 0.0), [], 0))

    # T20: the measurement is against the remaining route only. Standing on the
    # leg already walked is not evidence of being on route.
    check(
        "T20 distance uses the whole path from index 0",
        abs(pf.distance_to_remaining_path((200.0, 0.0), L_PATH, 0)) < 1e-9,
    )
    check(
        "T20 distance ignores the traversed leg from index 1",
        abs(pf.distance_to_remaining_path((200.0, 0.0), L_PATH, 1) - 800.0) < 1e-9,
    )
    check(
        "T20 a player on a traversed leg still replans",
        off_path((200.0, 0.0), L_PATH, 1),
    )
    check(
        "T20 the same player on the current leg does not",
        not off_path((200.0, 0.0), L_PATH, 0),
    )
    check(
        "T20 nothing remaining measures against the last node",
        abs(pf.distance_to_remaining_path((1000.0, 3300.0), L_PATH, 2) - 300.0) < 1e-9,
    )
    check(
        "T20 an empty path has no measurable distance",
        pf.distance_to_remaining_path((0.0, 0.0), [], 0) == float("inf"),
    )

    # T21: the periodic refresh and the minimum spacing share one shape but must
    # disagree about a missing reference timestamp.
    check(
        "T21 an interval of 0 means off",
        not pf.should_replan_interval(10000, 0, interval_ms=0),
    )
    check(
        "T21 a negative interval means off",
        not pf.should_replan_interval(10000, 0, interval_ms=-5),
    )
    check(
        "T21 no route acquired yet is never due for a refresh",
        not pf.should_replan_interval(10000, None, interval_ms=1000),
    )
    check(
        "T21 the interval boundary is due",
        pf.should_replan_interval(2000, 1000, interval_ms=1000),
    )
    check(
        "T21 one tick short of the interval is not due",
        not pf.should_replan_interval(1999, 1000, interval_ms=1000),
    )
    check(
        "T21 never having replanned is always far enough",
        pf.replan_spacing_elapsed(10000, None, min_interval_ms=2000),
    )
    check(
        "T21 inside the spacing window is too soon",
        not pf.replan_spacing_elapsed(3000, 2000, min_interval_ms=2000),
    )
    check(
        "T21 the spacing boundary is far enough",
        pf.replan_spacing_elapsed(4000, 2000, min_interval_ms=2000),
    )
    check(
        "T21 a spacing of 0 imposes no window",
        pf.replan_spacing_elapsed(2001, 2000, min_interval_ms=0),
    )

    # T22: an unreachable destination replans forever without this, because every
    # fresh route hands the timeout watcher a fresh budget.
    check("T22 no replans yet is not exhausted", not pf.replan_budget_exhausted(0, max_replans=2))
    check("T22 one replan short of the cap is not exhausted", not pf.replan_budget_exhausted(1, max_replans=2))
    check("T22 reaching the cap is exhausted", pf.replan_budget_exhausted(2, max_replans=2))
    check("T22 past the cap is exhausted", pf.replan_budget_exhausted(9, max_replans=2))
    check("T22 a cap of 0 disables replanning", pf.replan_budget_exhausted(0, max_replans=0))

    # The reasons are published on the blackboard, so they are part of the contract.
    check(
        "replan reasons are distinct",
        len({pf.REPLAN_REASON_STALL, pf.REPLAN_REASON_OFF_PATH, pf.REPLAN_REASON_INTERVAL}) == 3,
    )
    check("no replan reason is empty", pf.REPLAN_REASON_NONE == "")


# ---------------------------------------------------------------------------
# 4. The module has to stay offline-testable
# ---------------------------------------------------------------------------


def test_no_runtime_imports() -> None:
    section("module isolation")

    source = MODULE_PATH.read_text(encoding="utf-8")
    check("no relative game-runtime imports", "from .." not in source)
    check("no Py4GW import", "import Py4GW" not in source)
    check("source stays ASCII", all(ord(character) < 128 for character in source))


def main() -> int:
    test_waypoint_passed()
    test_select_target_index()
    test_replan_predicates()
    test_no_runtime_imports()

    print("\n" + "-" * 60)
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
