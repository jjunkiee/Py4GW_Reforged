"""Overlay UI-scale controller.

Owns the persisted scale and reconciles it onto ``ImGuiStyle.FontScaleMain``,
the ImGui 1.92 replacement for the obsolete ``io.FontGlobalScale`` (and for the
now no-op ``set_window_font_scale``). Final glyph size is
``FontSizeBase * FontScaleMain * FontScaleDpi``, so this scales every window's
text, including text a widget pushes at an explicit pixel size.

Fonts are loaded dynamically at a design size of 14 with automatic oversampling,
so raising the scale re-bakes glyphs at the larger size rather than magnifying a
14px bitmap.
"""

from typing import Optional

import PyImGui

from . import model
from . import persistence


_FEATURE_NAME = "UI Scale"


def _log(message: str) -> None:
    try:
        import PySystem

        PySystem.Console.Log(_FEATURE_NAME, message, PySystem.Console.MessageType.Warning)
    except Exception:
        pass


class UiScaleController:
    """Own the persisted overlay scale and keep the live ImGui style matching it."""

    def __init__(self) -> None:
        self.config: model.UiScaleConfig = persistence.load()
        self._warned: bool = False

    def preview_scale(self, scale: float) -> None:
        """Set the scale in memory without touching disk.

        Used while a slider is being dragged: the reconcile in :meth:`apply`
        picks the value up on the next frame, so the user sees it immediately
        without writing the ini once per frame of the drag.
        """

        self.config.scale = model.clamp(scale)

    def set_scale(self, scale: float) -> None:
        """Set the scale and persist it."""

        self.config.scale = model.clamp(scale)
        persistence.save(self.config)

    def save(self) -> None:
        """Persist the current in-memory scale (commits a finished drag)."""

        persistence.save(self.config)

    def reset(self) -> None:
        """Restore the default 1.0 scale and persist it."""

        self.set_scale(model.DEFAULT_SCALE)

    def apply(self) -> None:
        """Reconcile the live ImGui style with the configured scale.

        Called every frame from the render thread. In the common case this is a
        float compare and nothing else. It deliberately re-applies instead of
        running once at boot: a D3D9 device reset (alt-tab, resolution change)
        re-runs the native ImGui initialisation, which restores the default
        style and would otherwise silently drop the scale back to 1.0 until the
        next widget reload.

        Writing the style here rather than from the slider also keeps a frame
        internally consistent: the whole frame is drawn at one scale instead of
        changing size halfway down the settings window.
        """

        try:
            if model.differs(PyImGui.get_global_font_scale(), self.config.scale):
                PyImGui.set_global_font_scale(self.config.scale)
        except Exception as exc:
            # Log once. This runs every frame; a broken binding must not turn
            # into sixty console lines a second.
            if not self._warned:
                self._warned = True
                _log("could not apply the overlay scale: %s" % exc)


_controller: Optional[UiScaleController] = None


def get_controller() -> UiScaleController:
    """Return the process-wide overlay-scale controller."""

    global _controller
    if _controller is None:
        _controller = UiScaleController()
    return _controller
