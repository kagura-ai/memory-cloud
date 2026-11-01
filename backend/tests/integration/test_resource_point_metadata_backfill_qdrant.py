"""Integration: the resource point metadata backfill against Postgres and Qdrant (#2254).

A resource point indexed before v0.122.0 carries no ``type`` / ``tags`` /
``context_summary``, so recall's ``filters.type="resource_data"`` (``must``
on the payload key) does not match it. After ``--apply`` it does, the point
keeps its vectors and its other keys, and rows that do not own a resource
point are left alone. Routing is real: the context's ``ContextSearchConfig``
names a throwaway ``kagura_memories_*`` collection.

Skipped when ``QDRANT_URL`` is unreachable (and by ``db_session`` when Postgres
is). Run with::

    make test-integration
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct
from sqlalchemy import text

from db import qdrant as qdrant_module
from db.qdrant import (
    KAGURA_MEMORIES_VECTOR_NAME,
    _build_search_filter,
    ensure_kagura_memories_collection,
    get_collection_name,
)
from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory
from services.resource_indexer import resource_point_id
from tasks.resource_point_metadata_backfill import backfill_resource_point_metadata
from utils.datetime import utcnow

_DIM = 8
_USER = "backfill_owner"
_RESOURCE = "crm"


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
async def routed(db_session, qdrant: AsyncQdrantClient) -> AsyncIterator[tuple[Context, str]]:
    """A context whose search config routes it to a throwaway collection."""
    model = f"it-backfill-{uuid4().hex[:10]}"
    collection = get_collection_name(model, _DIM)
    await ensure_kagura_memories_collection(_DIM, collection)
    workspace = Workspace(id=uuid4(), name="Backfill Test", owner_user_id=_USER)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=_USER, role=WorkspaceRole.OWNER)
    )
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"bf_{uuid4().hex[:8]}",
        display_name="Backfill Context",
        created_by=_USER,
        is_private=False,
    )
    db_session.add(context)
    await db_session.flush()
    db_session.add(
        ContextSearchConfig(context_id=context.id, embedding_model=model, embedding_dimensions=_DIM)
    )
    await db_session.flush()
    try:
        yield context, collection
    finally:
        await qdrant.delete_collection(collection)


async def _seed_row(
    db,
    context: Context,
    *,
    doc_id: str,
    own_point: bool = True,
    deleted: bool = False,
    tags: list[str] | None = None,
    context_summary: str | None = "ｶﾀｶﾅ summary",
    memory_type: str = "resource_data",
    memory_id: UUID | None = None,
) -> Memory:
    memory_id = memory_id or uuid4()
    memory = Memory(
        id=memory_id,
        user_id=_USER,
        workspace_id=context.workspace_id,
        context_id=context.id,
        summary=f"[resource] {doc_id} v1",
        content='{"title": "x"}',
        context_summary=context_summary,
        client="test",
        source="resource_ingest",
        type=memory_type,
        tags=tags or [],
        importance=0.6,
        embedding_status="success",
        details={"resource_id": _RESOURCE, "doc_id": doc_id, "version": 1},
        summary_embedding_id=(resource_point_id(_RESOURCE, doc_id, 1) if own_point else memory_id),
        deleted_at=utcnow() if deleted else None,
    )
    db.add(memory)
    await db.flush()
    return memory


async def _write_old_point(qdrant: AsyncQdrantClient, collection: str, context, memory) -> UUID:
    """The point as the indexer wrote it before v0.122.0: no row metadata."""
    point_id = memory.summary_embedding_id
    await qdrant.upsert(
        collection_name=collection,
        points=[
            PointStruct(
                id=str(point_id),
                vector={KAGURA_MEMORIES_VECTOR_NAME: [0.1] * _DIM},
                payload={
                    "workspace_id": str(context.workspace_id),
                    "context_id": str(context.id),
                    "user_id": _USER,
                    "resource_id": _RESOURCE,
                    "doc_id": memory.details["doc_id"],
                    "version": 1,
                    "content": "x",
                    "memory_id": str(memory.id),
                },
            )
        ],
        wait=True,
    )
    return point_id


async def _type_matches(qdrant: AsyncQdrantClient, collection: str, context) -> set[str]:
    """Point ids recall's ``filters.type="resource_data"`` keeps in ``context``."""
    points, _ = await qdrant.scroll(
        collection_name=collection,
        scroll_filter=_build_search_filter(
            workspace_id=str(context.workspace_id),
            context_id=str(context.id),
            user_id=_USER,
            is_shared_context=True,
            filters={"type": "resource_data"},
        ),
        limit=100,
        with_payload=False,
        with_vectors=False,
    )
    return {str(p.id) for p in points}


