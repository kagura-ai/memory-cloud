"""The content chunk backfill on enable against a real Postgres (#2015 D4).

Turning ``content_chunks_enabled`` on re-queues the context's existing long
memories (``embedding_status`` back to ``pending``) in the same transaction as
the flag, so the embedding sweep rebuilds them with their chunk points. These
tests pin the UPDATE's predicate and what it leaves alone, and the pending
count the search-config response serves. The vector store is not touched (the
chunk purge is patched). Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from models.auth import Context, Workspace
from models.config import ContextSearchConfig
from models.memory import Memory
from models.schemas import ContextSearchConfigUpdate
from repositories.config_repository import ContextSearchConfigRepository
from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "chunk-backfill-it"
LONG = "x" * CONTENT_CHUNK_MIN_CONTENT_CHARS
SHORT = "x" * (CONTENT_CHUNK_MIN_CONTENT_CHARS - 1)
_STAMP = datetime(2026, 1, 2, 3, 4, 5)
_EDITED = datetime(2026, 2, 3, 4, 5, 6)
_ON = ContextSearchConfigUpdate(
    semantic_weight=0.6,
    bm25_weight=0.4,
    fetch_factor=3,
    use_rerank=False,
    reranker_provider="voyage",
    reranker_model="rerank-2",
    content_chunks_enabled=True,
)


async def _context(db, ws_id):
    ctx = Context(id=uuid4(), workspace_id=ws_id, name=f"bf-{uuid4().hex[:8]}", created_by=_USER)
    db.add(ctx)
    await db.flush()
    db.add(ContextSearchConfig(context_id=ctx.id, content_chunks_enabled=False))
    await db.flush()
    return ctx.id


def _memory(ws_id, ctx_id, label, *, content=LONG, status="success", **extra):
    return Memory(
        id=uuid4(),
        workspace_id=ws_id,
        context_id=ctx_id,
        user_id=_USER,
        summary=label,
        content=content,
        type="note",
        scope="working",
        client="test",
        created_at=utcnow(),
        updated_at=_EDITED,
        embedding_status=status,
        embedding_attempted_at=_STAMP,
        **extra,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session):
    ws = Workspace(
        id=uuid4(),
        name=f"bf-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=_USER,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.flush()
    ctx, other = await _context(db_session, ws.id), await _context(db_session, ws.id)
    rows = {
        "long": _memory(ws.id, ctx, "long"),
        "short": _memory(ws.id, ctx, "short", content=SHORT),
        "processing": _memory(ws.id, ctx, "processing", status="processing"),
        "failed": _memory(ws.id, ctx, "failed", status="failed"),
        "deleted": _memory(ws.id, ctx, "deleted", deleted_at=utcnow()),
        "resource": _memory(ws.id, ctx, "resource", details={"resource_id": "docs"}),
        "other_context": _memory(ws.id, other, "other"),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    # Plain ids: a rollback expires the ORM objects (no lazy load under asyncio).
    return ctx, {label: m.id for label, m in rows.items()}


async def _statuses(db, rows):
    result = await db.execute(
        select(Memory.id, Memory.embedding_status, Memory.embedding_attempted_at, Memory.updated_at)
        .where(Memory.id.in_(list(rows.values())))
        .execution_options(populate_existing=True)
    )
    by_id = {r.id: r for r in result.all()}
    return {label: by_id[memory_id] for label, memory_id in rows.items()}


async def test_enabling_requeues_exactly_the_long_live_success_rows(db_session, world):
    ctx, rows = world
    repo = ContextSearchConfigRepository(db_session)
    with patch.object(repo, "_purge_chunk_points", AsyncMock()):
        await repo.update(ctx, _ON)

    after = await _statuses(db_session, rows)
    assert {label for label, r in after.items() if r.embedding_status == "pending"} == {"long"}
    assert after["processing"].embedding_status == "processing"
    assert after["failed"].embedding_status == "failed"
    for r in after.values():
        # The pipeline's clock and the edit cue are never written.
        assert r.embedding_attempted_at == _STAMP
        assert r.updated_at == _EDITED

    # What the response serves: long, live, not success, not a resource row.
    assert await repo.content_chunk_backfill_pending(ctx) == 3  # long, processing, failed


async def test_a_failed_backfill_rolls_the_flag_back(db_session, world):
    ctx, rows = world
    repo = ContextSearchConfigRepository(db_session)
    with (
        patch.object(repo, "_purge_chunk_points", AsyncMock()),
        patch.object(repo, "_queue_content_chunk_backfill", AsyncMock(side_effect=RuntimeError)),
        pytest.raises(RuntimeError),
    ):
        await repo.update(ctx, _ON)

    flag = await db_session.scalar(
        select(ContextSearchConfig.content_chunks_enabled)
        .where(ContextSearchConfig.context_id == ctx)
        .execution_options(populate_existing=True)
    )
    assert flag is False
    assert (await _statuses(db_session, rows))["long"].embedding_status == "success"
