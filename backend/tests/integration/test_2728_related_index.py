"""Real PostgreSQL checks; skipped explicitly when TEST_DATABASE_URL is absent."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from models.auth import Context, Workspace
from models.memory import Memory, NeuralMemoryEdge
from models.sleep import SleepRelatedIndex
from services.memory_service import MemoryService
from services.recall_related_index import expand_recall
from services.sleep.related_index import candidate_links, evidence
from tests.services.sleep.test_related_index import reader


@pytest.mark.asyncio
async def test_cancelled_related_read_leaves_caller_transaction_usable(db_session, monkeypatch):
    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    monkeypatch.setenv("RECALL_RELATED_INDEX_TIMEOUT_MS", "25")
    source = Memory(id=uuid4(), user_id="test", context_id=uuid4(), workspace_id=uuid4())
    _, args = reader(source, [])
    service = MemoryService(db_session)

    async def slow(db, *args, **kwargs):
        await db.execute(text("SELECT pg_sleep(1)"))

    assert await db_session.scalar(text("SELECT 1")) == 1
    with patch("services.recall_related_index.candidate_links", slow):
        assert await expand_recall(service, **args) == ([], {})
    assert await db_session.scalar(text("SELECT 1")) == 1


@pytest.mark.asyncio
async def test_index_query_scope_and_live_supersession(db_session):
    db = db_session
    ws = Workspace(id=uuid4(), name="related-index-test", owner_user_id="test")
    db.add(ws)
    await db.flush()
    ctx = Context(id=uuid4(), workspace_id=ws.id, name="related-index-test", created_by="test")
    db.add(ctx)
    await db.flush()
    memories = [
        Memory(
            id=uuid4(),
            user_id="test",
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"Fictional source number {n}",
            content=f"Fictional content {n}",
            type="fact",
            client="pytest",
        )
        for n in range(3)
    ]
    db.add_all(memories)
    await db.flush()
    source, target, newer = memories
    edge = NeuralMemoryEdge(
        user_id="test",
        workspace_id=ws.id,
        context_id=ctx.id,
        src_id=source.id,
        dst_id=target.id,
        origin="semantic",
        edge_type="related_to",
        weight=0.8,
    )
    db.add(edge)
    await db.flush()
    db.add(
        SleepRelatedIndex(
            edge_id=edge.id,
            source_id=source.id,
            target_id=target.id,
            context_id=ctx.id,
            workspace_id=ws.id,
            evidence={"source": evidence(source), "target": evidence(target)},
        )
    )
    await db.flush()
    assert len(await candidate_links(db, [source], context_id=ctx.id, workspace_id=ws.id)) == 1
    assert await candidate_links(db, [source], context_id=uuid4(), workspace_id=ws.id) == []
    assert await candidate_links(db, [source], context_id=ctx.id, workspace_id=uuid4()) == []
    db.add(
        NeuralMemoryEdge(
            user_id="test",
            workspace_id=ws.id,
            context_id=ctx.id,
            src_id=newer.id,
            dst_id=target.id,
            origin="declared",
            edge_type="supersedes",
            weight=1,
        )
    )
    await db.flush()
    assert await candidate_links(db, [source], context_id=ctx.id, workspace_id=ws.id) == []
