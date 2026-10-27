"""The #2085 seed fence against a real Postgres session.

An edit committed between the run's ``success`` write and its seed commit makes
``_create_knn_seed_edges`` drop its uncommitted seed edges with a rollback. A
root rollback expires every instance in the session, so the caller's
``memory`` must be usable again afterwards: the tag co-occurrence seeding that
follows reads ``memory.context_id`` / ``tags``, and on an expired instance
under ``AsyncSession`` that is a lazy load (``MissingGreenlet``), which escaped
as a spurious ``embedding_claim_lost`` and re-queued the row. The mocked-session
unit tests in ``tests/services/test_knn_seeding.py`` do not model the expiry.
The vector store and the embedding provider are stubbed; skipped when Postgres
is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import models.hub_tag  # noqa: F401  (hub_tag_cache: the tag seeding no-ops without it)
from models.auth import Context, User, Workspace
from models.memory import Memory, NeuralMemoryEdge
from neural.config import NeuralMemoryConfig
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_TAGS = ["seed-fence-alpha", "seed-fence-beta"]


@pytest.fixture
def seeding_env(monkeypatch: pytest.MonkeyPatch):
    """Seed edges and tag seeding on, the supersede detection off."""
    monkeypatch.setenv("KNN_SEED_ENABLED", "true")
    monkeypatch.setenv("TAG_COOCCURRENCE_ENABLED", "true")
    monkeypatch.setenv("SUPERSEDE_SUGGEST_ENABLED", "false")
    NeuralMemoryConfig.invalidate_cache()
    yield
    NeuralMemoryConfig.invalidate_cache()


@pytest_asyncio.fixture(loop_scope="session")
async def scope(db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:
    owner = f"seedfence_{uuid4().hex[:8]}"
    db_session.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db_session.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"seedfence-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.flush()
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"seedfence-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    def _memory(summary: str, status: str) -> Memory:
        return Memory(
            id=uuid4(),
            workspace_id=ws.id,
            context_id=ctx.id,
            user_id=owner,
            summary=summary,
            content=summary,
            type="note",
            scope="working",
            client="test",
            tags=list(_TAGS),
            created_at=utcnow(),
            embedding_status=status,
        )

    row = _memory("seed fence: the text this run embeds", "pending")
    neighbours = [_memory(f"seed fence neighbour {i}", "success") for i in range(2)]
    db_session.add_all([row, *neighbours])
    await db_session.commit()
    yield {"owner": owner, "row": row, "neighbours": neighbours}
    await db_session.rollback()
    await db_session.execute(delete(NeuralMemoryEdge).where(NeuralMemoryEdge.user_id == owner))
    await db_session.execute(delete(Memory).where(Memory.user_id == owner))
    await db_session.execute(delete(Context).where(Context.id == ctx.id))
    await db_session.execute(delete(Workspace).where(Workspace.id == ws.id))
    await db_session.execute(delete(User).where(User.user_id == owner))
    await db_session.commit()


async def test_tag_seeding_runs_after_the_seed_fence_rollback(
    async_engine: AsyncEngine, scope: dict[str, Any], seeding_env: None
) -> None:
    from services import memory_service

    row: Memory = scope["row"]
    neighbours: list[Memory] = scope["neighbours"]
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    async def _search_then_edit(**_kwargs: Any) -> list[dict[str, Any]]:
        # The edit lands after this run's success commit and before its seed
        # commit: update_memory commits a new text and re-queues the row.
        async with maker() as editor:
            await editor.execute(
                update(Memory)
                .where(Memory.id == row.id)
                .values(summary="seed fence: an edited subject", embedding_status="pending")
            )
            await editor.commit()
        return [{"id": str(row.id), "score": 1.0, "payload": {}}] + [
            {"id": str(n.id), "score": 0.9, "payload": {}} for n in neighbours
        ]

    svc = MagicMock(model="m")
    mock_logger = MagicMock()
    requeue = AsyncMock(return_value=True)
    try:
        with (
            patch("db.base.get_db", side_effect=_get_db),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch(
                "services.context_routing.resolve_context_routing",
                AsyncMock(return_value=("kagura_memories", svc)),
            ),
            patch.object(
                memory_service, "write_memory_point", AsyncMock(return_value=([0.1] * 8, {}))
            ),
            patch.object(memory_service, "write_content_chunks", AsyncMock()),
            patch("neural.calibration.resolve_knn_threshold", AsyncMock(return_value=0.5)),
            patch("db.qdrant.search_memories_qdrant", AsyncMock(side_effect=_search_then_edit)),
            patch.object(memory_service, "_reconcile_after_lost_claim", requeue),
            patch.object(memory_service, "logger", mock_logger),
        ):
            await memory_service.process_pending_embedding(row.id)
    finally:
        for session in sessions:
            await session.close()

    events = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
    warnings = [c.args[0] for c in mock_logger.warning.call_args_list if c.args]
    assert "knn_seed_skipped_source_edited" in events
    # Before the fix: the tag seeding hit the expired instance (MissingGreenlet),
    # its except handler raised again on ``memory.id``, and the run reported a
    # lost claim and re-queued the row.
    assert "embedding_claim_lost" not in events
    assert "tag_cooccurrence_seeding_failed" not in warnings
    requeue.assert_not_awaited()
    assert "tag_cooccurrence_edges_created" in events

    async with maker() as check:
        edges = (
            (await check.execute(select(NeuralMemoryEdge).where(NeuralMemoryEdge.src_id == row.id)))
            .scalars()
            .all()
        )
    # The k-NN seeds were dropped; the tag seeds were written.
    assert edges, "tag co-occurrence seeding did not run"
    assert all((e.edge_metadata or {}).get("source") == "tag_cooccurrence" for e in edges)
    assert {e.dst_id for e in edges} == {n.id for n in neighbours}
