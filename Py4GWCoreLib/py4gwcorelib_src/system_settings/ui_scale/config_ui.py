"""Overlay UI-scale settings UI hosted by System Settings."""

from typing import TYPE_CHECKING

import PyImGui

from . import model
from .controller import UiScaleController
from .controller import get_controller

if TYPE_CHECKING:
    # Type-only: the settings window is built lazily, and importing the ImGui
    # window stack at module import time would defeat that.
    from Py4GWCoreLib.ImGui_src.SidebarWindow import SidebarWindow
    from ..window import SystemSettingsWindow


_MUTED = (0.60, 0.60, 0.65, 1.0)
_WARN = (0.95, 0.75, 0.35, 1.0)

_PRESETS = (
    ("100%", 1.00),
    ("125%", 1.25),
    ("150%", 1.50),
    ("200%", 2.00),
)


def _draw(controller: UiScaleController) -> None:
    current = controller.config.scale

    requested = PyImGui.slider_float(
        "Overlay text scale", current, model.MIN_SCALE, model.MAX_SCALE, "%.2fx"
    )
    if model.differs(requested, current):
        # In-memory only while dragging; the ini write waits for the release
        # below so a drag does not write the file once per frame.
        controller.preview_scale(requested)
    if PyImGui.is_item_deactivated_after_edit():
        controller.save()

    for index, (label, value) in enumerate(_PRESETS):
        if index:
            PyImGui.same_line(0.0, -1.0)
        if PyImGui.small_button(label):
            controller.set_scale(value)

    PyImGui.spacing()
    PyImGui.separator()
    PyImGui.text_wrapped(
        "Scales the text of every Py4GW overlay and widget. Takes effect on the "
        "next frame and is saved for this machine, so it survives an account switch."
    )
    PyImGui.text_colored(
        "Text only: window padding, button sizes and icon rects do not scale with "
        "it, so widgets built on fixed pixel layouts get tighter as you go up. "
        "Past roughly 1.5x, expect some crowding.",
        _WARN,
    )
    PyImGui.text_colored(
        "Guild Wars' own interface is not affected; this is the Py4GW overlay only.",
        _MUTED,
    )


def add_sections(win: "SystemSettingsWindow", group: "SidebarWindow.Group") -> None:
    """Add the overlay-scale settings section to the System category."""

    controller = get_controller()
    win.add_section(
        group,
        "Overlay Scale",
        lambda c=controller: _draw(c),
    )
