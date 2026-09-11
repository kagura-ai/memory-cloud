"""Read the blue-green deploy marker on demand (#1482).

The marker names the color **serving traffic right now**; ``deploy.sh``
publishes it in place after the new color passes readiness. It is the single
source of truth for "which color is live", so it is read from disk on every
call — never cached at import or startup, or a color switch would leave this
process reporting the drained color for the rest of its life.

The read is deliberately strict, and it matches ``deploy.sh``'s own reader
byte for byte: exactly ``blue`` or ``green``, an optional trailing newline,
nothing else. A missing, unreadable, oversized, padded or unknown marker is an
error, not a fallback to ``blue`` — a guessed color is exactly the failure the
marker exists to prevent, and a normalization the marker's writer rejects
would let the two readers disagree about the same file.
"""

from __future__ import annotations

import asyncio
from typing import cast

from config.constants import DEPLOY_COLORS, DeployColor
from utils.exceptions import ActiveColorUnavailableError

# The marker is one short word plus a newline. Anything longer is not a
# marker, so the read is bounded — a stray large file at that path cannot be
# pulled into memory or echoed back to a caller.
_MARKER_MAX_BYTES = 64

# deploy.sh republishes the marker IN PLACE (cp onto the live inode, so
# single-file bind mounts stay current). cp opens with O_TRUNC, so a read that
# lands inside the truncate->write window sees zero bytes. That window is
# microseconds; one re-read after this pause is enough to step over it.
_EMPTY_REREAD_DELAY_S = 0.05


def read_active_color(path: str) -> DeployColor:
    """Return the color named by the marker at ``path`` (one read, no retry).

    Raises:
        ActiveColorUnavailableError: with ``reason`` = ``missing`` (no file),
            ``unreadable`` (permissions, a directory in its place — what
            Docker leaves behind when the host file did not exist at
            ``compose up``), or ``invalid`` (empty, oversized, padded, wrong
            case, not a known color). Every reason can be transient while
            the marker is being republished; see
            :func:`read_active_color_settled`.
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
    # Only the trailing newline is tolerated (``echo blue >`` writes one).
    # Leading whitespace or a capital letter is refused by deploy.sh, so it is
    # refused here too.
    color = raw.decode("ascii", errors="replace").rstrip("\r\n")
    if color not in DEPLOY_COLORS:
        raise ActiveColorUnavailableError(reason="invalid", path=path, empty=not raw)
    return cast(DeployColor, color)


async def read_active_color_settled(path: str) -> DeployColor:
    """:func:`read_active_color`, re-reading once if the first read was empty.

    The only failure this masks is the in-place republish window described
    at ``_EMPTY_REREAD_DELAY_S``. A marker that is genuinely empty still
    fails, one pause later, with the same ``invalid`` reason.
    """
    try:
        return read_active_color(path)
    except ActiveColorUnavailableError as exc:
        if not exc.empty:
            raise
    await asyncio.sleep(_EMPTY_REREAD_DELAY_S)
    return read_active_color(path)
