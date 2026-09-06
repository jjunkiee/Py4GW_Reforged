"""Py4GW widget host — the always-on script the C++ DLL runs (`g_widget_host`).

The DLL calls this module's ``main()`` **every draw frame** (via ExecuteDraw, which runs both
``draw()`` and ``main()``). It has two jobs now:

  1. **Bootstrap once** — resolve the manager settings key, discover widgets, apply the saved
     enabled-state (forcing System widgets on). Widgets then run via their C++ PyCallbacks.
  2. **Render the launchpad every frame** — the launchpad (LaunchBar) is the widget-manager UI,
     and this always-on host is where it must be drawn (HEAD drew the old WM UI here). Without
     this the launchpad has no host and nothing appears on screen.

Both are made bulletproof: a missing/broken settings file must never stop the launchpad — the
cornerstone UI — from rendering.
"""

import os
import sys

from Py4GWCoreLib.py4gwcorelib_src.Settings import Settings
from Py4GWCoreLib.py4gwcorelib_src.WidgetManager import WidgetHandler
from Py4GWCoreLib.py4gwcorelib_src.WidgetManager import get_widget_handler
from Py4GWCoreLib.py4gwcorelib_src.launch_bar.launchpad import register_launchpad_once

MODULE_NAME = "Widget Manager"

# --- TEMPORARY: Phase 1 boot-closure probe -----------------------------------
# Proves the static boot closure against a live client, per
# docs/architecture/plans/refactor-sequencing.md. Revert this block once the
# capture is taken; it exists to be measured with, not to ship.
#
# Three stages, because they answer different questions:
#   import         - the host's module-level imports only. This is the stage the
#                    static boot closure actually models, so it is the only
#                    apples-to-apples comparison.
#   post_discover  - after discover(). Discovery only walks the filesystem, so
#                    any repository module appearing here is a surprise.
#   post_bootstrap - after _apply_ini_configuration(), which loads and enables
#                    every saved-enabled widget plus the forced System tier.
#                    The difference from "import" is what the MVP gate proposes
#                    to stop paying at startup.
_BOOT_CAPTURE_STAGES: dict[str, list[str]] = {}
_BOOT_CAPTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boot-capture.json")


def _capture_stage(label: str) -> None:
    """Record the loaded-module names at *label*. Never raises."""

    try:
        _BOOT_CAPTURE_STAGES[label] = sorted(sys.modules)
    except Exception:
        pass


def _write_boot_capture() -> None:
    """Write the capture once. Never raises: the launchpad outranks the probe."""

    try:
        import json

        files: dict[str, str] = {}
        for name, module in list(sys.modules.items()):
            path = getattr(module, "__file__", None)
            if isinstance(path, str):
                files[name] = path
        payload = {
            "meta": {
                "probe": "Py4GW_widget_manager.py Phase 1 boot-closure probe",
                "python": sys.version,
                "cwd": os.getcwd(),
                "host_file": os.path.abspath(__file__),
            },
            "stages": _BOOT_CAPTURE_STAGES,
            "module_files": files,
        }
        with open(_BOOT_CAPTURE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        _log("boot capture written: %s" % _BOOT_CAPTURE_PATH)
    except Exception as exc:
        _log("boot capture failed: %s" % exc)


_capture_stage("import")
# --- end TEMPORARY probe -----------------------------------------------------

widget_manager: WidgetHandler = get_widget_handler()

INI_KEY = ""
INI_PATH = "Widgets/WidgetManager"
INI_FILENAME = "WidgetManager.ini"


def _log(msg: str) -> None:
    try:
        import PySystem

        PySystem.Console.Log(MODULE_NAME, msg, PySystem.Console.MessageType.Warning)
    except Exception:
        pass


def _bootstrap_once() -> None:
    """Resolve settings + discover widgets + apply saved state. Retries each frame until done."""

    global INI_KEY
    if INI_KEY:
        return
    try:
        if not os.path.exists(INI_PATH):
            os.makedirs(INI_PATH, exist_ok=True)

        cfg = Settings(f"{INI_PATH}/{INI_FILENAME}", "account")
        key = cfg.name
        if not key:
            return  # settings not ready yet — retry next frame (launchpad still renders below)

        # Order is load-bearing: MANAGER_INI_KEY must be set before discovery (it reads each
        # widget's saved-enabled state during load), then _apply_ini_configuration re-applies
        # and force-enables System widgets.
        INI_KEY = key
        widget_manager.MANAGER_INI_KEY = INI_KEY
        widget_manager.discover()
        _capture_stage("post_discover")  # TEMPORARY: Phase 1 probe
        widget_manager.enable_all = bool(cfg.get_bool("Configuration", "enable_all", True))
        widget_manager._apply_ini_configuration()
    except Exception as exc:
        _log("bootstrap error (will retry): %s" % exc)
    finally:
        # TEMPORARY: Phase 1 probe. In finally, because a single widget raising
        # during enable must not cost a whole game launch: INI_KEY is already set
        # by then, so this function will not run again to try a second time.
        if INI_KEY and "post_bootstrap" not in _BOOT_CAPTURE_STAGES:
            _capture_stage("post_bootstrap")
            _write_boot_capture()


def update():
    return  # widgets run via C++ callbacks; nothing on the update loop here


def draw():
    return  # nothing here; the launchpad renders via its own registered Draw callback


def main():
    """Called every draw frame by the widget host. Now only lifecycle: register the launchpad
    callback once and run the settings/discovery bootstrap once. The launchpad itself renders
    through its own Draw callback, so this host's steady-state per-frame cost is ~nil."""

    register_launchpad_once()
    _bootstrap_once()


if __name__ == "__main__":
    main()
