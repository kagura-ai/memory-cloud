"""#2729: the startup pass adds every late-added payload index to existing collections.

v0.152.0 shipped the ``event_start`` / ``event_end`` indexes, but an existing
``kagura_memories*`` collection only received them from the retrofit branch of
``ensure_kagura_memories_collection`` — which runs on context creation and
embedding migration, never at startup. The startup pass that did run added only
``chunk_of``. Both now read one table, ``LATE_PAYLOAD_INDEXES``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.qdrant as qmod
from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    LATE_PAYLOAD_INDEXES,
    ensure_kagura_memories_collection,
    ensure_payload_indexes_on_all,
)
from utils.content_chunks import CHUNK_OF_FIELD

pytestmark = pytest.mark.asyncio

# The payload schema of a collection created by v0.151.0: every index of the
# creation path except the two event-date indexes added in v0.152.0.
_V0151_SCHEMA = {
    name: object()
    for name in (
        "workspace_id",
        "context_id",
        "user_id",
        "summary",
        "context_summary",
        "summary_tokens",
        "context_summary_tokens",
        "content_tokens",
        "scope",
        "type",
        "importance",
        "tags",
        "created_at",
        "updated_at",
        "observed_at",
        "location",
        CHUNK_OF_FIELD,
    )
}


@pytest.fixture
def mock_client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: None)
    return client


def _collections(*names: str):
    return SimpleNamespace(collections=[SimpleNamespace(name=n) for n in names])


def _created(client) -> dict[str, dict[str, str]]:
    """collection -> {field: schema} of every create_payload_index call."""
    out: dict[str, dict[str, str]] = {}
    for call in client.create_payload_index.await_args_list:
        kw = call.kwargs
        out.setdefault(kw["collection_name"], {})[kw["field_name"]] = kw["field_schema"]
    return out


async def test_startup_pass_adds_the_event_date_indexes_to_an_upgraded_collection(mock_client):
    """The v0.152.0 upgrade: a collection with the v0.151.0 schema and no
    create_context call afterwards still gets event_start / event_end."""
    upgraded = "kagura_memories_qwen3_embedding_4b_2560"
    complete = "kagura_memories"
    mock_client.get_collections.return_value = _collections(complete, upgraded, "other")
    schemas = {
        complete: {**_V0151_SCHEMA, "event_start": object(), "event_end": object()},
        upgraded: dict(_V0151_SCHEMA),
    }
    mock_client.get_collection.side_effect = lambda name: SimpleNamespace(
        payload_schema=schemas[name]
    )

    added = await ensure_payload_indexes_on_all()

    assert added == {upgraded: ["event_start", "event_end"]}
    assert _created(mock_client) == {
        upgraded: {"event_start": "datetime", "event_end": "datetime"},
    }


async def test_startup_pass_creates_the_same_indexes_as_the_ensure_retrofit(mock_client):
    """One table drives both paths: on a collection with none of the late
    indexes, the startup pass creates exactly what ensure_kagura_memories_collection
    retrofits on context creation."""
    mock_client.get_collections.return_value = _collections("kagura_memories")
    params = SimpleNamespace(sparse_vectors={KAGURA_MEMORIES_BM25_VECTOR_NAME: object()})
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(params=params),
        payload_schema={"scope": object()},
    )

    await ensure_kagura_memories_collection()
    by_retrofit = _created(mock_client)["kagura_memories"]
    mock_client.create_payload_index.reset_mock()

    added = await ensure_payload_indexes_on_all()
    by_startup = _created(mock_client)["kagura_memories"]

    assert by_startup == by_retrofit
    assert added == {"kagura_memories": [field for field, _schema in LATE_PAYLOAD_INDEXES]}
    assert set(by_startup) == {
        "tags",
        "location",
        "created_at",
        "updated_at",
        "observed_at",
        "event_start",
        "event_end",
        CHUNK_OF_FIELD,
    }


async def test_startup_pass_continues_past_one_failing_index(mock_client):
    """One index that Qdrant refuses must not block the ones after it — chunk_of
    is last in the table and every dense search / delete filters on it — and
    what was created is still reported."""
    mock_client.get_collections.return_value = _collections("kagura_memories")
    schema = {k: v for k, v in _V0151_SCHEMA.items() if k != CHUNK_OF_FIELD}
    mock_client.get_collection.return_value = SimpleNamespace(payload_schema=schema)

    async def _create(*, collection_name: str, field_name: str, field_schema: str) -> None:
        if field_name == "event_start":
            raise RuntimeError("refused")

    mock_client.create_payload_index.side_effect = _create

    added = await ensure_payload_indexes_on_all()

    assert added == {"kagura_memories": ["event_end", CHUNK_OF_FIELD]}
    attempted = [c.kwargs["field_name"] for c in mock_client.create_payload_index.await_args_list]
    assert attempted == ["event_start", "event_end", CHUNK_OF_FIELD]


async def test_ensure_retrofit_does_not_fail_context_creation_on_one_index(mock_client):
    """The retrofit on the request path (context creation) shares the helper:
    a refused late index is logged, the collection is still usable, and the
    next API start retries it. Before #2729 it surfaced as a collection
    creation failure although the collection existed."""
    mock_client.get_collections.return_value = _collections("kagura_memories")
    params = SimpleNamespace(sparse_vectors={KAGURA_MEMORIES_BM25_VECTOR_NAME: object()})
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(params=params),
        payload_schema={k: v for k, v in _V0151_SCHEMA.items() if k != CHUNK_OF_FIELD},
    )
    mock_client.create_payload_index.side_effect = RuntimeError("refused")

    await ensure_kagura_memories_collection()

    attempted = [c.kwargs["field_name"] for c in mock_client.create_payload_index.await_args_list]
    assert attempted == ["event_start", "event_end", CHUNK_OF_FIELD]
    mock_client.create_collection.assert_not_awaited()


async def test_startup_pass_leaves_a_complete_collection_alone(mock_client):
    mock_client.get_collections.return_value = _collections("kagura_memories")
    mock_client.get_collection.return_value = SimpleNamespace(
        payload_schema={**_V0151_SCHEMA, "event_start": object(), "event_end": object()}
    )

    assert await ensure_payload_indexes_on_all() == {}
    mock_client.create_payload_index.assert_not_awaited()


async def test_startup_pass_is_fail_soft_per_collection(mock_client):
    """One collection that fails is logged and skipped; the others still get
    their indexes, and a failed listing adds nothing and raises nothing."""
    mock_client.get_collections.return_value = _collections("kagura_memories", "kagura_memories_b")
    mock_client.get_collection.side_effect = [
        RuntimeError("down"),
        SimpleNamespace(payload_schema=dict(_V0151_SCHEMA)),
    ]

    assert await ensure_payload_indexes_on_all() == {
        "kagura_memories_b": ["event_start", "event_end"]
    }

    mock_client.get_collections.side_effect = RuntimeError("down")
    assert await ensure_payload_indexes_on_all() == {}


async def test_startup_pass_is_a_no_op_on_a_non_qdrant_store(mock_client, monkeypatch):
    monkeypatch.setattr(qmod, "_active_store", lambda: object())

    assert await ensure_payload_indexes_on_all() == {}
    mock_client.get_collections.assert_not_awaited()