class TestResourcePointMetadataBackfill:
    @pytest.mark.asyncio
    async def test_an_old_point_matches_the_type_filter_after_apply(
        self, db_session, qdrant, routed
    ):
        context, collection = routed
        old = await _seed_row(db_session, context, doc_id="doc-old", tags=["crm"])
        old_point = await _write_old_point(qdrant, collection, context, old)
        # A deleted row's point and a remember(external_id) row (its own id)
        # are not resource points to backfill.
        gone = await _seed_row(db_session, context, doc_id="doc-gone", deleted=True)
        gone_point = await _write_old_point(qdrant, collection, context, gone)
        own_id = await _seed_row(db_session, context, doc_id="doc-own", own_point=False)
        own_id_point = await _write_old_point(qdrant, collection, context, own_id)
        await db_session.commit()
        assert await _type_matches(qdrant, collection, context) == set()

        plan = await backfill_resource_point_metadata(
            db_session, dry_run=True, workspace_id=context.workspace_id
        )
        assert (plan.rows, plan.contexts, plan.written) == (1, 1, 0)
        assert await _type_matches(qdrant, collection, context) == set()
        [before] = await qdrant.retrieve(
            collection_name=collection, ids=[str(old_point)], with_vectors=True
        )

        applied = await backfill_resource_point_metadata(
            db_session, dry_run=False, workspace_id=context.workspace_id
        )
        assert (applied.written, applied.shared_skipped, applied.failed) == (1, 0, [])
        assert await _type_matches(qdrant, collection, context) == {str(old_point)}

        [point] = await qdrant.retrieve(
            collection_name=collection, ids=[str(old_point)], with_vectors=True
        )
        payload = point.payload or {}
        assert payload["type"] == "resource_data"
        assert payload["tags"] == ["crm"]
        assert payload["context_summary"] == "カタカナ summary"
        # Set-payload only: the indexer's keys and the vector are untouched.
        assert payload["doc_id"] == "doc-old"
        assert payload["memory_id"] == str(old.id)
        assert point.vector == before.vector

        untouched = await qdrant.retrieve(
            collection_name=collection, ids=[str(gone_point), str(own_id_point)]
        )
        assert all("type" not in (p.payload or {}) for p in untouched)
        assert len(untouched) == 2

        # Idempotent: a second run writes the same values again.
        again = await backfill_resource_point_metadata(
            db_session, dry_run=False, workspace_id=context.workspace_id
        )
        assert (again.written, again.failed) == (1, [])
        assert await _type_matches(qdrant, collection, context) == {str(old_point)}

    @pytest.mark.asyncio
    async def test_a_row_whose_point_is_gone_fails_alone(self, db_session, qdrant, routed):
        context, collection = routed
        missing = await _seed_row(db_session, context, doc_id="doc-missing")
        present = await _seed_row(db_session, context, doc_id="doc-present")
        present_point = await _write_old_point(qdrant, collection, context, present)
        await db_session.commit()

        applied = await backfill_resource_point_metadata(
            db_session, dry_run=False, workspace_id=context.workspace_id
        )
        assert applied.failed == [missing.id]
        assert applied.written == 1
        assert await _type_matches(qdrant, collection, context) == {str(present_point)}

    @pytest.mark.asyncio
    async def test_a_failed_routing_lookup_fails_only_its_row(self, db_session, qdrant, routed):
        # A real Postgres error in the routing SELECT aborts the transaction
        # unless it runs in a SAVEPOINT; the rows after it must still be
        # written (the walk goes in id order, so the broken row goes first).
        from services import context_routing

        context, collection = routed
        broken = Context(
            id=uuid4(),
            workspace_id=context.workspace_id,
            name=f"bf_{uuid4().hex[:8]}",
            display_name="Broken Routing",
            created_by=_USER,
            is_private=False,
        )
        db_session.add(broken)
        await db_session.flush()
        first = await _seed_row(
            db_session,
            broken,
            doc_id="doc-broken",
            memory_id=UUID(f"00000000-0000-4000-8000-{uuid4().hex[:12]}"),
        )
        await _write_old_point(qdrant, collection, broken, first)
        second = await _seed_row(
            db_session,
            context,
            doc_id="doc-fine",
            memory_id=UUID(f"ffffffff-ffff-4fff-8fff-{uuid4().hex[:12]}"),
        )
        second_point = await _write_old_point(qdrant, collection, context, second)
        await db_session.commit()

        fetch = context_routing._fetch_config

        async def failing_fetch(db, context_id):
            if context_id == broken.id:
                await db.execute(text("SELECT 1/0"))
            return await fetch(db, context_id)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(context_routing, "_fetch_config", failing_fetch)
            applied = await backfill_resource_point_metadata(
                db_session, dry_run=False, workspace_id=context.workspace_id
            )
        assert applied.failed == [first.id]
        assert applied.written == 1
        assert await _type_matches(qdrant, collection, context) == {str(second_point)}
