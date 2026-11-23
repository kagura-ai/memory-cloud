"""Integration: the keyword weighting against Postgres and Qdrant (#2984).

Memories are written by the real point writer (``write_memory_point``), which
stores their keyword lengths and weights each point with the context's cached
averages. The rebuild (``run_keyword_rebuild``) then brings every point to the
context's current averages, and a memory written again after it, with the same
averages, gets exactly the vector the rebuild wrote. A second rebuild writes
nothing. The dense vectors and the other payload keys stay as they were.

#2986: a point indexed with the dictionary-form tokens of spelling variants
(引越し, 届出, ...) gets the row's normalized tokens from the rebuild, the same
tokens and vector a write after the upgrade gives it.

Skipped when ``QDRANT_URL`` is unreachable (and by ``db_session`` when Postgres
is). Run with::

    make test-integration
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointVectors, SparseVector
from sqlalchemy import select, update

from db import qdrant as qdrant_module
from db.qdrant import ensure_kagura_memories_collection, get_collection_name
from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory
from services import keyword_stats
from services.memory_service import memory_keyword_tokens, write_memory_point
from tasks.keyword_rebuild import run_keyword_rebuild

_DIM = 8
_USER = "keyword_owner"

_TEXTS = [
    # #2986: spelling variants; rows[0] is the one written again after the rebuild.
    ("引越しの届出", "打合せの議事録", "附属のサーバの設定を変更する。"),
    ("認証エラーの対処", "トークンの期限切れ", "期限切れのトークンで認証エラーが出たら更新する。"),
    (
        "データベース移行の手順",
        "移行前に必ず読む",
        "バックアップを取り、移行を実行し、検証する。" * 12,
    ),
    ("deploy checklist", "", "run the migration first, then deploy, then the rebuild."),
]


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
    yield client
    await client.close()


@pytest_asyncio.fixture(loop_scope="session")
async def routed(db_session, qdrant: AsyncQdrantClient) -> AsyncIterator[tuple[Context, str]]:
    model = f"it-keyword-{uuid4().hex[:10]}"
    collection = get_collection_name(model, _DIM)
    await ensure_kagura_memories_collection(_DIM, collection)
    workspace = Workspace(id=uuid4(), name="Keyword Test", owner_user_id=_USER)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=_USER, role=WorkspaceRole.OWNER)
    )
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"kw_{uuid4().hex[:8]}",
        display_name="Keyword Context",
        created_by=_USER,
        is_private=False,
    )
    db_session.add(context)
    await db_session.flush()
    db_session.add(
        ContextSearchConfig(context_id=context.id, embedding_model=model, embedding_dimensions=_DIM)
    )
    await db_session.commit()
    keyword_stats.clear_keyword_stats_cache()
    try:
        yield context, collection
    finally:
        keyword_stats.clear_keyword_stats_cache()
        await qdrant.delete_collection(collection)


def _embedding_service() -> MagicMock:
    service = MagicMock()
    service.embed = AsyncMock(return_value=[0.125] * _DIM)
    return service


async def _write(db, memory: Memory, context: Context, collection: str) -> None:
    await write_memory_point(
        db,
        memory,
        collection_name=collection,
        embedding_service=_embedding_service(),
        user_id=memory.user_id,
        workspace_id=str(context.workspace_id),
        context_id=str(context.id),
    )
    await db.commit()


async def _point(qdrant: AsyncQdrantClient, collection: str, memory: Memory):
    (record,) = await qdrant.retrieve(
        collection_name=collection, ids=[str(memory.id)], with_payload=True, with_vectors=True
    )
    return record


def _bm25(record) -> dict[int, float]:
    vector = record.vector["bm25"]
    return dict(zip(vector.indices, vector.values, strict=True))


@pytest.mark.asyncio(loop_scope="session")
async def test_written_equals_rebuilt_and_the_rebuild_is_idempotent(db_session, qdrant, routed):
    context, collection = routed
    rows: list[Memory] = []
    for summary, context_summary, content in _TEXTS:
        memory = Memory(
            id=uuid4(),
            user_id=_USER,
            workspace_id=context.workspace_id,
            context_id=context.id,
            summary=summary,
            context_summary=context_summary or None,
            content=content,
            client="test",
            type="note",
            importance=0.5,
            embedding_status="success",
        )
        db_session.add(memory)
        rows.append(memory)
    await db_session.commit()
    for memory in rows:
        await _write(db_session, memory, context, collection)

    stored = {
        m.id: m
        for m in (
            await db_session.execute(select(Memory).where(Memory.context_id == context.id))
        ).scalars()
    }
    for memory in rows:
        payload = (await _point(qdrant, collection, memory)).payload
        assert stored[memory.id].keyword_summary_len == len(payload["summary_tokens"].split()) > 0
        assert stored[memory.id].keyword_content_len == len(payload["content_tokens"].split())
        assert stored[memory.id].keyword_fulltext_len is None
    # As written before #2986: the variant row's point carries dictionary forms.
    legacy = {
        "summary_tokens": "引越し 届出",
        "context_summary_tokens": "打合せ 議事録",
        "content_tokens": "附属 サーバ 設定 変更 する",
    }
    await qdrant.set_payload(collection_name=collection, payload=legacy, points=[str(rows[0].id)])
    before = {m.id: await _point(qdrant, collection, m) for m in rows}

    applied = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)

    # The first writes were weighted with averages that did not include the
    # later rows (cached, then empty): the rebuild moved them.
    assert applied.vectors_written >= 1
    assert applied.skipped == applied.missing_point == 0
    rebuilt = {m.id: await _point(qdrant, collection, m) for m in rows}
    assert applied.tokens_rewritten == 1
    variant = memory_keyword_tokens(stored[rows[0].id]).payload_fields()
    assert variant["summary_tokens"].split()[:1] == ["引っ越し"]
    assert "届け出" in variant["summary_tokens"].split()
    assert "打ち合わせ" in variant["context_summary_tokens"].split()
    assert {"付属", "サーバー"} <= set(variant["content_tokens"].split())
    for memory in rows:
        assert rebuilt[memory.id].vector["dense"] == before[memory.id].vector["dense"]
        expected_payload = dict(before[memory.id].payload)
        if memory.id == rows[0].id:
            expected_payload.update(variant)
        assert rebuilt[memory.id].payload == expected_payload

    # Written again with the same averages: the vector the rebuild wrote.
    keyword_stats.clear_keyword_stats_cache()
    target = rows[0]
    fresh = (await db_session.execute(select(Memory).where(Memory.id == target.id))).scalar_one()
    await _write(db_session, fresh, context, collection)
    written_point = await _point(qdrant, collection, target)
    assert {k: written_point.payload[k] for k in variant} == variant
    written = _bm25(written_point)
    expected = _bm25(rebuilt[target.id])
    assert written.keys() == expected.keys()
    for index, value in expected.items():
        assert written[index] == pytest.approx(value, rel=1e-6)

    again = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)
    assert again.written == 0
    assert again.unchanged == len(rows)

    plan = await run_keyword_rebuild(db_session, context_id=context.id)
    assert (plan.rows, plan.rows_without_lengths) == (len(rows), 0)


async def _memory(db_session, context: Context, summary: str, content: str) -> Memory:
    memory = Memory(
        id=uuid4(),
        user_id=_USER,
        workspace_id=context.workspace_id,
        context_id=context.id,
        summary=summary,
        context_summary=None,
        content=content,
        client="test",
        type="note",
        importance=0.5,
        embedding_status="success",
    )
    db_session.add(memory)
    await db_session.commit()
    return memory


@pytest.mark.asyncio(loop_scope="session")
async def test_a_point_deleted_before_the_page_write_is_counted_and_the_run_goes_on(
    db_session, qdrant, routed, monkeypatch
):
    """Qdrant fails the page's batch for a point that is gone; the rebuild
    writes the page point by point, counts that one and goes on."""
    from tasks import keyword_rebuild as task

    context, collection = routed
    gone = await _memory(db_session, context, "認証エラーの対処", "トークンを更新する。")
    kept = await _memory(db_session, context, "移行の手順", "バックアップを取ってから移行する。")
    for memory in (gone, kept):
        await _write(db_session, memory, context, collection)
    # Raw weights, as before #2984, so both points need a new vector.
    for memory in (gone, kept):
        vector = _bm25(await _point(qdrant, collection, memory))
        await qdrant.update_vectors(
            collection_name=collection,
            points=[
                PointVectors(
                    id=str(memory.id),
                    vector={
                        "bm25": SparseVector(
                            indices=list(vector), values=[v * 3 for v in vector.values()]
                        )
                    },
                )
            ],
        )
    real_retrieve = task.retrieve_keyword_points

    async def retrieve_then_delete(point_ids, collection_name, *, with_bm25=False):
        found = await real_retrieve(point_ids, collection_name, with_bm25=with_bm25)
        if with_bm25 and str(gone.id) in found:
            await qdrant.delete(collection_name=collection_name, points_selector=[str(gone.id)])
        return found

    monkeypatch.setattr(task, "retrieve_keyword_points", retrieve_then_delete)

    applied = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)

    assert applied.missing_point == 1
    assert applied.vectors_written == 1
    assert applied.managed_contexts == 0
    assert await qdrant.retrieve(collection_name=collection, ids=[str(gone.id)]) == []
    monkeypatch.setattr(task, "retrieve_keyword_points", real_retrieve)
    again = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)
    assert again.vectors_written == 0
    assert again.unchanged == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_point_without_tokens_loses_a_stale_bm25_vector(db_session, qdrant, routed):
    context, collection = routed
    memory = await _memory(db_session, context, "", "")
    await _write(db_session, memory, context, collection)
    written = await _point(qdrant, collection, memory)
    assert "bm25" not in (written.vector or {})
    await qdrant.update_vectors(
        collection_name=collection,
        points=[
            PointVectors(
                id=str(memory.id), vector={"bm25": SparseVector(indices=[7], values=[2.0])}
            )
        ],
    )

    applied = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)

    assert applied.vectors_written == 1
    record = await _point(qdrant, collection, memory)
    assert "bm25" not in (record.vector or {})
    assert record.vector["dense"] == written.vector["dense"]
    again = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)
    assert again.written == 0
    assert again.unchanged == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_the_migration_keeps_lengths_a_newer_write_stored(db_session, routed):
    """``reembed_context`` stores a page's lengths after its unlocked read: an
    edit or a pipeline write since then keeps its own lengths."""
    from services.embedding_migration_service import _store_keyword_lengths

    context, _ = routed
    edited = await _memory(db_session, context, "古い要約", "古い本文")
    reembedded = await _memory(db_session, context, "要約", "本文")
    untouched = await _memory(db_session, context, "そのまま", "本文")
    ids = [edited.id, reembedded.id, untouched.id]
    read = {
        m.id: m
        for m in (await db_session.execute(select(Memory).where(Memory.id.in_(ids)))).scalars()
    }
    db_session.expunge_all()
    await db_session.commit()

    # After the page's read: an edit (stamps updated_at) and a pipeline success
    # write (sets the digest), each storing the lengths of its own point.
    newer = dict.fromkeys(keyword_stats.LENGTH_COLUMNS, None) | {"keyword_summary_len": 42}
    await db_session.execute(
        update(Memory)
        .where(Memory.id == edited.id)
        .values(updated_at=datetime(2030, 1, 1), summary="新しい要約", **newer)
    )
    await db_session.execute(
        update(Memory)
        .where(Memory.id == reembedded.id)
        .values(embedded_text_digest="0" * 32, **newer)
    )
    await db_session.commit()

    stale = dict.fromkeys(keyword_stats.LENGTH_COLUMNS, None) | {"keyword_summary_len": 1}
    await _store_keyword_lengths(db_session, dict.fromkeys(ids, stale), read)

    stored = {
        m.id: m.keyword_summary_len
        for m in (
            await db_session.execute(
                select(Memory).where(Memory.id.in_(ids)).execution_options(populate_existing=True)
            )
        ).scalars()
    }
    assert stored == {edited.id: 42, reembedded.id: 42, untouched.id: 1}
