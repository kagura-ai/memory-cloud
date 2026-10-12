"""The one definition of a workspace's memory count (#1946).

Every comparison against a plan's ``memory_limit`` — the remember quota gate
(``QuotaService.check_memory_quota``), the plan page, the workspace stats card,
the downgrade eligibility check, the capacity lock (#1941) and the resource
indexer's room (#1939) — counts the same rows: memories whose ``workspace_id``
is this workspace and that are not soft-deleted.

Before this module the gate and the plan page counted by member ``user_id``
instead, which double-counted a member's memories from their other workspaces
(refusing ``remember`` before the limit was reached) and silently dropped rows
whose author had left the workspace.

This module is a leaf: it depends only on the ``Memory`` model and SQLAlchemy,
so ``quota_service``, ``workspace_service`` and the routes can all import it
without a cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory


def live_memory_count_stmt(workspace_id: UUID) -> Select[int]:
    """``SELECT count(*)`` of the workspace's live (not soft-deleted) memories.

    Exposed for callers that run their own scalar reads in a batch; most
    callers want :func:`count_live_memories`.
    """
    return select(func.count(Memory.id)).where(
        Memory.workspace_id == workspace_id,
        Memory.deleted_at.is_(None),
    )


async def count_live_memories(db: AsyncSession, workspace_id: UUID) -> int:
    """Number of live memories the workspace holds (the figure every
    ``memory_limit`` check compares against)."""
    result = await db.execute(live_memory_count_stmt(workspace_id))
    return int(result.scalar() or 0)


async def live_memory_counts_by_workspace(
    db: AsyncSession, workspace_ids: Sequence[UUID]
) -> dict[UUID, int]:
    """The same count for many workspaces in one grouped query.

    Workspaces with no live memories are absent from the result (callers use
    ``.get(ws_id, 0)``); an empty ``workspace_ids`` issues no query.
    """
    if not workspace_ids:
        return {}
    result = await db.execute(
        select(Memory.workspace_id, func.count(Memory.id).label("memory_count"))
        .where(
            Memory.workspace_id.in_(list(workspace_ids)),
            Memory.deleted_at.is_(None),
        )
        .group_by(Memory.workspace_id)
    )
    return {row.workspace_id: int(row.memory_count) for row in result.all()}
