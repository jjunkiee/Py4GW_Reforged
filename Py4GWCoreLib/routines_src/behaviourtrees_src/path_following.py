"""Pure geometry helpers for BT path following.

Waypoint arrival used to be a single distance disc, so an intermediate node the
mover could not physically enter stalled the whole route, and a node the mover
had already walked past was still chased backwards after a combat or loot
pause.  This module answers a different question: given where the mover is now,
which node of the route is still worth walking to?

Intermediate nodes are route hints, not destinations.  They are considered
passed either by entering their arrival disc or by crossing their outgoing
plane while staying inside a corridor around the outgoing segment.  The final
node is never treated that way; destination precision stays with the caller.

It also owns the replan predicates: a route is a snapshot of what was known
when it was planned, so the follower needs a decidable answer to "is this route
still worth following?".  Those predicates are pure so the triggers can be
tested without a client, even though the replan itself cannot be.

Like :mod:`local_avoidance` this module deliberately has no game-runtime
imports so the geometry can be tested offline.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
import math

Point2D = tuple[float, float]
ReachableCheck = Callable[[Point2D, Point2D], bool]

REASON_NONE = ""
REASON_DISC = "disc"
REASON_CORRIDOR = "corridor"
REASON_LOOKAHEAD = "lookahead"
REASON_SKIP_STALL = "skip_stall"

REPLAN_REASON_NONE = ""
REPLAN_REASON_STALL = "stall"
REPLAN_REASON_OFF_PATH = "off_path"
REPLAN_REASON_INTERVAL = "interval"


@dataclass(frozen=True, slots=True)
class TargetSelection:
    """The node the mover should walk to, and why it was chosen."""

    index: int
    reason: str
    skipped: int


def _passed_reason(
    position: Point2D,
    waypoint: Point2D,
    next_waypoint: Point2D | None,
    *,
    tolerance: float,
    corridor_half_width: float,
) -> str:
    """Classify how the mover cleared a waypoint, or `REASON_NONE` if it did not."""

    if math.dist(position, waypoint) <= max(0.0, float(tolerance)):
        return REASON_DISC
    if next_waypoint is None:
        return REASON_NONE

    forward_x = next_waypoint[0] - waypoint[0]
    forward_y = next_waypoint[1] - waypoint[1]
    forward_length = math.hypot(forward_x, forward_y)
    if forward_length <= 1e-6:
        return REASON_NONE

    relative_x = position[0] - waypoint[0]
    relative_y = position[1] - waypoint[1]
    projection = (relative_x * forward_x + relative_y * forward_y) / forward_length
    if projection < 0.0:
        return REASON_NONE

    offset = abs(relative_x * forward_y - relative_y * forward_x) / forward_length
    if offset > max(0.0, float(corridor_half_width)):
        return REASON_NONE
    return REASON_CORRIDOR


def waypoint_passed(
    position: Point2D,
    waypoint: Point2D,
    next_waypoint: Point2D | None,
    *,
    tolerance: float,
    corridor_half_width: float,
) -> bool:
    """True when the mover is inside the arrival disc, or has crossed the
    waypoint's outgoing plane while staying inside the corridor.

    `next_waypoint` of `None` marks the final node and falls back to the disc
    test alone, so destination precision is never weakened.
    """

    return (
        _passed_reason(
            position,
            waypoint,
            next_waypoint,
            tolerance=tolerance,
            corridor_half_width=corridor_half_width,
        )
        != REASON_NONE
    )


def select_target(
    position: Point2D,
    path: Sequence[Point2D],
    current_index: int,
    *,
    tolerance: float,
    corridor_half_width: float,
    lookahead_nodes: int,
    is_reachable: ReachableCheck | None = None,
) -> TargetSelection:
    """Choose the furthest node within the look-ahead window still worth walking to.

    Only intermediate nodes are consumed: the returned index never exceeds the
    final node, so arrival at the destination stays the caller's decision.  A
    corridor advance is gated by `is_reachable` because crossing an outgoing
    plane says nothing about the geometry between the mover and the node it
    would jump to; a disc advance is not gated, since the mover is standing on
    the node it would leave behind.
    """

    unchanged = TargetSelection(index=int(current_index), reason=REASON_NONE, skipped=0)
    if not path:
        return unchanged

    last_index = len(path) - 1
    start_index = max(0, int(current_index))
    if start_index >= last_index:
        return unchanged

    window_end = min(start_index + max(0, int(lookahead_nodes)), last_index)
    selected = start_index
    reason = REASON_NONE
    for index in range(start_index, window_end):
        next_waypoint = path[index + 1]
        passed = _passed_reason(
            position,
            path[index],
            next_waypoint,
            tolerance=tolerance,
            corridor_half_width=corridor_half_width,
        )
        if passed == REASON_NONE:
            break
        if passed == REASON_CORRIDOR and is_reachable is not None and not is_reachable(position, next_waypoint):
            break
        selected = index + 1
        reason = passed

    if selected <= start_index:
        return unchanged

    skipped = selected - start_index - 1
    return TargetSelection(
        index=selected,
        reason=REASON_LOOKAHEAD if skipped > 0 else reason,
        skipped=skipped,
    )


def select_target_index(
    position: Point2D,
    path: Sequence[Point2D],
    current_index: int,
    *,
    tolerance: float,
    corridor_half_width: float,
    lookahead_nodes: int,
    is_reachable: ReachableCheck | None = None,
) -> int:
    """Return the furthest index within the look-ahead window that is a better
    target than current_index. Never returns less than current_index."""

    return select_target(
        position,
        path,
        current_index,
        tolerance=tolerance,
        corridor_half_width=corridor_half_width,
        lookahead_nodes=lookahead_nodes,
        is_reachable=is_reachable,
    ).index


def _distance_to_segment(position: Point2D, start: Point2D, end: Point2D) -> float:
    """Shortest distance from `position` to the segment `start`-`end`."""

    segment_x = end[0] - start[0]
    segment_y = end[1] - start[1]
    segment_length_sq = segment_x * segment_x + segment_y * segment_y
    if segment_length_sq <= 1e-12:
        return math.dist(position, start)

    projection = ((position[0] - start[0]) * segment_x + (position[1] - start[1]) * segment_y) / segment_length_sq
    projection = max(0.0, min(1.0, projection))
    closest = (start[0] + segment_x * projection, start[1] + segment_y * projection)
    return math.dist(position, closest)


def distance_to_remaining_path(position: Point2D, path: Sequence[Point2D], current_index: int) -> float:
    """Shortest distance from the mover to the part of the route it has not walked yet.

    Already-traversed legs are excluded deliberately: standing beside a leg the
    mover finished is not evidence that it is still on route, and counting it
    would hide exactly the detours this measurement exists to catch.  Returns
    `math.inf` when there is no remaining route to measure against.
    """

    if not path:
        return math.inf

    start_index = max(0, min(int(current_index), len(path) - 1))
    if start_index >= len(path) - 1:
        return math.dist(position, path[start_index])

    best = math.inf
    for index in range(start_index, len(path) - 1):
        best = min(best, _distance_to_segment(position, path[index], path[index + 1]))
    return best


def should_replan_off_path(
    position: Point2D,
    path: Sequence[Point2D],
    current_index: int,
    *,
    threshold: float,
) -> bool:
    """True when the mover has drifted further from its remaining route than `threshold`.

    This is the case the corridor test in `select_target` cannot rescue: a
    combat or loot detour that left the corridor entirely, where walking back to
    the old route costs more than planning a new one.  A `threshold` of 0 or
    less means off.
    """

    if float(threshold) <= 0.0:
        return False

    distance = distance_to_remaining_path(position, path, current_index)
    if not math.isfinite(distance):
        return False
    return distance > float(threshold)


def should_replan_interval(now_ms: int, reference_ms: int | None, *, interval_ms: int) -> bool:
    """True when `interval_ms` has elapsed since the route was last acquired.

    `interval_ms` of 0 or less means off.  A `reference_ms` of `None` means no
    route has been acquired yet, which is never due for a refresh.
    """

    if int(interval_ms) <= 0:
        return False
    if reference_ms is None:
        return False
    return (int(now_ms) - int(reference_ms)) >= int(interval_ms)


def replan_spacing_elapsed(now_ms: int, last_replan_ms: int | None, *, min_interval_ms: int) -> bool:
    """True when a new replan is far enough from the previous one to not be thrash.

    Opposite of `should_replan_interval` on the `None` case, and deliberately
    so: never having replanned is always far enough, whereas never having
    acquired a route is never due for a refresh.
    """

    if int(min_interval_ms) <= 0:
        return True
    if last_replan_ms is None:
        return True
    return (int(now_ms) - int(last_replan_ms)) >= int(min_interval_ms)


def replan_budget_exhausted(consecutive_replans: int, *, max_replans: int) -> bool:
    """True when consecutive replans have produced no progress and must stop.

    A destination that cannot be reached would otherwise replan forever, since
    every fresh route restarts the follower's timeout budget.  A `max_replans`
    of 0 or less disables replanning outright.
    """

    return int(consecutive_replans) >= max(0, int(max_replans))
