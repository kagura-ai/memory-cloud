"""With recall-time association learning off, the periodic tasks still reach the user (#3023).

The hourly ``weight_decay_task`` and the legacy ``consolidation_task`` visit
only users that have a ``graph_memory`` row. The recall learning pass used to be
the place that made that row, so turning the pass off must not leave a user
who recalls without one: their tag co-occurrence seeds (origin ``hebbian``)
would then never decay or prune.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.memory import GraphMemory
from services.memory_service import MemoryService

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _graph_rows(factory, user_id: str) -> int:
    async with factory() as s:
        return int(
            await s.scalar(
                select(func.count()).select_from(GraphMemory).where(GraphMemory.user_id == user_id)
            )
            or 0
        )


async def test_switch_off_recall_row_is_visited_by_weight_decay(
    async_engine: AsyncEngine, monkeypatch
) -> None:
    from config.settings import get_settings
    from tasks import neural_tasks

    monkeypatch.setattr(get_settings(), "recall_association_learning_enabled", False)
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
    monkeypatch.setenv("ENABLE_DECAY", "true")
    user_id = f"graph-row-{uuid4().hex[:8]}"
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as request_db:
            service = MemoryService(request_db)
            await service._recall_ensure_graph_row(user_id)
            # A later recall finds the row and adds none.
            await service._recall_ensure_graph_row(user_id)
        assert await _graph_rows(factory, user_id) == 1

        async def _get_db():
            async with factory() as s:
                yield s

        visited: list[str] = []

        class _Decay:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def apply_decay(self, uid, **_kwargs):
                visited.append(uid)
                return {"edges_decayed": 0}

        with (
            patch.object(neural_tasks, "get_db", _get_db),
            patch.object(neural_tasks, "DecayManager", _Decay),
            patch.object(neural_tasks, "GraphService", MagicMock()),
        ):
            await neural_tasks.weight_decay_task()

        assert user_id in visited
    finally:
        async with factory() as s:
            await s.execute(delete(GraphMemory).where(GraphMemory.user_id == user_id))
            await s.commit()
