"""Shared Time Memory (``type='time'``) window query (#877, hoisted for #1276).

The upcoming-window overlap query historically lived inline in the
``recall_upcoming`` MCP handler. F2 (``get_agent_bootstrap``) composes the same
query, so it is hoisted here as the single implementation both the standalone
tool and the bootstrap service consume — the ``AgentStateService`` dual-surface
pattern. Same predicate, same clamps, no second implementation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Clamp bounds shared by every caller — a missing lower bound let k<=0 through
# as LIMIT 0 (always empty) or LIMIT -1 (no cap, bypassing the ceiling).
UPCOMING_K_DEFAULT = 20
UPCOMING_K_MIN = 1
UPCOMING_K_MAX = 100


def clamp_upcoming_k(raw_k: Any) -> int:
    """Coerce + clamp the ``k`` argument into ``[1, 100]`` (default 20).

    Raises ``ValueError`` on a non-integer so the caller can surface a
    structured ``validation_error`` (matches the pre-hoist handler behavior).
    """
    if raw_k is None:
        raw_k = UPCOMING_K_DEFAULT
    k = int(raw_k)  # may raise ValueError/TypeError → caller maps to 422
    return max(UPCOMING_K_MIN, min(k, UPCOMING_K_MAX))


async def query_upcoming_time_memories(
    db: AsyncSession,
    context_id: UUID,
    *,
    q_from: str | None,
    q_until: str | None,
    k: int,
) -> list[dict[str, Any]]:
    """Return the context's Time Memories whose window overlaps ``[q_from, q_until]``.

    ``q_from`` / ``q_until`` are fixed-width ISO strings (or None = unbounded)
    already normalized by ``utils.time_trigger.parse_query_bound``; the
    ``trigger_from`` / ``trigger_until`` columns are TEXT fixed-width ISO so the
    lexical comparison equals a chronological one. Soonest-first, capped at
    ``k``. Result rows are byte-compatible with the ``recall_upcoming`` handler
    (``memory_id`` / ``summary`` / ``type`` / ``details``).
    """
    from models.memory import Memory

    query = (
        select(Memory)
        .where(Memory.deleted_at.is_(None))
        .where(Memory.type == "time")
        .where(Memory.context_id == context_id)
    )
    # Window overlap: stored [trigger_from, trigger_until] overlaps the query
    # window [q_from, q_until] iff trigger_until >= q_from AND trigger_from <= q_until.
    if q_from is not None:
        query = query.where(Memory.trigger_until >= q_from)
    if q_until is not None:
        query = query.where(Memory.trigger_from <= q_until)
    query = query.order_by(Memory.trigger_from.asc()).limit(k)

    rows = (await db.execute(query)).scalars().all()
    return [
        {
            "memory_id": str(m.id),
            "summary": m.summary,
            "type": m.type,
            "details": m.details,
        }
        for m in rows
    ]
