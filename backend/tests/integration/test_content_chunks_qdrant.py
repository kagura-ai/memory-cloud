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


# --------------------------------------------------------------------------
# #2015 PR 2a: chunks across collections and contexts
# --------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def second_collection(qdrant: AsyncQdrantClient) -> AsyncIterator[str]:
    """A migration's target collection, next to ``collection`` (its source)."""
    name = f"kagura_memories_it_{uuid4().hex[:12]}"
    await ensure_kagura_memories_collection(_DIM, name)
    yield name
    await qdrant.delete_collection(name)


@pytest.mark.asyncio(loop_scope="session")
async def test_cross_context_recall_returns_chunks_of_opted_in_contexts_only(qdrant, collection):
    """Primary on + secondary off with a leftover chunk → the leftover is not
    returned; primary off + secondary on → the secondary's chunk is."""
    on_ctx, off_ctx = str(uuid4()), str(uuid4())
    on_memory, off_memory = uuid4(), uuid4()
    await _write(collection, on_memory, ctx=on_ctx)
    await _write(collection, off_memory, ctx=off_ctx)  # its chunks are a leftover

    async def _cross(contexts, chunk_contexts):
        return await search_memories_qdrant(
            user_id=_USER,
            query_vector=_QUERY_VEC,
            workspace_id=_WS,
            context_id=contexts,
            limit=20,
            collection_name=collection,
            include_chunks=chunk_contexts,
        )

    for contexts in ([on_ctx, off_ctx], [off_ctx, on_ctx]):
        hits = await _cross(contexts, [on_ctx])
        chunk_owners = {
            h["payload"][CHUNK_OF_FIELD] for h in hits if CHUNK_OF_FIELD in h["payload"]
        }
        assert chunk_owners == {str(on_memory)}
        summary_ids = {str(h["id"]) for h in hits if CHUNK_OF_FIELD not in h["payload"]}
        assert summary_ids == {str(on_memory), str(off_memory)}
        assert [str(h["id"]) for h in collapse_chunk_hits(hits)][0] == str(on_memory)

    # No context opted in: summary points only.
    hits = await _cross([on_ctx, off_ctx], [])
    assert all(CHUNK_OF_FIELD not in h["payload"] for h in hits)


