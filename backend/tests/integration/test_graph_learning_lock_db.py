"""One Hebbian learning pass per graph owner at a time (Issue #1985).

A pass strengthens edges and then prunes a node's weakest ones; two passes
over the same graph can lock edge rows in opposite orders across those two
phases. The live pass therefore takes a per-user transaction advisory lock
without waiting and skips itself when the owner is busy; batch tasks take the
same lock blocking.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from db.graph_learning_lock import lock_graph_learning, try_lock_graph_learning
from models.memory import GraphMemory
from models.schemas import RecallRequest
from services.memory_service import MemoryService

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_busy_owner_skips_and_frees_on_commit(async_engine: AsyncEngine) -> None:
    user_id = f"graph-lock-{uuid4().hex[:8]}"
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as holder, factory() as learner, factory() as other:
        await lock_graph_learning(holder, user_id)

        # The live pass sees the owner busy and writes nothing.
        await MemoryService(learner)._recall_learn_on_session(
            learner,
            RecallRequest(query="q", k=5),
            [],
            {},
            user_id=user_id,
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
        )
        graphs = await learner.scalar(
            select(func.count()).select_from(GraphMemory).where(GraphMemory.user_id == user_id)
        )
        assert graphs == 0
        await learner.rollback()

        # Another owner is never blocked.
        assert await try_lock_graph_learning(other, f"{user_id}-other") is True
        await other.rollback()

        await holder.commit()  # transaction lock released
        assert await try_lock_graph_learning(learner, user_id) is True
        # Held by this transaction now: a second session is refused.
        assert await try_lock_graph_learning(other, user_id) is False
        await other.rollback()
        await learner.rollback()
