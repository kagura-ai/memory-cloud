"""Integration tests: content chunk points against a live Qdrant (#2015).

A memory on a context with ``content_chunks_enabled`` has its summary point
plus chunk points (``chunk_of`` = the memory id). These tests pin, against a
real Qdrant, that:

- a default dense search returns summary points only; ``include_chunks=True``
  also returns chunk points, which collapse back to the memory;
- deleting the memory removes its chunk points;
- a payload update (tags) reaches the chunk points, so a tag-filtered search
  still matches through a chunk;
- the orphan sweep keeps the chunks of a live memory and removes those of a
  memory whose row is gone.

Skipped when ``QDRANT_URL`` is unreachable (and by ``db_session`` when Postgres
is); CI's integration job starts a Qdrant. The tests use a throwaway
``kagura_memories_it_*`` collection. Run with::

    make test-integration
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from db import qdrant as qdrant_module
from db.qdrant import (
    add_memory_to_qdrant,
    delete_memory_from_qdrant,
    ensure_kagura_memories_collection,
    search_memories_qdrant,
    update_memory_payload_in_qdrant,
    upsert_chunk_points,
)
from models.memory import Memory
from services.orphan_vector_sweep import sweep_orphan_points
from utils.content_chunks import (
    CHUNK_OF_FIELD,
    build_chunk_payload,
    chunk_point_id,
    collapse_chunk_hits,
)

_DIM = 8
_USER = "chunk_owner"
_WS = str(uuid4())
_CTX = str(uuid4())

# Orthogonal-ish unit vectors: the query matches the chunk, not the summary.
_SUMMARY_VEC = [1.0, 0, 0, 0, 0, 0, 0, 0]
_CHUNK_VEC = [0, 1.0, 0, 0, 0, 0, 0, 0]
_QUERY_VEC = [0, 1.0, 0, 0, 0, 0, 0, 0]


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


@pytest_asyncio.fixture(loop_scope="session")
async def qdrant(monkeypatch) -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(url=_qdrant_url(), timeout=5)
    try:
        await client.get_collections()
    except Exception as exc:  # noqa: BLE001 — intentional broad skip guard
        await client.close()
        pytest.skip(f"Qdrant unreachable at {_qdrant_url()}: {exc}")
    monkeypatch.setattr(qdrant_module, "_qdrant_client", client)
    monkeypatch.setattr(qdrant_module, "_active_store", lambda: None)
    yield client
    await client.close()


@pytest_asyncio.fixture(loop_scope="session")
async def collection(qdrant: AsyncQdrantClient) -> AsyncIterator[str]:
    name = f"kagura_memories_it_{uuid4().hex[:12]}"
    await ensure_kagura_memories_collection(_DIM, name)
    yield name
    await qdrant.delete_collection(name)


async def _write(
    collection: str, memory_id, *, chunks: int = 2, tags=None, user=_USER, ctx=_CTX
) -> None:
    payload = {
        "summary": "summary",
        "context_summary": "",
        "type": "note",
        "importance": 0.5,
        "tags": tags or [],
        "scope": "context",
        "client": "pytest",
        "created_at": "2026-10-17T00:00:00+00:00",
        "updated_at": "2026-10-17T00:00:00+00:00",
    }
    await add_memory_to_qdrant(
        user_id=user,
        memory_id=memory_id,
        vector=_SUMMARY_VEC,
        payload=dict(payload),
        workspace_id=_WS,
        context_id=ctx,
        collection_name=collection,
    )
    await upsert_chunk_points(
        [
            (
                chunk_point_id(memory_id, i),
                _CHUNK_VEC,
                build_chunk_payload(
                    payload, memory_id=memory_id, chunk_index=i, chunk_text=f"t{i}"
                ),
            )
            for i in range(chunks)
        ],
        user_id=user,
        workspace_id=_WS,
        context_id=ctx,
        collection_name=collection,
    )


async def _count(qdrant: AsyncQdrantClient, collection: str, memory_id) -> tuple[int, int]:
    """(summary points, chunk points) of a memory."""
    summary = await qdrant.retrieve(collection, ids=[str(memory_id)])
    chunks = await qdrant.count(
        collection,
        count_filter=Filter(
            must=[FieldCondition(key=CHUNK_OF_FIELD, match=MatchValue(value=str(memory_id)))]
        ),
        exact=True,
    )
    return len(summary), chunks.count


async def _search(collection: str, **kwargs):
    return await search_memories_qdrant(
        user_id=_USER,
        query_vector=_QUERY_VEC,
        workspace_id=_WS,
        context_id=_CTX,
        limit=10,
        collection_name=collection,
        **kwargs,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_default_search_excludes_chunks_and_opt_in_collapses(qdrant, collection):
    memory_id = uuid4()
    await _write(collection, memory_id)

    plain = await _search(collection)
    assert [str(h["id"]) for h in plain] == [str(memory_id)]

    with_chunks = await _search(collection, include_chunks=True)
    assert len(with_chunks) == 3
    collapsed = collapse_chunk_hits(with_chunks)
    assert [str(h["id"]) for h in collapsed] == [str(memory_id)]
    assert collapsed[0]["matched_chunk_text"] in {"t0", "t1"}
    assert collapsed[0]["score"] > plain[0]["score"]


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_memory_removes_its_chunks(qdrant, collection):
    memory_id = uuid4()
    await _write(collection, memory_id)
    assert await _count(qdrant, collection, memory_id) == (1, 2)

    await delete_memory_from_qdrant(_USER, memory_id, collection)

    assert await _count(qdrant, collection, memory_id) == (0, 0)


@pytest.mark.asyncio(loop_scope="session")
async def test_tag_update_reaches_the_chunks(qdrant, collection):
    memory_id = uuid4()
    await _write(collection, memory_id, tags=["old"])

    await update_memory_payload_in_qdrant(memory_id, {"tags": ["new"]}, collection)

    hits = await _search(collection, include_chunks=True, filters={"tags": ["new"]})
    ids = {str(h["payload"].get(CHUNK_OF_FIELD) or h["id"]) for h in hits}
    assert ids == {str(memory_id)}
    assert len(hits) == 3
    stale = await _search(collection, include_chunks=True, filters={"tags": ["old"]})
    assert [h for h in stale if str(h["payload"].get(CHUNK_OF_FIELD)) == str(memory_id)] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_orphan_sweep_keeps_live_chunks_and_removes_rowless_ones(
    qdrant, collection, db_session
):
    live_id, gone_id = uuid4(), uuid4()
    db_session.add(
        Memory(
            id=live_id,
            user_id=_USER,
            workspace_id=uuid4(),
            summary="live summary",
            content="c",
            type="note",
            client="pytest",
            embedding_status="success",
        )
    )
    await db_session.commit()
    await _write(collection, live_id)
    await _write(collection, gone_id)

    result = await sweep_orphan_points(db_session, collections=[collection], dry_run=False)

    assert await _count(qdrant, collection, live_id) == (1, 2)
    assert await _count(qdrant, collection, gone_id) == (0, 0)
    assert result.orphans >= 3


class _Embedder:
    async def embed_batch(self, texts, *args, **kwargs):
        return [_CHUNK_VEC for _ in texts]


def _row(memory_id, content: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=memory_id, user_id=_USER, workspace_id=_WS, context_id=_CTX, content=content
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_content_that_shrinks_loses_its_chunks(qdrant, collection):
    """#2015: removed body text must not survive in chunk_text."""
    from services.content_chunk_writer import write_content_chunks

    memory_id = uuid4()
    await _write(collection, memory_id, chunks=3)
    assert await _count(qdrant, collection, memory_id) == (1, 3)

    n = await write_content_chunks(
        None,  # type: ignore[arg-type] — the flag is passed, no lookup
        _row(memory_id, "now short"),
        base_payload={"summary": "summary"},
        embed_svc=_Embedder(),
        collection_name=collection,
        chunks_enabled=True,
    )

    assert n == 0
    assert await _count(qdrant, collection, memory_id) == (1, 0)


