"""#2996: a resource-indexed row survives every automated pass in an unfenced context.

The protection used to come from the context's feed settings alone. Here the
context is ordinary (no feed, ``sleep_mode='full'``) and the resource row is
31 days old, never adopted and never read: Sleep consolidation, the dedup
merge and the legacy consolidation pass all leave it as it is. Since #3021 no
consolidation pass removes anything, so a plain row of the same shape is kept
too; the dedup merge, which still removes, has the control that shows it ran
(a plain duplicate beside it is merged).

The row is tried both as the indexer writes it now (persistent) and as it
was written before the f62 migration (working). The vector store is stubbed
as in test_consolidation_point_deletes_db. Not executed in the local unit run
— needs the DB container.
"""

# ruff: noqa: F811 — the fixtures are shared with test_session_rollback_db
from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from neural.config import NeuralMemoryConfig
from services.resource_indexer import resource_point_id
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.reporter import SleepBudget
from tests.integration.test_consolidation_point_deletes_db import (  # noqa: F401 — fixtures
    _legacy_patches,
    _run_sleep,
    chunks,
    client,
)
from tests.integration.test_session_rollback_db import (  # noqa: F401 — fixtures
    World,
    _stubs,
    world,
)
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _aged(world: World, **fields: Any) -> Memory:
    """31 days old, never adopted, never read."""
    old = utcnow() - timedelta(days=31)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": world.owner,
        "workspace_id": world.workspace_id,
        "context_id": world.context_id,
        "content": "body",
        "type": "note",
        "scope": "working",
        "importance": 0.0,
        "access_count": 0,
        "reference_count": 0,
        "client": "test",
        "embedding_status": "success",
        "created_at": old,
    }
    values.update(fields)
    return Memory(**values)


async def _seed(world: World, scope: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A resource row and a plain row with the same summary; their ids."""
    resource = _aged(
        world,
        summary="[catalog] item-1 v1",
        type="resource_data",
        source="resource_ingest",
        client="resource_indexer",
        details={"resource_id": "catalog", "doc_id": "item-1", "version": 1},
        summary_embedding_id=resource_point_id("catalog", "item-1", 1),
        scope=scope,
        promoted_at=utcnow() if scope == "persistent" else None,
    )
    plain = _aged(world, summary="[catalog] item-1 v1")
    world.session.add_all([resource, plain])
    await world.session.commit()
    return resource.id, plain.id


async def _state(world: World, memory_id: uuid.UUID) -> tuple[bool, str | None] | None:
    """(live, deleted_by), or None when the row is gone."""
    row = (
        await world.session.execute(
            select(Memory.deleted_at, Memory.deleted_by)
            .where(Memory.id == memory_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    return None if row is None else (row.deleted_at is None, row.deleted_by)


@pytest.mark.parametrize("scope", ["persistent", "working"])
async def test_sleep_consolidation_keeps_the_resource_row(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock, scope: str
) -> None:
    resource, plain = await _seed(world, scope)

    await _run_sleep(world)

    assert await _state(world, resource) == (True, None)
    assert await _state(world, plain) == (True, None)
    _stubs["points"].assert_not_awaited()


@pytest.mark.parametrize("scope", ["persistent", "working"])
async def test_the_dedup_merge_keeps_the_resource_row(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock, scope: str
) -> None:
    """The vector search is replaced by one that calls every fetched pair a
    0.99 duplicate (rule path, no judge): the resource row is no candidate, and
    the two plain duplicates beside it are merged."""
    resource, plain = await _seed(world, scope)
    twin = _aged(world, summary="[catalog] item-1 v1")
    world.session.add(twin)
    await world.session.commit()
    fetched: list[uuid.UUID] = []

    async def every_pair(
        self: DedupMergePhase, memories: list[Memory], *_: Any, **__: Any
    ) -> list[tuple[uuid.UUID, uuid.UUID, float]]:
        fetched.extend(m.id for m in memories)
        ids = sorted((m.id for m in memories), key=str)
        return [(a, b, 0.99) for i, a in enumerate(ids) for b in ids[i + 1 :]]

    with ExitStack() as stack:
        stack.enter_context(patch("services.sleep.dedup_merge.NeuralEdgeRepository"))
        stack.enter_context(patch("services.sleep.dedup_merge.EmbeddingService"))
        stack.enter_context(patch.object(DedupMergePhase, "_find_similar_pairs", every_pair))
        phase = DedupMergePhase(world.session, AsyncMock())
        phase.edge_repo = AsyncMock()
        phase.edge_repo.transfer_edges = AsyncMock(return_value=0)
        phase.edge_repo.get_edge = AsyncMock(return_value=None)
        await phase.execute(
            NeuralMemoryConfig(sleep_llm_provider=""),
            world.owner,
            str(world.workspace_id),
            str(world.context_id),
            SleepBudget(),
        )
        await world.session.commit()

    assert resource not in fetched
    assert await _state(world, resource) == (True, None)
    states = {await _state(world, plain), await _state(world, twin.id)}
    assert states == {(True, None), (False, DELETED_BY_SLEEP_MERGE)}


@pytest.mark.parametrize("scope", ["persistent", "working"])
async def test_the_legacy_pass_keeps_the_resource_row(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock, scope: str
) -> None:
    from tasks.neural_tasks import consolidation_task

    resource, plain = await _seed(world, scope)

    with ExitStack() as stack:
        _legacy_patches(world, stack)
        result = await consolidation_task()

    assert await _state(world, resource) == (True, None)
    assert await _state(world, plain) == (True, None)
    assert result is not None
    assert result["deleted"] == 0
    # Counted only when the pass reached it (a working row).
    assert result["resource_skipped"] == (1 if scope == "working" else 0)


async def test_the_legacy_pass_keeps_a_row_whose_key_was_edited(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, client: MagicMock
) -> None:
    """The legacy pass decides by ``resource_owned_predicate`` like Sleep does:
    an indexer row whose ``details.version`` was edited still names its old
    point (``owns_resource_point`` is false for it) and is still kept."""
    from tasks.neural_tasks import consolidation_task

    edited = _aged(
        world,
        summary="[catalog] item-2 v1",
        type="resource_data",
        source="resource_ingest",
        details={"resource_id": "catalog", "doc_id": "item-2", "version": 2},
        summary_embedding_id=resource_point_id("catalog", "item-2", 1),
    )
    world.session.add(edited)
    await world.session.commit()

    with ExitStack() as stack:
        _legacy_patches(world, stack)
        result = await consolidation_task()

    assert await _state(world, edited.id) == (True, None)
    assert result is not None and result["resource_skipped"] == 1
