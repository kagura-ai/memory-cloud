"""#1525: ``--rollback-to`` without ``--requeue-since`` on a real database.

The default re-queue boundary is ``ContextSearchConfig.updated_at``, an
aware ``TIMESTAMP WITH TIME ZONE``, while the columns it is compared with
(``Memory.created_at`` / ``updated_at`` / ``embedding_attempted_at`` /
``deleted_at``) are naive UTC. asyncpg refuses an aware value bound against a
naive column, so the rollback normalises the boundary to naive UTC first.

Real Postgres: the predicates and the re-queue UPDATE run as SQL.
"""

from __future__ import annotations

from datetime import UTC, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory
from services import embedding_migration_service as svc
from utils.datetime import utcnow

SOURCE = "text-embedding-3-small"
TARGET = "qwen3-embedding:4b"
TARGET_DIMENSIONS = 2560


async def _scope(db, owner: str) -> Context:
    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"rb-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"rb-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return ctx


def _memory(owner: str, ctx: Context, **values) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ctx.workspace_id,
        context_id=ctx.id,
        user_id=owner,
        summary="rollback default boundary",
        content="c",
        type="note",
        client="pytest",
        scope="working",
        embedding_status="success",
        **values,
    )


@pytest.mark.asyncio
async def test_rollback_defaults_to_the_recorded_switch_time(db_session):
    owner = f"rb-user-{uuid4().hex[:8]}"
    ctx = await _scope(db_session, owner)
    await db_session.commit()

    # The switch being undone: a pure routing flip stamps the config row.
    await svc.switch_context_embedding(db_session, ctx.id, TARGET, TARGET_DIMENSIONS)
    switched_at = (
        await db_session.execute(
            select(ContextSearchConfig.updated_at).where(ContextSearchConfig.context_id == ctx.id)
        )
    ).scalar_one()
    assert switched_at.tzinfo is not None  # the aware column this guards against
    switched_naive = switched_at.astimezone(UTC).replace(tzinfo=None)

    before = switched_naive - timedelta(days=30)
    after = switched_naive + timedelta(minutes=1)
    untouched = _memory(owner, ctx, created_at=before, embedding_attempted_at=before)
    written_on_target = _memory(owner, ctx, created_at=after, embedding_attempted_at=after)
    edited_on_target = _memory(
        owner, ctx, created_at=before, updated_at=after, embedding_attempted_at=after
    )
    forgotten_on_target = _memory(
        owner, ctx, created_at=before, embedding_attempted_at=before, deleted_at=after
    )
    rows = [untouched, written_on_target, edited_on_target, forgotten_on_target]
    db_session.add_all(rows)
    await db_session.commit()

    client = MagicMock()
    client.collection_exists = AsyncMock(return_value=True)
    with (
        patch.object(svc, "get_qdrant_client", return_value=client),
        patch.object(svc, "delete_points_from_qdrant", AsyncMock()) as delete_points,
        patch.object(svc, "delete_chunk_points", AsyncMock()),
    ):
        result = await svc.rollback_context_embedding(db_session, ctx.id, SOURCE)

    assert result.model == SOURCE
    assert result.requeued == 2
    assert result.stale_removed == 1
    delete_points.assert_awaited_once()
    result_rows = await db_session.execute(
        select(Memory.id, Memory.embedding_status).where(Memory.id.in_([row.id for row in rows]))
    )
    statuses = dict(result_rows.all())
    assert statuses == {
        untouched.id: "success",
        written_on_target.id: "pending",
        edited_on_target.id: "pending",
        forgotten_on_target.id: "success",
    }


@pytest.mark.asyncio
async def test_switch_accepts_an_aware_requeue_since(db_session):
    owner = f"rb-user-{uuid4().hex[:8]}"
    ctx = await _scope(db_session, owner)
    since = utcnow()
    fresh = _memory(
        owner,
        ctx,
        created_at=since + timedelta(minutes=1),
        embedding_attempted_at=since + timedelta(minutes=1),
    )
    db_session.add(fresh)
    await db_session.commit()

    # The same instant at +09:00: compared as naive UTC, not as wall-clock +9h.
    aware = since.replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=9)))
    with (
        patch.object(svc, "delete_points_from_qdrant", AsyncMock()),
        patch.object(svc, "delete_chunk_points", AsyncMock()),
    ):
        result = await svc.switch_context_embedding(
            db_session, ctx.id, TARGET, TARGET_DIMENSIONS, requeue_since=aware
        )

    assert result.requeued == 1
