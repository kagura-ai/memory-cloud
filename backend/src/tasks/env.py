"""Seconds-valued environment knobs of the scheduler jobs, read at call time."""

import math
import os

from utils.logger import get_logger

logger = get_logger(__name__)


def env_seconds(name: str, default: float, maximum: float | None = None) -> float:
    """A non-negative number of seconds from ``name``. Read at call time.

    An unparsable, negative or above-``maximum`` value falls back to
    ``default`` and is logged, like the tombstone retention window, so a typo
    never turns into a wait without bound or a job silently switched off.

    Args:
        name: The environment variable.
        default: The value when unset or invalid.
        maximum: The largest valid value, if any.

    Returns:
        The seconds.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
        logger.warning("env_seconds_invalid", name=name, value=raw, fallback=default)
        return default
    return value


def env_interval_seconds(name: str, default: int) -> int:
    """Whole seconds between the runs of an interval job; 0 turns the job off.

    Args:
        name: The environment variable.
        default: The interval when unset or invalid.

    Returns:
        The interval, at least 1 unless the job is off.
    """
    seconds = env_seconds(name, default)
    # A fraction of a second still means "on": never round it down to off.
    return max(1, int(seconds)) if seconds > 0 else 0
