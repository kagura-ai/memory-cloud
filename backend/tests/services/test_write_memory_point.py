"""#2065: ``write_memory_point`` — the one place that picks how a memory's point is written.

The embedding pipeline, the Sleep reindex and the Sleep restores write
through it. A row the resource indexer wrote gets the indexer's point back
(``ResourceIndexer.rebuild_point``); every other row its summary point.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.memory_service import build_memory_point, write_memory_point
from services.resource_indexer import resource_point_id


def _memory(**overrides) -> MagicMock:
    memory = MagicMock()
    memory.id = uuid4()
    memory.user_id = "u"
    memory.summary = "a summary"
    memory.context_summary = "why"
    memory.content = "content body"
    memory.type = "note"
    memory.importance = 0.6
    memory.scope = "persistent"
    memory.tags = ["t"]
    memory.client = "pytest"
    memory.context = None
    memory.created_at = datetime(2026, 1, 1)
    memory.updated_at = datetime(2026, 1, 2)
    memory.location_lat = None
    memory.location_lon = None
    memory.resource_id = None
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


def _resource_memory() -> MagicMock:
    point_id = resource_point_id("reports", "doc-1", 2)
    return _memory(
        summary="[resource] doc-1 v2",
        resource_id="reports",
        resource_doc_id="doc-1",
        resource_version=2,
        summary_embedding_id=point_id,
        point_id=point_id,
    )


_SCOPE = {"user_id": "u", "workspace_id": "w", "context_id": "c"}


@pytest.mark.asyncio
async def test_a_resource_row_gets_the_indexer_point_back() -> None:
    memory = _resource_memory()
    svc = MagicMock()
    svc.embed = AsyncMock()
    indexer = MagicMock()
    order: list[str] = []
    indexer.rebuild_point = AsyncMock(side_effect=lambda *a, **k: order.append("rebuild"))
    usage: dict[str, int] = {}
    with patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert:
        written = await write_memory_point(
            MagicMock(),
            memory,
            collection_name="coll",
            embedding_service=svc,
            usage=usage,
            on_write=lambda: order.append("on_write"),
            resource_indexer=indexer,
            **_SCOPE,
        )

    assert written is None
    indexer.rebuild_point.assert_awaited_once_with(
        memory, collection_name="coll", embedding_service=svc, usage=usage
    )
    assert order == ["on_write", "rebuild"]
    upsert.assert_not_awaited()
    svc.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_resource_row_builds_an_indexer_when_none_is_given() -> None:
    db = MagicMock()
    indexer = MagicMock()
    indexer.rebuild_point = AsyncMock()
    with patch("services.resource_indexer.ResourceIndexer", return_value=indexer) as cls:
        await write_memory_point(
            db, _resource_memory(), collection_name="coll", embedding_service=MagicMock(), **_SCOPE
        )
    cls.assert_called_once_with(db)
    indexer.rebuild_point.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_plain_row_gets_its_summary_point() -> None:
    memory = _memory()
    svc = MagicMock()
    order: list[str] = []

    async def _embed(*_args, **_kwargs):
        order.append("embed")
        return [0.1, 0.2]

    svc.embed = AsyncMock(side_effect=_embed)
    with patch(
        "services.memory_service.add_memory_to_qdrant",
        new=AsyncMock(side_effect=lambda **_: order.append("upsert")),
    ) as upsert:
        written = await write_memory_point(
            MagicMock(),
            memory,
            collection_name="coll",
            embedding_service=svc,
            on_write=lambda: order.append("on_write"),
            **_SCOPE,
        )

    assert order == ["embed", "on_write", "upsert"]
    svc.embed.assert_awaited_once_with("a summary", "u", context_id="c", workspace_id="w")
    payload, sparse_indices, sparse_values = build_memory_point(memory)
    kwargs = upsert.await_args.kwargs
    assert kwargs["memory_id"] == memory.id
    assert kwargs["collection_name"] == "coll"
    assert (kwargs["workspace_id"], kwargs["context_id"], kwargs["user_id"]) == ("w", "c", "u")
    assert kwargs["sparse_indices"] == sparse_indices
    assert kwargs["sparse_values"] == sparse_values
    assert written == ([0.1, 0.2], kwargs["payload"])
    assert set(kwargs["payload"]) == set(payload)


@pytest.mark.asyncio
async def test_usage_is_counted_also_when_the_upsert_fails() -> None:
    svc = MagicMock()
    svc.embed_with_usage = AsyncMock(return_value=([0.1], 17))
    usage: dict[str, int] = {}
    with (
        patch(
            "services.memory_service.add_memory_to_qdrant",
            new=AsyncMock(side_effect=RuntimeError("qdrant down")),
        ),
        pytest.raises(RuntimeError),
    ):
        await write_memory_point(
            MagicMock(),
            _memory(),
            collection_name="coll",
            embedding_service=svc,
            usage=usage,
            **_SCOPE,
        )
    assert usage == {"tokens": 17}


@pytest.mark.asyncio
async def test_a_failed_embedding_writes_nothing() -> None:
    svc = MagicMock()
    svc.embed = AsyncMock(side_effect=RuntimeError("provider down"))
    on_write = MagicMock()
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        pytest.raises(RuntimeError),
    ):
        await write_memory_point(
            MagicMock(),
            _memory(),
            collection_name="coll",
            embedding_service=svc,
            on_write=on_write,
            **_SCOPE,
        )
    on_write.assert_not_called()
    upsert.assert_not_awaited()
