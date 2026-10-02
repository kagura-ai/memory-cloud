"""Integration tests: context deletion and the orphan sweep against a live Qdrant (#1798).

Deleting a context used to soft-delete its memories and leave every point in
the vector store; after the tombstone purge those points had no row at all.
These tests drive the real ``ContextService`` and the real sweep against a
real Qdrant and a real Postgres.

Local-only, like ``test_resource_indexer_qdrant.py``: skipped when
``QDRANT_URL`` is unreachable (and by ``db_session`` when Postgres is). Run with::

    make test-integration
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
)

from config.settings import get_settings
from db import qdrant as qdrant_module
from db.qdrant import (
    KAGURA_MEMORIES_VECTOR_NAME,
    add_memory_to_qdrant,
    ensure_kagura_memories_collection,
    get_collection_name,
)
from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from services.context_service import ContextService
from services.orphan_vector_sweep import sweep_orphan_points
from utils.datetime import utcnow

_DIM = 8
_USER = "vec_owner"


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


@pytest_asyncio.fixture(loop_scope="session")
async def qdrant(monkeypatch) -> AsyncIterator[AsyncQdrantClient]:
    """A live client, also installed as the module client ``db.qdrant`` uses."""
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
async def collection(qdrant: AsyncQdrantClient) -> AsyncIterator[str]:
    """A throwaway ``kagura_memories*`` collection — the prefix is what makes
    ``delete_context`` find it; the suffix keeps the sweep off anything else."""
    name = f"kagura_memories_it_{uuid4().hex[:12]}"
    await ensure_kagura_memories_collection(_DIM, name)
    yield name
    await qdrant.delete_collection(name)


async def _seed_context(db, *, workspace: Workspace | None = None) -> tuple[Workspace, Context]:
    if workspace is None:
        workspace = Workspace(id=uuid4(), name="Vec Test", owner_user_id=_USER)
        db.add(workspace)
        await db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=_USER, role=WorkspaceRole.OWNER))
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"vec_{uuid4().hex[:8]}",
        display_name="Vec Context",
        created_by=_USER,
        is_private=False,
    )
    db.add(context)
    await db.flush()
    return workspace, context


async def _seed_memory(db, context: Context, collection: str, **overrides) -> Memory:
    """A memory row plus its point, the way ``remember()`` leaves them."""
    memory = Memory(
        id=uuid4(),
        user_id=_USER,
        workspace_id=context.workspace_id,
        context_id=context.id,
        summary="a summary long enough",
        content="content",
        client="test",
        type="note",
        importance=0.5,
        embedding_status="success",
        **overrides,
    )
    db.add(memory)
    await db.flush()
    await _add_point(memory.id, context, collection)
    return memory


async def _add_point(memory_id: UUID, context: Context, collection: str) -> None:
    await add_memory_to_qdrant(
        user_id=_USER,
        memory_id=memory_id,
        vector=[0.1] * _DIM,
        payload={"summary": "a summary long enough"},
        workspace_id=str(context.workspace_id),
        context_id=str(context.id),
        collection_name=collection,
    )


async def _context_point_count(qdrant: AsyncQdrantClient, collection: str, context_id) -> int:
    result = await qdrant.count(
        collection_name=collection,
        count_filter=Filter(
            must=[FieldCondition(key="context_id", match=MatchValue(value=str(context_id)))]
        ),
        exact=True,
    )
    return result.count


async def _point_ids(qdrant: AsyncQdrantClient, collection: str) -> set[str]:
    points, _ = await qdrant.scroll(
        collection_name=collection, limit=1000, with_payload=False, with_vectors=False
    )
    return {str(p.id) for p in points}


class TestDeleteContextRemovesPoints:
    @pytest.mark.asyncio
    async def test_delete_context_removes_its_points_and_only_its_points(
        self, db_session, qdrant, collection
    ):
        workspace, doomed = await _seed_context(db_session)
        _, kept = await _seed_context(db_session, workspace=workspace)
        for _ in range(3):
            await _seed_memory(db_session, doomed, collection)
        survivor = await _seed_memory(db_session, kept, collection)
        await db_session.commit()
        assert await _context_point_count(qdrant, collection, doomed.id) == 3

        await ContextService(db_session).delete_context(_USER, doomed.id)

        assert await _context_point_count(qdrant, collection, doomed.id) == 0
        assert await _point_ids(qdrant, collection) == {str(survivor.id)}

    @pytest.mark.asyncio
    async def test_merge_with_delete_source_leaves_no_source_points(self, db_session, qdrant):
        # merge_contexts resolves the collection from the embedding settings,
        # so this one runs in the deployment default collection (random ids,
        # removed again below).
        settings = get_settings()
        merge_collection = get_collection_name(
            settings.embedding_model, settings.embedding_dimensions
        )
        await ensure_kagura_memories_collection(settings.embedding_dimensions, merge_collection)
        workspace, source = await _seed_context(db_session)
        _, target = await _seed_context(db_session, workspace=workspace)
        memories = [
            Memory(
                id=uuid4(),
                user_id=_USER,
                workspace_id=workspace.id,
                context_id=source.id,
                summary="a summary long enough",
                content="content",
                client="test",
                type="note",
                importance=0.5,
                embedding_status="success",
            )
            for _ in range(2)
        ]
        db_session.add_all(memories)
        await db_session.flush()
        for memory in memories:
            await add_memory_to_qdrant(
                user_id=_USER,
                memory_id=memory.id,
                vector=[0.1] * settings.embedding_dimensions,
                payload={"summary": "a summary long enough"},
                workspace_id=str(workspace.id),
                context_id=str(source.id),
                collection_name=merge_collection,
            )
        await db_session.commit()

        try:
            result = await ContextService(db_session).merge_contexts(
                _USER, source.id, target.id, delete_source=True
            )

            assert result["merged"] == 2
            assert await _context_point_count(qdrant, merge_collection, source.id) == 0
            assert await _context_point_count(qdrant, merge_collection, target.id) == 2
        finally:
            await qdrant.delete(
                collection_name=merge_collection,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="workspace_id", match=MatchValue(value=str(workspace.id))
                        )
                    ]
                ),
            )


class TestOrphanSweepLive:
    @pytest.mark.asyncio
    async def test_sweep_removes_orphans_and_keeps_live_points(
        self, db_session, qdrant, collection
    ):
        workspace, live_ctx = await _seed_context(db_session)
        _, dead_ctx = await _seed_context(db_session, workspace=workspace)
        live = await _seed_memory(db_session, live_ctx, collection)
        just_deleted = await _seed_memory(
            db_session, live_ctx, collection, deleted_at=utcnow(), deleted_by=_USER
        )
        long_deleted = await _seed_memory(
            db_session,
            live_ctx,
            collection,
            deleted_at=utcnow() - timedelta(days=2),
            deleted_by=_USER,
        )
        no_row_id = uuid4()
        await _add_point(no_row_id, live_ctx, collection)
        # A context deleted before #1798: rows tombstoned, points left behind.
        in_dead_ctx = await _seed_memory(
            db_session,
            dead_ctx,
            collection,
            deleted_at=utcnow() - timedelta(days=2),
            deleted_by=_USER,
        )
        dead_ctx.deleted_at = utcnow() - timedelta(days=2)
        # A resource point: its id is not a memory id, so only its context
        # can vouch for it.
        resource_point_id = uuid4()
        await qdrant.upsert(
            collection_name=collection,
            points=[
                PointStruct(
                    id=str(resource_point_id),
                    vector={KAGURA_MEMORIES_VECTOR_NAME: [0.1] * _DIM},
                    payload={
                        "workspace_id": str(workspace.id),
                        "context_id": str(live_ctx.id),
                        "user_id": _USER,
                        "resource_id": "docs",
                        "doc_id": "a",
                    },
                )
            ],
        )
        await db_session.commit()
        before = await _point_ids(qdrant, collection)
        orphans = {str(long_deleted.id), str(no_row_id), str(in_dead_ctx.id)}
        kept = {str(live.id), str(just_deleted.id), str(resource_point_id)}
        assert before == orphans | kept

        plan = await sweep_orphan_points(db_session, dry_run=True, collections=[collection])

        assert plan.orphans == 3
        assert plan.deleted == 0
        assert await _point_ids(qdrant, collection) == before

        applied = await sweep_orphan_points(db_session, dry_run=False, collections=[collection])

        assert applied.deleted == 3
        assert await _point_ids(qdrant, collection) == kept

        again = await sweep_orphan_points(db_session, dry_run=False, collections=[collection])
        assert again.orphans == 0
