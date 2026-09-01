"""Map Overlay interaction — targeting, simple move, and NavMesh snap-move.

Projection-agnostic: it converts clicks to game coordinates via ``projection.screen_to_game``
and draws its overlay via ``projection.game_to_screen``, so the same controller serves both
modes. Behaviours (union of both widgets):

- **Left-click** on the frame → target the nearest drawn marker (Compass) and record the
  clicked game coords for the coords strip (Mission Map).
- **Alt + left-click** → simple ``Player.Move`` to the point (Compass).
- **Right-click** (when snap is enabled) → snap to the nearest reachable NavMesh point and walk
  there via a BottingTree MoveTo; **Shift + right-click** queues ordered waypoints. Danger-pause,
  arrival advance, and a stop action are all preserved (Mission Map).

The NavMesh snap machinery is ported from ``Mission Map +`` with ``mm`` replaced by ``self``.
"""

import math
from typing import Optional
from typing import cast

import PyImGui
import PySystem

from Py4GWCoreLib import Agent
from Py4GWCoreLib import AutoPathing
from Py4GWCoreLib import GLOBAL_CACHE
from Py4GWCoreLib import Map
from Py4GWCoreLib import Player
from Py4GWCoreLib import Routines
from Py4GWCoreLib.BottingTree import BottingTree
from Py4GWCoreLib.Pathing import NavMesh
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Py4GWCoreLib.py4gwcorelib_src.Utils import Utils
from Py4GWCoreLib.routines_src.BehaviourTrees import BT as RoutinesBT

from . import shapes
from .model import OverlayConfig
from .projection import Projection

MODULE_NAME = "Map Overlay"

_SNAP_ARRIVAL_RADIUS = 200.0
_SNAP_WAYPOINT_RADIUS = 140.0
_TARGET_HIT_PAD = 3.0

# A right-click is a human asking for a route now, not a bot replaying a fixed one, so
# the overlay opts into the periodic refresh that bots keep switched off until measured.
_SNAP_REPLAN_INTERVAL_MS = 8000


# ── snap coroutines (take the Interaction instance) ──────────────────────────────────────
def _snap_get_navmesh(it: "Interaction") -> "NavMesh | None":
    map_id = int(Map.GetMapID())
    if map_id == 0:
        return None
    if it.snap_navmesh is not None and it.snap_navmesh_map_id == map_id:
        return it.snap_navmesh
    try:
        pathing_maps = Map.Pathing.GetPathingMaps()
        if not pathing_maps:
            return None
        it.snap_navmesh = NavMesh(pathing_maps, map_id)
        it.snap_navmesh_map_id = map_id
    except Exception:
        return None
    return it.snap_navmesh


def _snap_launch_path_coroutine(goal_x: float, goal_y: float, it: "Interaction"):
    it.snap_path_computing = True
    it.snap_current_path = []
    try:
        path = yield from AutoPathing().get_path_to(goal_x, goal_y)
        computed_path = list(path) if path else []
        if len(computed_path) == 0:
            it.snap_current_path = [(float(goal_x), float(goal_y))]
            PySystem.Console.Log(
                MODULE_NAME,
                f"Snap path was empty; using direct fallback waypoint at ({goal_x:.1f}, {goal_y:.1f}).",
                PySystem.Console.MessageType.Warning,
            )
        else:
            it.snap_current_path = computed_path
    except Exception as e:
        it.snap_current_path = [(float(goal_x), float(goal_y))]
        PySystem.Console.Log(
            MODULE_NAME,
            f"Snap path computation failed ({goal_x:.1f}, {goal_y:.1f}): {e}",
            PySystem.Console.MessageType.Error,
        )
    finally:
        it.snap_path_computing = False


def _snap_launch_bt_move_coroutine(goal_x: float, goal_y: float, it: "Interaction", generation: int):
    it.snap_move_running = True
    move_tree = None
    try:
        move_tree = RoutinesBT.Movement.Move(
            goal_x,
            goal_y,
            log=False,
            replan_interval_ms=_SNAP_REPLAN_INTERVAL_MS,
        )
        it.snap_bt_move_tree = move_tree
        while generation == it.snap_move_generation:
            pause_for_danger = it._snap_is_danger_nearby()
            move_tree.blackboard["PAUSE_MOVEMENT"] = pause_for_danger
            if pause_for_danger != it.snap_paused_for_danger:
                it.snap_paused_for_danger = pause_for_danger
            state = BehaviorTree.Node._normalize_state(move_tree.tick())
            if state == RoutinesBT.NodeState.FAILURE:
                it._snap_on_move_failed(generation)
                break
            if state == RoutinesBT.NodeState.SUCCESS:
                break
            yield from Routines.Yield.wait(100)
    except Exception as e:
        PySystem.Console.Log(
            MODULE_NAME,
            f"Snap movement failed to start or tick ({goal_x:.1f}, {goal_y:.1f}): {e}",
            PySystem.Console.MessageType.Error,
        )
    finally:
        if move_tree is not None:
            move_tree.blackboard["PAUSE_MOVEMENT"] = False
        if generation == it.snap_move_generation:
            it.snap_bt_move_tree = None
            it.snap_move_running = False
            it.snap_paused_for_danger = False


