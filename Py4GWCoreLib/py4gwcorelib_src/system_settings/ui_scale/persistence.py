"""Machine-wide persistence for the overlay UI scale."""

from . import model


# Global scope, not account scope: the readable text size belongs to the display
# you are sitting in front of, not to a character. Switching accounts on the same
# machine must not change how big the overlay is.
_DOCUMENT = "Widgets/System/UI Scale.ini"
_SECTION = "UI"
_KEY = "scale"


def _settings():
    try:
        from Py4GWCoreLib.py4gwcorelib_src.Settings import Settings

        return Settings(_DOCUMENT, "global")
    except Exception:
        return None


def load() -> model.UiScaleConfig:
    """Load the persisted scale, defaulting to 1.0 and clamping stale values."""

    config = model.default_config()
    settings = _settings()
    if settings is not None:
        config.scale = model.clamp(settings.get_float(_SECTION, _KEY, config.scale))
    return config


def save(config: model.UiScaleConfig) -> None:
    """Persist the scale through the sanctioned Settings wrapper."""

    settings = _settings()
    if settings is not None:
        settings.set_float(_SECTION, _KEY, config.scale)
