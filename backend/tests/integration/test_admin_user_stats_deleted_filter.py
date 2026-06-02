"""Regression test for #871 — admin user-stats/detail must exclude soft-deleted memories.

``admin.py``'s ``get_user_stats`` / ``get_user_detail`` counted ALL memories for
a user (no ``deleted_at IS NULL`` filter), so a user with soft-deleted memories
showed an inflated count in the admin user-detail view, while the user *list*
(``admin.py:254``) and the workspace quota (``workspace_service.py:919``) — both
of which filter ``deleted_at IS NULL`` — showed the correct (smaller) number.
This is the admin-side recurrence of #198 Bug D (the same fix on the quota path).

Pins both handlers so their total / working / persistent counts exclude
soft-deleted rows and therefore agree with the list/quota paths. Hits a real
Postgres test DB because the count is computed inline in the handler against
live rows.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import get_user_detail, get_user_stats
from auth.workspace_roles import WorkspaceRole
from models.auth import WorkspaceMember
from models.memory import Memory

from ._admin_helpers import make_context, make_user, make_workspace, mock_admin


def _make_memory(*, user_id, workspace_id, context_id, scope: str, deleted: bool) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        summary=f"mem-{uuid.uuid4().hex[:6]}",
        content="x",
        type="note",
        client="test",
        scope=scope,
        deleted_at=(func.now() if deleted else None),
    )


@pytest_asyncio.fixture
async def user_with_soft_deleted_memories(db_session: AsyncSession) -> dict:
    """User owns 3 live memories (2 working + 1 persistent) and 2 soft-deleted.

    The live counts (total=3 / working=2 / persistent=1) are exactly what the
    list (``admin.py:254``) and quota (``workspace_service.py:919``) paths
    produce, so asserting them pins cross-path consistency.
    """
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id)
    db_session.add(ws)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role=WorkspaceRole.OWNER)
    )

    ctx = make_context(workspace_id=ws.id, created_by=user.user_id)
    db_session.add(ctx)
    await db_session.flush()

    db_session.add_all(
        [
            _make_memory(
                user_id=user.user_id,
                workspace_id=ws.id,
                context_id=ctx.id,
                scope="working",
                deleted=False,
            ),
            _make_memory(
                user_id=user.user_id,
                workspace_id=ws.id,
                context_id=ctx.id,
                scope="working",
                deleted=False,
            ),
            _make_memory(
                user_id=user.user_id,
                workspace_id=ws.id,
                context_id=ctx.id,
                scope="persistent",
                deleted=False,
            ),
            # soft-deleted — must NOT be counted by the user-stats/detail views
            _make_memory(
                user_id=user.user_id,
                workspace_id=ws.id,
                context_id=ctx.id,
                scope="working",
                deleted=True,
            ),
            _make_memory(
                user_id=user.user_id,
                workspace_id=ws.id,
                context_id=ctx.id,
                scope="persistent",
                deleted=True,
            ),
        ]
    )
    await db_session.commit()

    return {"user_id": user.user_id}


@pytest.mark.asyncio
async def test_get_user_stats_excludes_soft_deleted(
    user_with_soft_deleted_memories: dict, db_session: AsyncSession
) -> None:
    result = await get_user_stats(
        user_id=user_with_soft_deleted_memories["user_id"],
        admin=mock_admin(),
        db=db_session,
    )
    # Without the deleted_at filter these would be 5 / 3 / 2.
    assert result.memories["total"] == 3
    assert result.memories["working"] == 2
    assert result.memories["persistent"] == 1


@pytest.mark.asyncio
async def test_get_user_detail_excludes_soft_deleted(
    user_with_soft_deleted_memories: dict, db_session: AsyncSession
) -> None:
    result = await get_user_detail(
        user_id=user_with_soft_deleted_memories["user_id"],
        admin=mock_admin(),
        db=db_session,
    )
    # Without the deleted_at filter these would be 5 / 3 / 2.
    assert result.stats["total_memories"] == 3
    assert result.stats["working_memories"] == 2
    assert result.stats["persistent_memories"] == 1