def _snap_launch_queue_preview_path_coroutine(start_x, start_y, goal_x, goal_y, it: "Interaction",
                                              generation: int, queue_index: int):
    try:
        zplane = 0.0
        _player = Player.GetAgent()
        if _player:
            zplane = float(_player.pos.zplane)
        path3d = yield from AutoPathing().get_path(
            (float(start_x), float(start_y), zplane),
            (float(goal_x), float(goal_y), zplane),
        )
        preview_path = [(float(x), float(y)) for (x, y, _) in path3d] if path3d else []
        if len(preview_path) == 0:
            preview_path = [(float(start_x), float(start_y)), (float(goal_x), float(goal_y))]
        if generation == it.snap_queue_preview_generation and 0 <= queue_index < len(it.snap_target_queue_paths):
            it.snap_target_queue_paths[queue_index] = preview_path
    except Exception:
        if generation == it.snap_queue_preview_generation and 0 <= queue_index < len(it.snap_target_queue_paths):
            it.snap_target_queue_paths[queue_index] = [(float(start_x), float(start_y)), (float(goal_x), float(goal_y))]


class Interaction:
    def __init__(self, cfg: OverlayConfig) -> None:
        self.cfg = cfg
        self.last_click_x = 0.0
        self.last_click_y = 0.0

        # Target hit boxes. The host points this at the agent pass's own list, which the pass
        # refills each frame; do not rebind it per frame.
        self.hit_targets: list[tuple[int, float, float, float]] = []

        # NavMesh snap state
        self.snap_navmesh: Optional[NavMesh] = None
        self.snap_navmesh_map_id: int = 0
        self.snap_clicked_target: Optional[tuple[float, float]] = None
        self.snap_snapped_target: Optional[tuple[float, float]] = None
        self.snap_target_queue: list[tuple[float, float]] = []
        self.snap_target_queue_paths: list[list[tuple[float, float]]] = []
        self.snap_queue_preview_generation: int = 0
        self.snap_current_path: list[tuple[float, float]] = []
        self.snap_path_computing: bool = False
        self.snap_path_index: int = 0
        self.snap_move_generation: int = 0
        self.snap_move_running: bool = False
        self.snap_paused_for_danger: bool = False
        self.snap_bt_move_tree: Optional[BehaviorTree] = None
        self.snap_bt_draw_helper = BottingTree("Map Overlay Snap Draw")

    @property
    def snap_active(self) -> bool:
        return (
            self.snap_snapped_target is not None
            or self.snap_path_computing
            or self.snap_move_running
            or len(self.snap_target_queue) > 0
            or len(self.snap_current_path) > 0
        )

    # ── snap helpers ─────────────────────────────────────────────────────────────────────
    def _clear_snap_bt_draw_state(self) -> None:
        bb = self.snap_bt_draw_helper.blackboard
        bb["move_state"] = ""
        bb["move_reason"] = ""
        bb["move_target"] = None
        bb["move_path_points"] = []
        bb["move_path_index"] = 0
        bb["move_path_count"] = 0
        bb["move_current_waypoint"] = None
        bb["move_current_waypoint_index"] = -1
        bb["move_skipped_waypoints"] = 0
        bb["move_target_selection_reason"] = ""
        bb["move_replan_count"] = 0
        bb["move_replan_reason"] = ""
        bb["move_replanning"] = False

    def _snap_on_move_failed(self, generation: int) -> None:
        """Drop the navigation when the move tree gives up.

        `snap_active` keys off `snap_snapped_target`, so leaving it set after a
        failure left the overlay reporting a live navigation and drawing a route
        that nothing was walking.
        """
        if generation != self.snap_move_generation:
            return
        reason = ""
        if self.snap_bt_move_tree is not None:
            reason = str(self.snap_bt_move_tree.blackboard.get("move_reason", ""))
        target = self.snap_snapped_target
        dropped = len(self.snap_target_queue)
        self.snap_clear()
        target_text = f"({target[0]:.1f}, {target[1]:.1f})" if target is not None else "the snapped target"
        queue_text = f" {dropped} queued waypoint(s) dropped." if dropped > 0 else ""
        PySystem.Console.Log(
            MODULE_NAME,
            f"Snap movement to {target_text} failed ({reason or 'unknown'}); navigation cleared.{queue_text}",
            PySystem.Console.MessageType.Warning,
        )

    def _sync_path_from_move_tree(self) -> None:
        """Track the route the mover is actually following.

        The click-time path coroutine fills `snap_current_path` once, so without
        this the 2D overlay would keep drawing a route the move tree has already
        replanned away from.
        """
        if self.snap_bt_move_tree is None or self.snap_path_computing:
            return
        source = self.snap_bt_move_tree.blackboard
        if str(source.get("move_state", "")) not in ("running", "paused"):
            return
        points = source.get("move_path_points", [])
        if not isinstance(points, list) or len(points) == 0:
            return
        # The blackboard is untyped; Move publishes this key as a list of 2D points.
        route = cast("list[tuple[float, float]]", points)
        self.snap_current_path = [(float(point_x), float(point_y)) for point_x, point_y in route]
        self.snap_path_index = int(source.get("move_path_index", 0) or 0)

    def _snap_is_danger_nearby(self) -> bool:
        if not self.cfg.snap.pause_on_danger:
            return False
        if not Player.IsPlayerLoaded():
            return False
        try:
            px, py = Player.GetXY()
            enemies = Routines.Agents.GetFilteredEnemyArray(px, py, self.cfg.snap.danger_radius)
            return len(enemies) > 0
        except Exception:
            return False

    def _snap_start_navigation(self, snapped_target: tuple[float, float]) -> None:
        self.snap_move_generation += 1
        move_generation = self.snap_move_generation
        self.snap_move_running = False
        self.snap_bt_move_tree = None
        self.snap_snapped_target = snapped_target
        self.snap_current_path = []
        self._clear_snap_bt_draw_state()
        self.snap_path_index = 0
        GLOBAL_CACHE.Coroutines.append(_snap_launch_path_coroutine(snapped_target[0], snapped_target[1], self))
        GLOBAL_CACHE.Coroutines.append(_snap_launch_bt_move_coroutine(snapped_target[0], snapped_target[1], self, move_generation))

    def snap_clear(self) -> None:
        self.snap_move_generation += 1
        self.snap_move_running = False
        self.snap_paused_for_danger = False
        self.snap_bt_move_tree = None
        self.snap_clicked_target = None
        self.snap_snapped_target = None
        self.snap_target_queue = []
        self.snap_target_queue_paths = []
        self.snap_queue_preview_generation += 1
        self.snap_current_path = []
        self._clear_snap_bt_draw_state()
        self.snap_path_index = 0
        px, py = Player.GetXY()
        Player.Move(px, py)

    def reset_for_map_change(self) -> None:
        self.snap_navmesh = None
        self.snap_navmesh_map_id = 0
        self.snap_clear()

    # ── per-frame update ─────────────────────────────────────────────────────────────────
    def update(self, proj: Projection) -> None:
        io = PyImGui.get_io()
        mx, my = io.mouse_pos_x, io.mouse_pos_y
        left, top, right, bottom = proj.content_rect()
        inside = (left <= mx <= right and top <= my <= bottom)

        if PyImGui.is_mouse_clicked(0) and not io.want_capture_mouse and inside:
            gx, gy = proj.screen_to_game(mx, my)
            self.last_click_x, self.last_click_y = gx, gy
            if io.key_alt:
                Player.Move(gx, gy)
            else:
                self._target_nearest(mx, my)

        if PyImGui.is_mouse_clicked(1) and not io.want_capture_mouse and inside:
            gx, gy = proj.screen_to_game(mx, my)
            if self.cfg.snap.enabled:
                self._snap_right_click((float(gx), float(gy)), bool(io.key_shift))
            else:
                self.snap_clear()

        self._sync_path_from_move_tree()
        self._advance_arrival()

    def _target_nearest(self, mx: float, my: float) -> None:
        best_id = 0
        best_d2 = None
        for agent_id, sx, sy, radius in self.hit_targets:
            r = radius + _TARGET_HIT_PAD
            d2 = (mx - sx) ** 2 + (my - sy) ** 2
            if d2 <= r * r and (best_d2 is None or d2 < best_d2):
                best_d2 = d2
                best_id = agent_id
        if best_id:
            Player.ChangeTarget(best_id)

    def _snap_right_click(self, click_game: tuple[float, float], shift: bool) -> None:
        self.snap_clicked_target = click_game
        nav = _snap_get_navmesh(self)
        snapped = nav.find_nearest_reachable(click_game) if nav else None
        if snapped is None:
            return
        if shift and (self.snap_snapped_target is not None or len(self.snap_target_queue) > 0):
            if len(self.snap_target_queue) > 0:
                start_x, start_y = self.snap_target_queue[-1]
            elif self.snap_snapped_target is not None:
                start_x, start_y = self.snap_snapped_target
            else:
                start_x, start_y = Player.GetXY()
            self.snap_target_queue.append(snapped)
            self.snap_target_queue_paths.append([])
            queue_index = len(self.snap_target_queue_paths) - 1
            GLOBAL_CACHE.Coroutines.append(
                _snap_launch_queue_preview_path_coroutine(
                    start_x, start_y, snapped[0], snapped[1], self,
                    self.snap_queue_preview_generation, queue_index,
                )
            )
        else:
            self.snap_target_queue = []
            self.snap_target_queue_paths = []
            self.snap_queue_preview_generation += 1
            self._snap_start_navigation(snapped)

    def _advance_arrival(self) -> None:
        if self.snap_snapped_target is None or self.snap_path_computing:
            return
        px, py = Player.GetXY()
        dx = px - self.snap_snapped_target[0]
        dy = py - self.snap_snapped_target[1]
        if (dx * dx + dy * dy) <= (_SNAP_ARRIVAL_RADIUS * _SNAP_ARRIVAL_RADIUS):
            if len(self.snap_target_queue) > 0:
                next_target = self.snap_target_queue.pop(0)
                if len(self.snap_target_queue_paths) > 0:
                    self.snap_target_queue_paths.pop(0)
                self._snap_start_navigation(next_target)
            else:
                self.snap_clear()

    # ── overlay draws ────────────────────────────────────────────────────────────────────
    def draw_snap_path_3d(self) -> None:
        if not Routines.Checks.Map.MapValid():
            return
        if self.snap_bt_move_tree is not None:
            src_bb = self.snap_bt_move_tree.blackboard
            src_state = str(src_bb.get("move_state", ""))
            src_points_raw = src_bb.get("move_path_points", [])
            if src_state in ("running", "paused") and isinstance(src_points_raw, list) and len(src_points_raw) > 0:
                bb = self.snap_bt_draw_helper.blackboard
                bb["move_state"] = src_state
                bb["move_reason"] = str(src_bb.get("move_reason", "map_overlay_snap"))
                bb["move_target"] = src_bb.get("move_target")
                bb["move_path_points"] = src_points_raw
                bb["move_path_index"] = int(src_bb.get("move_path_index", 0) or 0)
                bb["move_path_count"] = int(src_bb.get("move_path_count", len(src_points_raw)) or len(src_points_raw))
                bb["move_current_waypoint"] = src_bb.get("move_current_waypoint")
                bb["move_current_waypoint_index"] = int(src_bb.get("move_current_waypoint_index", -1) or -1)
                # The mirror is a fixed whitelist, so the follower diagnostics only become
                # visible to blackboard consumers if they are copied across explicitly.
                bb["move_skipped_waypoints"] = int(src_bb.get("move_skipped_waypoints", 0) or 0)
                bb["move_target_selection_reason"] = str(src_bb.get("move_target_selection_reason", ""))
                bb["move_replan_count"] = int(src_bb.get("move_replan_count", 0) or 0)
                bb["move_replan_reason"] = str(src_bb.get("move_replan_reason", ""))
                bb["move_replanning"] = bool(src_bb.get("move_replanning", False))
                self.snap_bt_draw_helper.DrawMovePath(draw_labels=False, path_thickness=3.0,
                                                      waypoint_radius=15.0, current_waypoint_radius=20.0)
                return

        move_points = [(float(x), float(y)) for x, y in self.snap_current_path]
        if len(move_points) == 0 and self.snap_snapped_target is not None:
            move_points = [(float(self.snap_snapped_target[0]), float(self.snap_snapped_target[1]))]
        if len(move_points) == 0:
            self._clear_snap_bt_draw_state()
            return

        player_x, player_y = Player.GetXY()
        current_index = max(0, min(int(self.snap_path_index), len(move_points) - 1))
        while current_index < (len(move_points) - 1):
            wp_x, wp_y = move_points[current_index]
            dx = player_x - wp_x
            dy = player_y - wp_y
            if (dx * dx + dy * dy) <= (_SNAP_WAYPOINT_RADIUS * _SNAP_WAYPOINT_RADIUS):
                current_index += 1
                continue
            break

        current_waypoint = move_points[current_index]
        move_target = (
            (float(self.snap_snapped_target[0]), float(self.snap_snapped_target[1]))
            if self.snap_snapped_target is not None else move_points[-1]
        )
        bb = self.snap_bt_draw_helper.blackboard
        bb["move_state"] = "running"
        bb["move_reason"] = "map_overlay_snap"
        bb["move_target"] = move_target
        bb["move_path_points"] = move_points
        bb["move_path_index"] = current_index
        bb["move_path_count"] = len(move_points)
        bb["move_current_waypoint"] = current_waypoint
        bb["move_current_waypoint_index"] = current_index
        self.snap_bt_draw_helper.DrawMovePath(draw_labels=False, path_thickness=3.0,
                                              waypoint_radius=15.0, current_waypoint_radius=20.0)

    def draw_overlay(self, proj: Projection) -> None:
        """2D snap overlay (click marker, snapped crosshair, path + queue) inside a draw window."""
        to_screen = proj.game_to_screen

        click_screen = to_screen(*self.snap_clicked_target) if self.snap_clicked_target is not None else None
        snapped_screen = to_screen(*self.snap_snapped_target) if self.snap_snapped_target is not None else None

        # Paths go through AddPolyline: one native call for the whole path instead of one
        # AddLine per segment (a 30-point path was 29 crossings), and the segments join
        # properly instead of being drawn disjoint.
        if len(self.snap_current_path) >= 2 or len(self.snap_target_queue_paths) > 0:
            dl = PyImGui.get_window_draw_list()

            if len(self.snap_current_path) >= 2:
                pts = [to_screen(px, py) for px, py in self.snap_current_path]
                dl.add_polyline(pts, Utils.RGBToColor(80, 160, 255, 210), 0, 2.5)

            if len(self.snap_target_queue_paths) > 0:
                q_color = Utils.RGBToColor(120, 190, 255, 170)
                for queue_path in self.snap_target_queue_paths:
                    if len(queue_path) < 2:
                        continue
                    dl.add_polyline([to_screen(qpx, qpy) for qpx, qpy in queue_path], q_color, 0, 1.8)

        draw_click = click_screen is not None
        if click_screen is not None and snapped_screen is not None:
            if math.hypot(click_screen[0] - snapped_screen[0], click_screen[1] - snapped_screen[1]) <= 12.0:
                draw_click = False
        if draw_click and click_screen is not None:
            c_col = Utils.RGBToColor(220, 220, 220, 200)
            shapes.DLCircleFilled(click_screen[0], click_screen[1], 2.5, c_col, 12)
            shapes.DLCircle(click_screen[0], click_screen[1], 4.0, c_col, 12, 1.0)

        if snapped_screen is not None:
            sx, sy = snapped_screen
            red = Utils.RGBToColor(255, 0, 0, 255)
            redring = Utils.RGBToColor(255, 50, 50, 255)
            cross, r_inner, r_outer = 15.0, 6.0, 11.0
            shapes.DLCircleFilled(sx, sy, r_inner, red, 20)
            shapes.DLCircle(sx, sy, r_outer, redring, 24, 2.5)
            shapes.DLLine(sx - cross, sy, sx - r_outer, sy, redring, 1.5)
            shapes.DLLine(sx + r_outer, sy, sx + cross, sy, redring, 1.5)
            shapes.DLLine(sx, sy - cross, sx, sy - r_outer, redring, 1.5)
            shapes.DLLine(sx, sy + r_outer, sx, sy + cross, redring, 1.5)

        if len(self.snap_target_queue) > 0:
            q_ring = Utils.RGBToColor(255, 220, 80, 230)
            q_fill = Utils.RGBToColor(255, 220, 80, 140)
            q_text = Utils.RGBToColor(255, 245, 170, 255)
            for qi, (qx, qy) in enumerate(self.snap_target_queue, start=1):
                qxs, qys = to_screen(qx, qy)
                shapes.DLCircleFilled(qxs, qys, 3.0, q_fill, 12)
                shapes.DLCircle(qxs, qys, 6.0, q_ring, 14, 1.5)
                PyImGui.draw_list_add_text(qxs + 8.0, qys - 9.0, q_text, str(qi))