@pytest.mark.asyncio(loop_scope="session")
async def test_migration_rebuild_writes_chunks_into_the_target_and_flag_off_purges_both(
    qdrant, collection, second_collection, monkeypatch
):
    """The re-embed rebuilds chunk points in the target (a chunk-only fact is
    found there); turning the flag off purges them from source and target."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from db.qdrant import count_context_chunk_points, purge_context_chunk_points_everywhere
    from services import content_chunk_writer
    from services.content_chunk_writer import rebuild_content_chunks

    ctx = str(uuid4())
    memory_id = uuid4()
    await _write(collection, memory_id, ctx=ctx, chunks=3)  # the source's copy
    # The target's summary point, as the re-embed writes it before the chunks.
    await add_memory_to_qdrant(
        user_id=_USER,
        memory_id=memory_id,
        vector=_SUMMARY_VEC,
        payload={"summary": "summary", "type": "note"},
        workspace_id=_WS,
        context_id=ctx,
        collection_name=second_collection,
    )
    monkeypatch.setattr(
        content_chunk_writer, "content_chunks_enabled", AsyncMock(return_value=True)
    )
    db = MagicMock()
    db.commit = AsyncMock()
    row = SimpleNamespace(
        id=memory_id,
        user_id=_USER,
        workspace_id=_WS,
        context_id=ctx,
        content="x" * 2500,
        resource_id=None,
    )

    written = await rebuild_content_chunks(
        db,
        [row],  # type: ignore[list-item]
        base_payloads={memory_id: {"summary": "summary", "type": "note"}},
        embed_svc=_Embedder(),
        collection_name=second_collection,
        context_id=ctx,
    )

    assert written == 3
    assert await count_context_chunk_points(_WS, ctx, second_collection) == {str(memory_id): 3}
    hits = await search_memories_qdrant(
        user_id=_USER,
        query_vector=_QUERY_VEC,
        workspace_id=_WS,
        context_id=ctx,
        limit=10,
        collection_name=second_collection,
        include_chunks=True,
    )
    collapsed = collapse_chunk_hits(hits)
    assert [str(h["id"]) for h in collapsed] == [str(memory_id)]
    assert collapsed[0]["matched_chunk_text"]

    await purge_context_chunk_points_everywhere(
        _WS, ctx, collections=[collection, second_collection]
    )
    for name in (collection, second_collection):
        assert await count_context_chunk_points(_WS, ctx, name) == {}
        # summary points stay
        assert len(await qdrant.retrieve(name, ids=[str(memory_id)])) == 1


class _MigrationEmbedder:
    """Target-model embedder: summaries and chunks get different vectors, so a
    hit on ``_QUERY_VEC`` can only come from a chunk point."""

    async def embed_batch(self, texts, *args, **kwargs):
        return [_SUMMARY_VEC if t.startswith("summary") else _CHUNK_VEC for t in texts]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_opted_in_context_migrates_end_to_end_with_its_chunks(
    qdrant, collection, second_collection, db_session
):
    """#2015 PR 2a, A1 + A2: plan (no refusal) → reembed (chunks rebuilt in
    the target) → verify (a forgotten memory's chunks go, nothing missing) →
    switch (chunks of a memory forgotten since go) → a chunk-only fact is
    found in the target → flag off purges source and target."""
    from dataclasses import replace
    from unittest.mock import patch

    from sqlalchemy import delete, update

    from config.settings import get_settings
    from db.qdrant import count_context_chunk_points
    from models.auth import Context, Workspace
    from models.config import ContextSearchConfig
    from models.schemas import ContextSearchConfigUpdate
    from repositories.config_repository import (
        ContextSearchConfigRepository,
        search_config_defaults,
    )
    from services import embedding_migration_service as svc
    from utils.datetime import utcnow

    workspace = Workspace(id=uuid4(), name="chunks-migration", owner_user_id=_USER)
    db_session.add(workspace)
    await db_session.flush()
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"chunks_migration_{uuid4().hex[:8]}",
        display_name="Chunks migration",
        created_by=_USER,
        is_private=False,
    )
    db_session.add(context)
    await db_session.flush()
    db_session.add(
        ContextSearchConfig(
            context_id=context.id,
            embedding_model="text-embedding-3-small",
            embedding_dimensions=512,
            content_chunks_enabled=True,
            **search_config_defaults(get_settings()),
        )
    )
    kept, forgotten_before_verify, forgotten_before_switch, short = (uuid4() for _ in range(4))
    for memory_id, content in (
        (kept, "the deploy key rotates on the first Monday " * 50),
        (forgotten_before_verify, "y" * 2000),
        (forgotten_before_switch, "z" * 2000),
        (short, "short"),
    ):
        db_session.add(
            Memory(
                id=memory_id,
                user_id=_USER,
                workspace_id=workspace.id,
                context_id=context.id,
                summary="summary of a long note",
                content=content,
                type="note",
                client="pytest",
                embedding_status="success",
                summary_embedding_id=memory_id,
            )
        )
    await db_session.commit()
    ws, ctx = str(workspace.id), str(context.id)

    try:
        plan = await svc.plan_context_migration(db_session, context.id, "qwen3-embedding:4b")
        assert plan.content_chunks_on
        assert plan.chunk_estimate == 2 + 2 + 2  # 2,150 / 2,000 / 2,000 chars
        plan = replace(
            plan,
            source_collection=collection,
            target_collection=second_collection,
            target_dimensions=_DIM,
        )

        result = await svc.reembed_context(db_session, plan, embedding_service=_MigrationEmbedder())
        assert result.embedded == 4 and result.chunks_written == 6
        assert await count_context_chunk_points(ws, ctx, second_collection) == {
            str(kept): 2,
            str(forgotten_before_verify): 2,
            str(forgotten_before_switch): 2,
        }

        await db_session.execute(
            update(Memory).where(Memory.id == forgotten_before_verify).values(deleted_at=utcnow())
        )
        await db_session.commit()
        verified = await svc.verify_context_migration(db_session, plan)
        assert verified.ok and verified.stale_removed == 1
        assert verified.chunks_stale_removed == 1
        assert verified.chunks_missing == []
        assert (verified.chunks_expected, verified.chunks_present) == (4, 4)

        await db_session.execute(
            update(Memory).where(Memory.id == forgotten_before_switch).values(deleted_at=utcnow())
        )
        await db_session.commit()
        with patch.object(svc, "get_collection_name", return_value=second_collection):
            await svc.switch_context_embedding(
                db_session, context.id, "qwen3-embedding:4b", 2560, requeue_since=result.started_at
            )
        assert await count_context_chunk_points(ws, ctx, second_collection) == {str(kept): 2}

        hits = await search_memories_qdrant(
            user_id=_USER,
            query_vector=_QUERY_VEC,
            workspace_id=ws,
            context_id=ctx,
            limit=10,
            collection_name=second_collection,
            is_shared_context=True,
            include_chunks=True,
        )
        collapsed = collapse_chunk_hits(hits)
        assert str(collapsed[0]["id"]) == str(kept)
        assert "deploy key" in collapsed[0]["matched_chunk_text"]

        # The source still holds the summary points only (it never had
        # chunks here); flag off must leave no chunk point in either.
        repo = ContextSearchConfigRepository(db_session)
        await repo.update(
            context.id, ContextSearchConfigUpdate(**_CONFIG_BASE, content_chunks_enabled=False)
        )
        assert await count_context_chunk_points(ws, ctx, second_collection) == {}
        assert await count_context_chunk_points(ws, ctx, collection) == {}
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Memory).where(Memory.context_id == context.id))
        await db_session.execute(
            delete(ContextSearchConfig).where(ContextSearchConfig.context_id == context.id)
        )
        await db_session.execute(delete(Context).where(Context.id == context.id))
        await db_session.execute(delete(Workspace).where(Workspace.id == workspace.id))
        await db_session.commit()


_CONFIG_BASE = {
    "semantic_weight": 0.6,
    "bm25_weight": 0.4,
    "fetch_factor": 3,
    "use_rerank": False,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2",
}
