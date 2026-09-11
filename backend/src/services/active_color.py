"""Read the blue-green deploy marker on demand (#1482).

The marker names the color **serving traffic right now**; ``deploy.sh``
publishes it in place after the new color passes readiness. It is the single
source of truth for "which color is live", so it is read from disk on every
call — never cached at import or startup, or a color switch would leave this
process reporting the drained color for the rest of its life.

The read is deliberately strict: a missing, unreadable, oversized or unknown
marker is an error, not a fallback to ``blue``. A guessed color is exactly
the failure the marker exists to prevent.
"""

from __future__ import annotations

from typing import Literal, cast

from config.constants import DEPLOY_COLORS
from utils.exceptions import ActiveColorUnavailableError

# The marker is one short word plus a newline. Anything longer is not a
# marker, so the read is bounded — a stray large file at that path cannot be
# pulled into memory or echoed back to a caller.
_MARKER_MAX_BYTES = 64

DeployColor = Literal["blue", "green"]
MarkerFailure = Literal["missing", "unreadable", "invalid"]


def read_active_color(path: str) -> DeployColor:
    """Return the color named by the marker at ``path``.

    Raises:
        ActiveColorUnavailableError: with ``reason`` = ``missing`` (no file),
            ``unreadable`` (permissions, a directory in its place — what
            Docker leaves behind when the host file did not exist at
            ``compose up``), or ``invalid`` (empty, oversized, not a known
            color).
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MARKER_MAX_BYTES + 1)
    except FileNotFoundError as exc:
        raise ActiveColorUnavailableError(reason="missing", path=path) from exc
    except OSError as exc:  # PermissionError, IsADirectoryError, EIO, ...
        raise ActiveColorUnavailableError(reason="unreadable", path=path) from exc

    if len(raw) > _MARKER_MAX_BYTES:
        raise ActiveColorUnavailableError(reason="invalid", path=path)
    color = raw.decode("ascii", errors="replace").strip().lower()
    if color not in DEPLOY_COLORS:
        raise ActiveColorUnavailableError(reason="invalid", path=path)
    return cast(DeployColor, color)
