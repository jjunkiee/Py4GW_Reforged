"""Pure configuration data for the overlay UI-scale feature."""

from dataclasses import dataclass


#: Supported range for the global overlay scale.
#:
#: The floor is not 0.0 because a scale small enough to be unreadable is also
#: small enough that the settings window you would need to fix it with becomes
#: unreadable too. The ceiling is a judgement call, not a hard limit: the scale
#: multiplies text but not padding or button rects, so widgets with fixed pixel
#: layouts start crowding well before 2.5x.
MIN_SCALE: float = 0.75
MAX_SCALE: float = 2.5
DEFAULT_SCALE: float = 1.0

#: Slider and reconcile comparisons are float equality in disguise; this is the
#: tolerance below which two scales count as the same value.
EPSILON: float = 1e-4


@dataclass
class UiScaleConfig:
    """Persisted overlay scale."""

    scale: float = DEFAULT_SCALE


def clamp(scale: float) -> float:
    """Clamp a requested scale into the supported range."""

    try:
        value = float(scale)
    except (TypeError, ValueError):
        return DEFAULT_SCALE
    return max(MIN_SCALE, min(MAX_SCALE, value))


def differs(left: float, right: float) -> bool:
    """Return True when two scales differ by more than :data:`EPSILON`."""

    return abs(float(left) - float(right)) > EPSILON


def default_config() -> UiScaleConfig:
    """Return a fresh default configuration."""

    return UiScaleConfig()