@pytest.mark.asyncio(loop_scope="session")
async def test_turning_the_flag_off_purges_and_later_edits_write_none(qdrant, collection):
    from db.qdrant import purge_context_chunk_points
    from services.content_chunk_writer import write_content_chunks

    memory_id = uuid4()
    await _write(collection, memory_id, chunks=2)

    await purge_context_chunk_points(_WS, _CTX, collection)
    assert await _count(qdrant, collection, memory_id) == (1, 0)

    # An edit while the flag is off writes no chunks.
    n = await write_content_chunks(
        None,  # type: ignore[arg-type]
        _row(memory_id, "x" * 6000),
        base_payload={"summary": "summary"},
        embed_svc=_Embedder(),
        collection_name=collection,
        chunks_enabled=False,
    )
    assert n == 0
    assert await _count(qdrant, collection, memory_id) == (1, 0)


@pytest.mark.asyncio(loop_scope="session")
async def test_flag_turned_off_mid_write_leaves_no_chunks(qdrant, collection, monkeypatch):
    """#2015 race, in order: the writer reads the flag on → the flag is turned
    off and the context purged while the chunks are embedded → the writer's
    fresh re-read stops the upsert: 0 chunks."""
    from db.qdrant import purge_context_chunk_points
    from services import content_chunk_writer
    from services.content_chunk_writer import write_content_chunks

    memory_id = uuid4()
    await _write(collection, memory_id, chunks=1)
    flag = {"on": True}

    async def _flag(db, context_id):
        return flag["on"]

    class _PurgingEmbedder:
        async def embed_batch(self, texts, *args, **kwargs):
            flag["on"] = False
            await purge_context_chunk_points(_WS, _CTX, collection)
            return [_CHUNK_VEC for _ in texts]

    monkeypatch.setattr(content_chunk_writer, "content_chunks_enabled", _flag)
    n = await write_content_chunks(
        None,  # type: ignore[arg-type]
        _row(memory_id, "x" * 6000),
        base_payload={"summary": "summary"},
        embed_svc=_PurgingEmbedder(),
        collection_name=collection,
    )

    assert n == 0
    assert await _count(qdrant, collection, memory_id) == (1, 0)


@pytest.mark.asyncio(loop_scope="session")
async def test_chunks_never_cross_user_or_context_isolation(qdrant, collection):
    """#2015: include_chunks=True returns only the caller's chunk points — not
    another user's in the same private context, not another context's, and
    not those of another user's private context."""
    own, other_user, other_ctx, other_private = uuid4(), uuid4(), uuid4(), uuid4()
    second_ctx, private_ctx = str(uuid4()), str(uuid4())
    await _write(collection, own)
    await _write(collection, other_user, user="someone_else")
    await _write(collection, other_ctx, ctx=second_ctx)
    await _write(collection, other_private, user="someone_else", ctx=private_ctx)

    hits = await _search(collection, include_chunks=True)  # private: user filter on

    owners = {str(h["payload"].get(CHUNK_OF_FIELD) or h["id"]) for h in hits}
    assert owners == {str(own)}
    assert len(hits) == 3  # its summary point + 2 chunks
    assert all(h["payload"]["user_id"] == _USER for h in hits)
    assert all(h["payload"]["context_id"] == _CTX for h in hits)
