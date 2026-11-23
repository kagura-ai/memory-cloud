"""Integration: the keyword weighting against Postgres and Qdrant (#2984).

Memories are written by the real point writer (``write_memory_point``), which
stores their keyword lengths and weights each point with the context's cached
averages. The rebuild (``run_keyword_rebuild``) then brings every point to the
context's current averages, and a memory written again after it, with the same
averages, gets exactly the vector the rebuild wrote. A second rebuild writes
nothing. The dense vectors and the other payload keys stay as they were.

Skipped when ``QDRANT_URL`` is unreachable (and by ``db_session`` when Postgres
is). Run with::

    make test-integration
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select

from db import qdrant as qdrant_module
from db.qdrant import ensure_kagura_memories_collection, get_collection_name
from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory
from services import keyword_stats
from services.memory_service import write_memory_point
from tasks.keyword_rebuild import run_keyword_rebuild

_DIM = 8
_USER = "keyword_owner"

_TEXTS = [
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
    before = {m.id: await _point(qdrant, collection, m) for m in rows}

    applied = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)

    # The first writes were weighted with averages that did not include the
    # later rows (cached, then empty): the rebuild moved them.
    assert applied.vectors_written >= 1
    assert applied.skipped == applied.missing_point == 0
    rebuilt = {m.id: await _point(qdrant, collection, m) for m in rows}
    for memory in rows:
        assert rebuilt[memory.id].vector["dense"] == before[memory.id].vector["dense"]
        assert rebuilt[memory.id].payload == before[memory.id].payload

    # Written again with the same averages: the vector the rebuild wrote.
    keyword_stats.clear_keyword_stats_cache()
    target = rows[0]
    fresh = (await db_session.execute(select(Memory).where(Memory.id == target.id))).scalar_one()
    await _write(db_session, fresh, context, collection)
    written = _bm25(await _point(qdrant, collection, target))
    expected = _bm25(rebuilt[target.id])
    assert written.keys() == expected.keys()
    for index, value in expected.items():
        assert written[index] == pytest.approx(value, rel=1e-6)

    again = await run_keyword_rebuild(db_session, context_id=context.id, dry_run=False)
    assert again.written == 0
    assert again.unchanged == len(rows)

    plan = await run_keyword_rebuild(db_session, context_id=context.id)
    assert (plan.rows, plan.rows_without_lengths) == (len(rows), 0)
