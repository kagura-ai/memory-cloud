"""Once-per-session marker for reference() reads (#2139).

A ``reference(content_offset=...)`` continuation counts as an adoption (#1046)
only when it is the session's first read of that memory: an agent that answers
from a recall result's cut content and reads on from ``content_next_offset``
fetched Layer-3 detail, but later pages of the same read must not count again.

The "already read in this session" state is one Redis key per (principal,
session, memory), set with ``SET NX EX``: atomic, so parallel continuations in
one session count once. A session id is an attribute of the principal (user
sub + credential's agent), not an identity, so both are part of the key. The
key holds ids only, never content.
"""

from __future__ import annotations

from uuid import UUID

from db.redis import get_redis_client
from utils.logger import get_logger

logger = get_logger(__name__)

# Stateful MCP sessions idle out after an hour, but baggage / _meta / header
# sessions have no server-side lifetime and a run can span a day. A key that
# expires inside a longer session allows at most one extra count.
REFERENCE_ADOPTION_SESSION_TTL_SECONDS = 86_400

_KEY_PREFIX = "reference_read"


def reference_read_key(
    *, user_id: str, agent_id: UUID | None, session_id: str, memory_id: UUID
) -> str:
    """The Redis key marking ``memory_id`` as read in this principal's session."""
    agent = str(agent_id) if agent_id is not None else "-"
    return f"{_KEY_PREFIX}:{user_id}:{agent}:{session_id}:{memory_id}"


async def mark_first_read(
    *, user_id: str, agent_id: UUID | None, session_id: str, memory_id: UUID
) -> bool:
    """Mark ``memory_id`` as read in the session; True only for the first mark.

    Fails closed: any Redis error is logged (``error_type`` only — the error
    text can carry the connection URL) and reads as "not first", so a
    continuation is not counted, as before #2139. Never raises.
    """
    key = reference_read_key(
        user_id=user_id, agent_id=agent_id, session_id=session_id, memory_id=memory_id
    )
    try:
        created = await get_redis_client().set(
            key, "1", nx=True, ex=REFERENCE_ADOPTION_SESSION_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails a read
        logger.warning("reference_adoption_mark_failed", error_type=type(exc).__name__)
        return False
    return bool(created)


async def unmark_read(
    *, user_id: str, agent_id: UUID | None, session_id: str, memory_id: UUID
) -> None:
    """Remove the session's mark for ``memory_id`` (its adoption did not land).

    Best-effort like :func:`mark_first_read`: a Redis error is logged
    (``error_type`` only) and the mark expires with its TTL. Never raises.
    """
    key = reference_read_key(
        user_id=user_id, agent_id=agent_id, session_id=session_id, memory_id=memory_id
    )
    try:
        await get_redis_client().delete(key)
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails a read
        logger.warning("reference_adoption_unmark_failed", error_type=type(exc).__name__)
