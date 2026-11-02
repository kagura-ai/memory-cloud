"""The supersede catch-up pass against a real Postgres (#2256).

One context with an older memory A, a newer near-duplicate B and an unrelated
C, all embedded with no candidate. The vector store is stubbed (stored vectors
and the neighbour search); the detection, its conditional write and the row
selection are real. Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.auth import Context, User, Workspace
from models.memory import Memory, NeuralMemoryEdge
from neural.config import NeuralMemoryConfig
from services.supersede_verify import embedded_text_digest
from utils.datetime import to_utc_iso, utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def detection_env(monkeypatch: pytest.MonkeyPatch):
    """Suggestions on, judge off; seeding on, so ``seed=False`` is what keeps edges out."""
    monkeypatch.setenv("SUPERSEDE_SUGGEST_ENABLED", "true")
    monkeypatch.setenv("SUPERSEDE_VERIFY_MODE", "off")
    monkeypatch.setenv("KNN_SEED_ENABLED", "true")
    monkeypatch.setenv("KNN_SEED_MIN_SIMILARITY", "0.3")
    NeuralMemoryConfig.invalidate_cache()
    yield
    NeuralMemoryConfig.invalidate_cache()


@pytest_asyncio.fixture(loop_scope="session")
async def scope(db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:
    owner = f"catchup_{uuid4().hex[:8]}"
    db_session.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db_session.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"catchup-ws-{uuid4().hex[:8]}",
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
        name=f"catchup-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    start = utcnow() - timedelta(hours=1)

    def _memory(summary: str, minutes: int) -> Memory:
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
            created_at=start + timedelta(minutes=minutes),
            embedding_status="success",
            embedded_text_digest=embedded_text_digest(summary, summary),
        )

    a = _memory("depot opens at 9:00", 0)
    b = _memory("depot opens at 10:00 from November", 1)
    c = _memory("an unrelated note", 2)
    db_session.add_all([a, b, c])
    await db_session.commit()
    yield {"owner": owner, "ctx": ctx, "a": a, "b": b, "c": c}
    await db_session.rollback()
    await db_session.execute(delete(NeuralMemoryEdge).where(NeuralMemoryEdge.user_id == owner))
    await db_session.execute(delete(Memory).where(Memory.user_id == owner))
    await db_session.execute(delete(Context).where(Context.id == ctx.id))
    await db_session.execute(delete(Workspace).where(Workspace.id == ws.id))
    await db_session.execute(delete(User).where(User.user_id == owner))
    await db_session.commit()


def _vector_store(scope: dict[str, Any]) -> tuple[AsyncMock, AsyncMock]:
    a, b, c = scope["a"], scope["b"], scope["c"]
    marker = {str(a.id): 1.0, str(b.id): 2.0, str(c.id): 3.0}

    def hit(memory: Memory, score: float) -> dict[str, Any]:
        return {
            "id": str(memory.id),
            "score": score,
            "payload": {"created_at": to_utc_iso(memory.created_at)},
        }

    neighbours = {
        1.0: [hit(a, 1.0), hit(b, 0.92), hit(c, 0.40)],
        2.0: [hit(b, 1.0), hit(a, 0.92), hit(c, 0.41)],
        3.0: [hit(c, 1.0), hit(b, 0.41), hit(a, 0.40)],
    }

    async def retrieve(_collection: str, point_ids: list[str]) -> dict[str, list[float]]:
        return {pid: [marker[pid]] for pid in point_ids if pid in marker}

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["context_id"] == str(scope["ctx"].id)
        assert kwargs["workspace_id"] == str(scope["ctx"].workspace_id)
        return neighbours[kwargs["query_vector"][0]][: kwargs["limit"]]

    return AsyncMock(side_effect=retrieve), AsyncMock(side_effect=search)


async def _pass(async_engine: AsyncEngine, scope: dict[str, Any], **kwargs: Any) -> Any:
    from tasks import supersede_catch_up as task

    retrieve, search = _vector_store(scope)
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        with (
            patch("db.qdrant.retrieve_dense_vectors", retrieve),
            patch("db.qdrant.search_memories_qdrant", search),
            patch.object(task, "_read_counter", AsyncMock(return_value=0)),
        ):
            return await task.run_supersede_catch_up(session, scope["ctx"].id, **kwargs)


async def _candidates(async_engine: AsyncEngine, scope: dict[str, Any]) -> dict[str, Any]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        rows = (
            await session.execute(
                select(Memory.id, Memory.supersede_candidate).where(
                    Memory.user_id == scope["owner"]
                )
            )
        ).all()
    by_id = {str(r.id): r.supersede_candidate for r in rows}
    return {name: by_id[str(scope[name].id)] for name in ("a", "b", "c")}


async def test_the_pass_detects_on_the_newer_row_only(
    async_engine: AsyncEngine, scope: dict[str, Any], detection_env: None
) -> None:
    plan = await _pass(async_engine, scope, dry_run=True)
    assert (plan.eligible, plan.planned, plan.processed) == (3, 3, 0)
    assert await _candidates(async_engine, scope) == {"a": None, "b": None, "c": None}

    applied = await _pass(async_engine, scope, dry_run=False)

    assert (applied.processed, applied.detected, applied.unchanged) == (3, 1, 2)
    assert (applied.cleared, applied.no_vector, applied.skipped, applied.failed) == (0, 0, 0, 0)
    assert applied.judge_calls == 0
    candidates = await _candidates(async_engine, scope)
    # B (newer) names A; A never names the newer B (D3); C has no near neighbour.
    assert candidates["b"]["memory_id"] == str(scope["a"].id)
    assert candidates["b"]["similarity"] == 0.92
    assert "verification" not in candidates["b"]
    assert candidates["a"] is None
    assert candidates["c"] is None

    # seed=False: no edge was written although seeding is on.
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        edges = (
            await session.execute(
                select(NeuralMemoryEdge.id).where(NeuralMemoryEdge.user_id == scope["owner"])
            )
        ).all()
    assert edges == []

    # A second plan lists A and C again (nothing records "checked"), not B.
    again = await _pass(async_engine, scope, dry_run=True)
    assert (again.eligible, again.planned) == (2, 2)
    # B's candidate is unverified: --include-unverified lists it too.
    unverified = await _pass(async_engine, scope, dry_run=True, include_unverified=True)
    assert unverified.planned == 3
    # --before B's creation leaves only A.
    bounded = await _pass(
        async_engine,
        scope,
        dry_run=True,
        include_unverified=True,
        before=scope["b"].created_at,
    )
    assert bounded.planned == 1
    # The resume point walks on past the rows that stay without a candidate.
    assert applied.last_created_at == scope["c"].created_at
    resumed = await _pass(async_engine, scope, dry_run=True, after=applied.last_created_at)
    assert resumed.eligible == 0
    after_a = await _pass(async_engine, scope, dry_run=True, after=scope["a"].created_at)
    assert after_a.planned == 1  # C (B has a candidate)
    # --limit caps the rows checked, not the eligible count.
    limited = await _pass(async_engine, scope, dry_run=True, include_unverified=True, limit=1)
    assert (limited.eligible, limited.planned) == (3, 1)
