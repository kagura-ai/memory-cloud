"""A resource point follows the context that holds its slug, on Postgres and Qdrant.

A resource point's id is derived from the document version (uuid5 of
``resource_id:doc_id:vN``), not from the context. A context that releases its
slug (unpublish, or a ``resource_id`` change) keeps its rows, and another
context can take the slug and index the same document version. The point
then belongs to the live row of the context that holds the slug; every other
live row that names it is detached: it becomes an ordinary memory with a point
under its own id, keeping its data, workspace and context.

These tests drive the real indexer, the real point writer, the Sleep and
migration callers and the orphan sweep against a real Postgres and Qdrant; only
the embedding provider is a stand-in (a vector derived from the text). Skipped
when ``QDRANT_URL`` is unreachable (and by ``db_session`` when Postgres is).
"""

from __future__ import annotations

import hashlib
import math
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, select, text

from db import qdrant as qdrant_module
from db.qdrant import (
    KAGURA_MEMORIES_VECTOR_NAME,
    _point_ref,
    ensure_kagura_memories_collection,
    get_collection_name,
    search_memories_qdrant,
)
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory
from models.resource import IndexerState, Resource, ResourceEvent, ResourceSchema
from models.schemas import RecallRequest
from services.memory_service import MemoryService
from services.resource_indexer import (
    INDEXER_SOURCE,
    RESOURCE_MEMORY_TYPE,
    ResourceIndexer,
    build_resource_point,
    resource_point_id,
)
from utils.datetime import utcnow

_DIM = 8
_DOC = "doc-1"
_ALPHA = "Alpha quarterly figures"
_BRAVO = "Bravo onboarding guide"
_SCHEMA = [{"name": "title", "classification": "public", "index_hint": "fulltext"}]


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


def _doc_text(title: str) -> str:
    """What the indexer embeds for ``{"title": title}`` under ``_SCHEMA``."""
    return f"title: {title}"


def _vec(text_: str) -> list[float]:
    """A stand-in embedding: a unit vector derived from the text."""
    digest = hashlib.sha256(text_.encode()).digest()
    raw = [byte / 255.0 + 0.01 for byte in digest[:_DIM]]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _embedder() -> MagicMock:
    async def embed(text: str, *args: Any, **kwargs: Any) -> list[float]:
        return _vec(text)

    async def embed_with_usage(text: str, *args: Any, **kwargs: Any) -> tuple[list[float], int]:
        return _vec(text), 0

    async def embed_batch(texts: list[str], *args: Any, **kwargs: Any) -> list[list[float]]:
        return [_vec(t) for t in texts]

    service = MagicMock()
    service.model = "it-model"
    service.embed = AsyncMock(side_effect=embed)
    service.embed_with_usage = AsyncMock(side_effect=embed_with_usage)
    service.embed_batch = AsyncMock(side_effect=embed_batch)
    return service


def _same_vector(actual: Any, expected: list[float]) -> bool:
    return actual is not None and all(
        math.isclose(a, e, abs_tol=1e-5) for a, e in zip(actual, expected, strict=True)
    )


@dataclass
class World:
    """One slug, two contexts: A and B, in two workspaces or in one.

    In one workspace the two contexts share the workspace's resource, so B
    indexes the document events A indexed (``title_b`` is A's title).
    """

    client: AsyncQdrantClient
    collection: str
    slug: str
    user_a: str
    user_b: str
    ws_a: Workspace
    ws_b: Workspace
    ctx_a: Context
    ctx_b: Context
    res_a: Resource
    res_b: Resource
    one_workspace: bool = False

    @property
    def title_b(self) -> str:
        return _ALPHA if self.one_workspace else _BRAVO

    @property
    def point(self) -> UUID:
        return resource_point_id(self.slug, _DOC, 1)

    async def reload(self, db) -> None:
        """Load the setup rows again after a rollback expired them."""
        for row in {
            id(r): r for r in (self.ws_a, self.ws_b, self.ctx_a, self.ctx_b, self.res_a, self.res_b)
        }.values():
            await db.refresh(row)


@pytest_asyncio.fixture(loop_scope="session")
async def qdrant(monkeypatch) -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(url=_qdrant_url(), timeout=30)
    try:
        await client.get_collections()
    except Exception as exc:  # noqa: BLE001 — intentional broad skip guard
        await client.close()
        pytest.skip(f"Qdrant unreachable at {_qdrant_url()}: {exc}")
    monkeypatch.setattr(qdrant_module, "_qdrant_client", client)
    yield client
    await client.close()


async def _workspace(db, prefix: str) -> tuple[str, Workspace]:
    user = f"{prefix}_{uuid.uuid4().hex[:8]}"
    db.add(User(email=f"{user}@test.example", user_id=user, name=user, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"{prefix}-ws-{uuid.uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=user,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user, role=WorkspaceRole.OWNER))
    await db.flush()
    return user, ws


async def _context(db, user: str, ws: Workspace, model: str, slug: str | None) -> Context:
    ctx = Context(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        name=f"holder-{uuid.uuid4().hex[:8]}",
        display_name="Holder",
        created_by=user,
        is_private=False,
        is_public=slug is not None,
        resource_id=slug,
    )
    db.add(ctx)
    await db.flush()
    db.add(
        ContextSearchConfig(
            context_id=ctx.id,
            semantic_weight=0.6,
            fetch_factor=3,
            embedding_model=model,
            embedding_dimensions=_DIM,
        )
    )
    await db.flush()
    return ctx


async def _resource(db, ws: Workspace, slug: str) -> Resource:
    resource = Resource(id=uuid.uuid4(), workspace_id=ws.id, resource_id=slug)
    db.add(resource)
    await db.flush()
    db.add(
        ResourceSchema(
            resource_pk=resource.id, resource_id=slug, schema_version=1, field_definitions=_SCHEMA
        )
    )
    await db.flush()
    return resource


@pytest_asyncio.fixture(loop_scope="session", params=["two_workspaces", "one_workspace"])
async def world(request, db_session, qdrant) -> AsyncIterator[World]:
    """Context A holds the slug; context B exists without one, in another
    workspace or in A's."""
    one_workspace = request.param == "one_workspace"
    model = f"it-holder-{uuid.uuid4().hex[:10]}"
    collection = get_collection_name(model, _DIM)
    await ensure_kagura_memories_collection(_DIM, collection)
    slug = f"res-{uuid.uuid4().hex[:8]}"
    user_a, ws_a = await _workspace(db_session, "holder_a")
    if one_workspace:
        user_b, ws_b = f"holder_b_{uuid.uuid4().hex[:8]}", ws_a
        db_session.add(
            User(email=f"{user_b}@test.example", user_id=user_b, name=user_b, role="user")
        )
        await db_session.flush()
        db_session.add(
            WorkspaceMember(workspace_id=ws_a.id, user_id=user_b, role=WorkspaceRole.ADMIN)
        )
        await db_session.flush()
    else:
        user_b, ws_b = await _workspace(db_session, "holder_b")
    ctx_a = await _context(db_session, user_a, ws_a, model, slug)
    ctx_b = await _context(db_session, user_b, ws_b, model, None)
    res_a = await _resource(db_session, ws_a, slug)
    res_b = res_a if one_workspace else await _resource(db_session, ws_b, slug)
    await db_session.commit()
    w = World(
        qdrant,
        collection,
        slug,
        user_a,
        user_b,
        ws_a,
        ws_b,
        ctx_a,
        ctx_b,
        res_a,
        res_b,
        one_workspace,
    )
    # Plain ids for the cleanup: a rollback expires the ORM objects.
    ws_ids = list({ws_a.id, ws_b.id})
    ctx_ids = [ctx_a.id, ctx_b.id]
    res_ids = list({res_a.id, res_b.id})
    try:
        yield w
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Memory).where(Memory.workspace_id.in_(ws_ids)))
        await db_session.execute(delete(IndexerState).where(IndexerState.context_id.in_(ctx_ids)))
        for model_ in (ResourceEvent, ResourceSchema):
            await db_session.execute(delete(model_).where(model_.resource_pk.in_(res_ids)))
        await db_session.execute(delete(Resource).where(Resource.id.in_(res_ids)))
        await db_session.execute(
            delete(ContextSearchConfig).where(ContextSearchConfig.context_id.in_(ctx_ids))
        )
        await db_session.execute(delete(Context).where(Context.id.in_(ctx_ids)))
        await db_session.execute(
            delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(ws_ids))
        )
        await db_session.execute(delete(Workspace).where(Workspace.id.in_(ws_ids)))
        await db_session.execute(delete(User).where(User.user_id.in_([user_a, user_b])))
        await db_session.commit()
        await qdrant.delete_collection(collection)


async def _event(
    db, resource: Resource, title: str | None, *, version: int | None = 1, op: str = "upsert"
) -> None:
    if op == "upsert":
        queued = await db.scalar(
            select(ResourceEvent.id).where(
                ResourceEvent.resource_pk == resource.id,
                ResourceEvent.doc_id == _DOC,
                ResourceEvent.version == version,
                ResourceEvent.op == "upsert",
            )
        )
        if queued is not None:
            # One workspace: the contexts share the resource and its events.
            return
    db.add(
        ResourceEvent(
            resource_pk=resource.id,
            resource_id=resource.resource_id,
            op=op,
            doc_id=_DOC,
            version=version,
            payload={"title": title} if title is not None else None,
            created_at=utcnow(),
        )
    )
    await db.commit()


async def _index(db, w: World, ctx: Context):
    with patch("services.resource_indexer.get_qdrant_client", return_value=w.client):
        indexer = ResourceIndexer(db)
    indexer._plan_suspended = AsyncMock(return_value=False)
    with patch(
        "services.resource_indexer.resolve_context_routing",
        AsyncMock(return_value=(w.collection, _embedder())),
    ):
        return await indexer.process_incremental(w.slug, ctx.id)


async def _release(db, w: World, how: str) -> None:
    """Context A gives the slug up, as ``ContextService.update_context`` does."""
    if how == "unpublish":
        w.ctx_a.is_public = False
        w.ctx_a.resource_id = None
    else:
        w.ctx_a.resource_id = f"renamed-{uuid.uuid4().hex[:8]}"
    await db.commit()


async def _claim(db, w: World) -> None:
    w.ctx_b.resource_id = w.slug
    w.ctx_b.is_public = True
    await db.commit()


async def _rows(db, ctx: Context) -> list[Memory]:
    result = await db.execute(
        select(Memory)
        .where(Memory.context_id == ctx.id, Memory.deleted_at.is_(None))
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _one_row(db, ctx: Context) -> Memory:
    rows = await _rows(db, ctx)
    assert len(rows) == 1, rows
    return rows[0]


async def _point(w: World, point_id: UUID | str, collection: str | None = None):
    found = await w.client.retrieve(
        collection_name=collection or w.collection,
        ids=[str(point_id)],
        with_payload=True,
        with_vectors=True,
    )
    return found[0] if found else None


def _dense(point: Any) -> Any:
    vectors = point.vector if point is not None else None
    return vectors.get(KAGURA_MEMORIES_VECTOR_NAME) if isinstance(vectors, dict) else None


async def _index_a_then_hand_over(db, w: World, how: str) -> Memory:
    """A indexes the document, releases the slug, B takes it. Returns A's row."""
    await _event(db, w.res_a, _ALPHA)
    metrics = await _index(db, w, w.ctx_a)
    assert metrics.errors == 0, metrics.reason
    row_a = await _one_row(db, w.ctx_a)
    assert row_a.summary_embedding_id == w.point
    await _release(db, w, how)
    await _claim(db, w)
    return row_a


async def _indexer_shaped_row(db, ctx: Context, user: str, w: World, title: str) -> Memory:
    """A row as the indexer writes it for the document, naming the shared point."""
    row = Memory(
        id=uuid.uuid4(),
        user_id=user,
        workspace_id=ctx.workspace_id,
        context_id=ctx.id,
        summary=f"[{w.slug}] {_DOC} v1",
        context_summary=_doc_text(title),
        content=f'{{"title": "{title}"}}',
        details={"resource_id": w.slug, "doc_id": _DOC, "version": 1},
        context={"context_id": str(ctx.id)},
        type=RESOURCE_MEMORY_TYPE,
        importance=0.6,
        scope="persistent",
        promoted_at=utcnow(),
        client="resource_indexer",
        source=INDEXER_SOURCE,
        source_type="connector",
        tags=[],
        summary_embedding_id=w.point,
        embedding_status="success",
    )
    db.add(row)
    await db.flush()
    return row


async def _write_shared_point(w: World, ctx: Context, row: Memory, title: str) -> None:
    point = build_resource_point(
        context=ctx,
        resource_id=w.slug,
        doc_id=_DOC,
        version=1,
        content=_doc_text(title),
        projected={
            "fulltext_content": _doc_text(title),
            "facets": {},
            "sortable": {},
            "metadata": {"title": title},
        },
        embedding=_vec(_doc_text(title)),
        updated_at=utcnow(),
        memory_id=row.id,
        user_id=row.user_id,
    )
    await w.client.upsert(collection_name=w.collection, points=[point], wait=True)


async def _shared_state(db, w: World, *, last_writer: str) -> tuple[Memory, Memory]:
    """Two live rows naming one point, as releases before this rule left them.

    A released the slug, B holds it; the point carries ``last_writer``'s row.
    """
    await _release(db, w, "unpublish")
    await _claim(db, w)
    row_a = await _indexer_shaped_row(db, w.ctx_a, w.user_a, w, _ALPHA)
    row_b = await _indexer_shaped_row(db, w.ctx_b, w.user_b, w, _BRAVO)
    await db.commit()
    if last_writer == "a":
        await _write_shared_point(w, w.ctx_a, row_a, _ALPHA)
    else:
        await _write_shared_point(w, w.ctx_b, row_b, _BRAVO)
    return row_a, row_b


async def _recall_ids(db, w: World, ctx: Context, user: str, ws: Workspace, title: str):
    """Recall in ``ctx``: the real vector search, then hydration on the database."""
    hits = await search_memories_qdrant(
        user_id=user,
        query_vector=_vec(_doc_text(title)),
        workspace_id=str(ws.id),
        context_id=str(ctx.id),
        limit=5,
        collection_name=w.collection,
    )
    svc = MemoryService(db)
    svc.search_service.hybrid_search = AsyncMock(return_value=hits)
    response = await svc.recall(
        RecallRequest(query=title, k=5, use_rerank=False),
        user_id=user,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return [r.memory_id for r in response.results]


# --------------------------------------------------------------------------
# Claim time
# --------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("how", ["unpublish", "resource_id_change"])
async def test_claim_detaches_the_former_holder_row(db_session, world, how):
    w = world
    row_a = await _index_a_then_hand_over(db_session, w, how)
    await _event(db_session, w.res_b, w.title_b)

    metrics = await _index(db_session, w, w.ctx_b)

    assert metrics.errors == 0, metrics.reason
    assert metrics.applied_upserts == 1
    row_b = await _one_row(db_session, w.ctx_b)
    assert row_b.summary_embedding_id == w.point
    shared = await _point(w, w.point)
    assert shared is not None
    assert shared.payload["context_id"] == str(w.ctx_b.id)
    assert shared.payload["memory_id"] == str(row_b.id)
    assert _same_vector(_dense(shared), _vec(_doc_text(w.title_b)))

    detached = await _one_row(db_session, w.ctx_a)
    assert detached.id == row_a.id
    assert detached.summary_embedding_id == row_a.id
    assert detached.content == row_a.content
    assert detached.workspace_id == w.ws_a.id
    own = await _point(w, row_a.id)
    assert own is not None
    assert own.payload["context_id"] == str(w.ctx_a.id)
    assert own.payload["workspace_id"] == str(w.ws_a.id)
    assert own.payload["user_id"] == w.user_a
    assert _same_vector(_dense(own), _vec(_doc_text(_ALPHA)))

    assert await _recall_ids(db_session, w, w.ctx_b, w.user_b, w.ws_b, w.title_b) == [row_b.id]
    assert await _recall_ids(db_session, w, w.ctx_a, w.user_a, w.ws_a, _ALPHA) == [row_a.id]


@pytest.mark.asyncio(loop_scope="session")
async def test_claim_waits_for_a_busy_former_holder_row(db_session, async_engine, world):
    """A row another transaction holds stops the batch before the event, unrecorded."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    w = world
    row_a = await _index_a_then_hand_over(db_session, w, "unpublish")
    await _event(db_session, w.res_b, w.title_b)

    async with async_sessionmaker(async_engine, expire_on_commit=False)() as holder:
        await holder.execute(
            text("SELECT id FROM memories WHERE id = :id FOR UPDATE"), {"id": row_a.id}
        )
        metrics = await _index(db_session, w, w.ctx_b)
        await holder.rollback()

    assert metrics.errors == 0, metrics.reason
    assert metrics.applied_upserts == 0
    assert metrics.failed_events == []
    assert await _rows(db_session, w.ctx_b) == []
    shared = await _point(w, w.point)
    assert shared is not None and shared.payload["context_id"] == str(w.ctx_a.id)

    metrics = await _index(db_session, w, w.ctx_b)
    assert metrics.errors == 0, metrics.reason
    assert metrics.applied_upserts == 1
    assert (await _one_row(db_session, w.ctx_a)).summary_embedding_id == row_a.id


@pytest.mark.asyncio(loop_scope="session")
async def test_a_released_context_does_not_index_the_slug_any_more(db_session, world):
    """An upsert left in the former holder's backlog writes no point and no row."""
    w = world
    row_a = await _index_a_then_hand_over(db_session, w, "unpublish")
    await _event(db_session, w.res_a, "Alpha second edition", version=2)

    metrics = await _index(db_session, w, w.ctx_a)

    assert metrics.errors == 0, metrics.reason
    assert await _point(w, resource_point_id(w.slug, _DOC, 2)) is None
    rows = await _rows(db_session, w.ctx_a)
    assert [(r.id, r.resource_version) for r in rows] == [(row_a.id, 1)]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_detach_retried_after_a_rollback_keeps_the_rows_own_vector(db_session, world):
    """The first attempt's copy is reused: a retry never copies the new holder's vector."""
    from services.resource_indexer import detach_resource_rows

    w = world
    row_a_id = (await _index_a_then_hand_over(db_session, w, "unpublish")).id

    assert await detach_resource_rows(db_session, w.point, holder_context_id=w.ctx_b.id) == 1
    # The new holder's upsert lands, then its transaction rolls back.
    row_b = await _indexer_shaped_row(db_session, w.ctx_b, w.user_b, w, _BRAVO)
    await _write_shared_point(w, w.ctx_b, row_b, _BRAVO)
    await db_session.rollback()
    await w.reload(db_session)
    assert (await _one_row(db_session, w.ctx_a)).summary_embedding_id == w.point

    assert await detach_resource_rows(db_session, w.point, holder_context_id=w.ctx_b.id) == 1
    await db_session.commit()
    assert await detach_resource_rows(db_session, w.point, holder_context_id=w.ctx_b.id) == 0
    await db_session.commit()

    detached = await _one_row(db_session, w.ctx_a)
    assert detached.summary_embedding_id == row_a_id
    own = await _point(w, row_a_id)
    assert _same_vector(_dense(own), _vec(_doc_text(_ALPHA)))


@pytest.mark.asyncio(loop_scope="session")
async def test_a_detach_without_the_shared_point_queues_the_row_under_its_own_id(db_session, world):
    from services.resource_indexer import detach_resource_rows

    w = world
    row_a, _row_b = await _shared_state(db_session, w, last_writer="b")
    await w.client.delete(collection_name=w.collection, points_selector=[str(w.point)])

    assert await detach_resource_rows(db_session, w.point, holder_context_id=w.ctx_b.id) == 1
    await db_session.commit()

    detached = await _one_row(db_session, w.ctx_a)
    assert detached.summary_embedding_id == row_a.id
    assert detached.embedding_status == "pending"
    assert await _point(w, row_a.id) is None


# --------------------------------------------------------------------------
# Re-embed: every caller of the point writer
# --------------------------------------------------------------------------


async def _rebuild_via(db, w: World, row: Memory, ctx: Context, user: str, caller: str) -> None:
    from services.memory_service import write_memory_point

    # The pipeline and Sleep load the row fresh; the generated columns expired
    # with the commit.
    await db.refresh(row)
    if caller == "point_writer":
        await write_memory_point(
            db,
            row,
            collection_name=w.collection,
            embedding_service=_embedder(),
            user_id=user,
            workspace_id=str(ctx.workspace_id),
            context_id=str(ctx.id),
        )
        await db.commit()
    elif caller == "sleep_reindex":
        from services.sleep.reindex import ReindexPhase

        phase = ReindexPhase(db, collection_name=w.collection)
        phase.embedding_service = _embedder()
        result = await phase.execute(
            {row.id}, user, workspace_id=str(ctx.workspace_id), context_id=str(ctx.id)
        )
        assert result.details["failed"] == 0, result.details
        await db.commit()
    elif caller == "sleep_undo":
        from services.sleep.undo import re_embed_memory_to_qdrant

        assert (
            await re_embed_memory_to_qdrant(
                row,
                user,
                _embedder(),
                str(ctx.workspace_id),
                str(ctx.id),
                w.collection,
                db=db,
            )
            is None
        )
        await db.commit()
    else:
        raise AssertionError(caller)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("caller", ["point_writer", "sleep_reindex", "sleep_undo"])
async def test_a_former_holder_rebuild_writes_its_own_point(db_session, world, caller):
    """A's edited row is re-embedded under its own id; the holder's point is untouched."""
    w = world
    row_a, row_b = await _shared_state(db_session, w, last_writer="b")
    row_a.summary = "Alpha figures, corrected"
    row_a.content = '{"title": "Alpha quarterly figures, corrected"}'
    await db_session.commit()

    await _rebuild_via(db_session, w, row_a, w.ctx_a, w.user_a, caller)

    shared = await _point(w, w.point)
    assert shared.payload["context_id"] == str(w.ctx_b.id)
    assert shared.payload["memory_id"] == str(row_b.id)
    assert _same_vector(_dense(shared), _vec(_doc_text(_BRAVO)))
    detached = await _one_row(db_session, w.ctx_a)
    assert detached.summary_embedding_id == row_a.id
    own = await _point(w, row_a.id)
    assert own is not None
    assert own.payload["context_id"] == str(w.ctx_a.id)
    assert own.payload["summary"] == "Alpha figures, corrected"
    assert _same_vector(_dense(own), _vec(_doc_text("Alpha quarterly figures, corrected")))


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("last_writer", ["a", "b"])
async def test_a_former_holder_migration_rebuild_writes_its_own_point(
    db_session, world, last_writer
):
    """The migration writes the row's own point in its target and in the
    collection the context still serves from (the one a rollback returns to)."""
    from services.embedding_migration_service import MigrationPlan, reembed_context

    w = world
    row_a, _row_b = await _shared_state(db_session, w, last_writer=last_writer)
    target_model = f"it-target-{uuid.uuid4().hex[:8]}"
    target = get_collection_name(target_model, _DIM)
    plan = MigrationPlan(
        context_id=w.ctx_a.id,
        workspace_id=w.ws_a.id,
        source_model="it-source",
        source_dimensions=_DIM,
        source_collection=w.collection,
        target_model=target_model,
        target_dimensions=_DIM,
        target_collection=target,
        memory_count=1,
        content_chunks_on=False,
        chunk_estimate=0,
    )
    try:
        result = await reembed_context(db_session, plan, embedding_service=_embedder())

        assert result.unrebuildable == []
        assert await _point(w, w.point, target) is None
        own = await _point(w, row_a.id, target)
        assert own is not None and own.payload["context_id"] == str(w.ctx_a.id)
        detached = await _one_row(db_session, w.ctx_a)
        assert detached.summary_embedding_id == row_a.id
        in_source = await _point(w, row_a.id)
        if last_writer == "a":
            # The source point still held A's document: copied under A's id.
            assert in_source is not None
            assert in_source.payload["context_id"] == str(w.ctx_a.id)
            assert _same_vector(_dense(in_source), _vec(_doc_text(_ALPHA)))
            assert detached.embedding_status == "success"
            assert await _recall_ids(db_session, w, w.ctx_a, w.user_a, w.ws_a, _ALPHA) == [row_a.id]
        else:
            # It held B's: A is queued for the embedding pipeline, which
            # writes the collection the context serves from.
            assert in_source is None
            assert detached.embedding_status == "pending"
            shared = await _point(w, w.point)
            assert shared.payload["context_id"] == str(w.ctx_b.id)
    finally:
        await w.client.delete_collection(target)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("last_writer", ["a", "b"])
async def test_the_holder_rebuild_detaches_the_other_rows_first(db_session, world, last_writer):
    from services.memory_service import write_memory_point

    w = world
    row_a, row_b = await _shared_state(db_session, w, last_writer=last_writer)
    await db_session.refresh(row_b)

    await write_memory_point(
        db_session,
        row_b,
        collection_name=w.collection,
        embedding_service=_embedder(),
        user_id=w.user_b,
        workspace_id=str(w.ws_b.id),
        context_id=str(w.ctx_b.id),
    )
    await db_session.commit()

    shared = await _point(w, w.point)
    assert shared.payload["context_id"] == str(w.ctx_b.id)
    assert shared.payload["memory_id"] == str(row_b.id)
    detached = await _one_row(db_session, w.ctx_a)
    assert detached.summary_embedding_id == row_a.id
    own = await _point(w, row_a.id)
    if last_writer == "a":
        # The point still held A's document: A keeps that vector under its own id.
        assert _same_vector(_dense(own), _vec(_doc_text(_ALPHA)))
        assert detached.embedding_status == "success"
    else:
        # The point held B's document: A is embedded again under its own id.
        assert own is None
        assert detached.embedding_status == "pending"


# --------------------------------------------------------------------------
# Deletes
# --------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_sweep_keeps_a_point_a_live_row_names_in_another_context(db_session, world):
    """The point carries the former holder's context, which is gone; B's row names it."""
    w = world
    row_a, row_b = await _shared_state(db_session, w, last_writer="a")
    row_a.deleted_at = utcnow() - timedelta(days=3)
    w.ctx_a.deleted_at = utcnow() - timedelta(days=3)
    await db_session.commit()

    from services.orphan_vector_sweep import _classify

    shared = await _point(w, w.point)
    assert await _classify(db_session, [_point_ref(shared)], utcnow() + timedelta(hours=1)) == {}


@pytest.mark.asyncio(loop_scope="session")
async def test_the_sweep_keeps_a_point_whose_payload_row_is_a_tombstone(db_session, world):
    w = world
    row_a, row_b = await _shared_state(db_session, w, last_writer="a")
    row_a.deleted_at = utcnow() - timedelta(days=3)
    await db_session.commit()

    from services.orphan_vector_sweep import _classify

    shared = await _point(w, w.point)
    assert await _classify(db_session, [_point_ref(shared)], utcnow() + timedelta(hours=1)) == {}


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("deleter", ["a", "b"])
async def test_a_whole_document_delete_keeps_the_point_another_row_names(
    db_session, world, deleter
):
    w = world
    row_a, row_b = await _shared_state(db_session, w, last_writer=deleter)
    ctx, resource, other = (
        (w.ctx_a, w.res_a, row_b) if deleter == "a" else (w.ctx_b, w.res_b, row_a)
    )
    await _event(db_session, resource, None, version=None, op="delete")

    metrics = await _index(db_session, w, ctx)

    assert metrics.errors == 0, metrics.reason
    assert metrics.applied_deletes == 1
    assert await _rows(db_session, ctx) == []
    assert await _point(w, w.point) is not None
    assert (
        await db_session.get(Memory, other.id, populate_existing=True)
    ).summary_embedding_id == w.point


@pytest.mark.asyncio(loop_scope="session")
async def test_a_whole_document_delete_drops_a_point_no_other_row_names(db_session, world):
    w = world
    await _event(db_session, w.res_a, _ALPHA)
    assert (await _index(db_session, w, w.ctx_a)).errors == 0
    await _event(db_session, w.res_a, None, version=None, op="delete")

    metrics = await _index(db_session, w, w.ctx_a)

    assert metrics.errors == 0, metrics.reason
    assert await _rows(db_session, w.ctx_a) == []
    assert await _point(w, w.point) is None


# --------------------------------------------------------------------------
# A claim whose event rolled back after its point write
# --------------------------------------------------------------------------


async def _rolled_back_claim(db, w: World) -> UUID:
    """A's row still names the point, which carries B's payload; B has no row.

    What a claim leaves when its event's SAVEPOINT rolls back after the point
    write: the detach of A's row is undone, the point write is not (A's own
    point from the detach stays as well). Returns A's row id.
    """
    from services.resource_indexer import detach_resource_rows

    row_a_id = (await _index_a_then_hand_over(db, w, "unpublish")).id
    await detach_resource_rows(db, w.point, holder_context_id=w.ctx_b.id)
    row_b = await _indexer_shaped_row(db, w.ctx_b, w.user_b, w, _BRAVO)
    await _write_shared_point(w, w.ctx_b, row_b, _BRAVO)
    await db.rollback()
    await w.reload(db)
    assert (await _one_row(db, w.ctx_a)).summary_embedding_id == w.point
    return row_a_id


@pytest.mark.asyncio(loop_scope="session")
async def test_a_metadata_edit_after_a_rolled_back_claim_detaches_the_row(db_session, world):
    from models.schemas import UpdateMemoryRequest

    w = world
    row_a_id = await _rolled_back_claim(db_session, w)

    await MemoryService(db_session).update_memory(
        UpdateMemoryRequest(memory_id=row_a_id, tags=["edited"]),
        user_id=w.user_a,
        current_context_id=w.ctx_a.id,
        current_workspace_id=w.ws_a.id,
    )
    await db_session.commit()

    shared = await _point(w, w.point)
    assert shared.payload["context_id"] == str(w.ctx_b.id)
    assert "edited" not in (shared.payload.get("tags") or [])
    detached = await _one_row(db_session, w.ctx_a)
    assert detached.summary_embedding_id == row_a_id
    own = await _point(w, row_a_id)
    assert own is not None and own.payload["tags"] == ["edited"]
    assert _same_vector(_dense(own), _vec(_doc_text(_ALPHA)))


@pytest.mark.asyncio(loop_scope="session")
async def test_forget_after_a_rolled_back_claim_keeps_the_holders_point(db_session, world):
    from models.schemas import ForgetRequest

    w = world
    row_a_id = await _rolled_back_claim(db_session, w)

    response = await MemoryService(db_session).forget(
        ForgetRequest(memory_id=row_a_id),
        user_id=w.user_a,
        current_context_id=w.ctx_a.id,
    )

    assert response.memory_ids == [row_a_id]
    shared = await _point(w, w.point)
    assert shared is not None and shared.payload["context_id"] == str(w.ctx_b.id)


# --------------------------------------------------------------------------
# The embedding pipeline end to end
# --------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_pipeline_embeds_a_former_holders_edit_under_its_own_id(db_session, world):
    from services.memory_service import process_pending_embedding

    w = world
    row_a, row_b = await _shared_state(db_session, w, last_writer="b")
    row_a_id = row_a.id
    row_a.summary = "Alpha figures, corrected"
    row_a.content = '{"title": "Alpha quarterly figures, corrected"}'
    row_a.embedding_status = "pending"
    await db_session.commit()

    async def one_session():
        yield db_session

    with (
        patch("db.base.get_db", one_session),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=(w.collection, _embedder())),
        ),
    ):
        await process_pending_embedding(row_a_id)

    detached = await _one_row(db_session, w.ctx_a)
    assert detached.summary_embedding_id == row_a_id
    assert detached.embedding_status == "success"
    own = await _point(w, row_a_id)
    assert _same_vector(_dense(own), _vec(_doc_text("Alpha quarterly figures, corrected")))
    shared = await _point(w, w.point)
    assert shared.payload["memory_id"] == str(row_b.id)
    assert _same_vector(_dense(shared), _vec(_doc_text(_BRAVO)))


# --------------------------------------------------------------------------
# The one-shot repair of points written before the rule
# --------------------------------------------------------------------------


async def _repair(db, w: World, *, dry_run: bool):
    from tasks.resource_point_holders import HolderRepair, run_holder_repair

    total = HolderRepair(dry_run=dry_run)
    with patch(
        "tasks.resource_point_holders.resolve_context_routing",
        AsyncMock(return_value=(w.collection, _embedder())),
    ):
        for ctx_id in [w.ctx_a.id, w.ctx_b.id]:
            part = await run_holder_repair(db, context_id=ctx_id, dry_run=dry_run)
            for name in (
                "contexts",
                "released_rows",
                "shared_points",
                "foreign_points",
                "missing_points",
                "detached",
                "rebuilt",
                "skipped",
                "failed",
            ):
                setattr(total, name, getattr(total, name) + getattr(part, name))
    # The plan ends its read transactions with a rollback.
    await w.reload(db)
    return total


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("last_writer", ["a", "b"])
async def test_the_repair_plans_applies_and_then_finds_nothing(db_session, world, last_writer):
    w = world
    row_a, row_b = await _shared_state(db_session, w, last_writer=last_writer)
    row_a_id, row_b_id = row_a.id, row_b.id

    plan = await _repair(db_session, w, dry_run=True)
    assert (plan.released_rows, plan.shared_points, plan.foreign_points) == (
        1,
        0 if last_writer == "a" else 1,
        1 if last_writer == "a" else 0,
    )
    assert (await _one_row(db_session, w.ctx_a)).summary_embedding_id == w.point

    applied = await _repair(db_session, w, dry_run=False)
    assert (applied.skipped, applied.failed) == (0, 0)
    assert applied.detached == 1
    assert applied.rebuilt == (1 if last_writer == "a" else 0)

    detached = await _one_row(db_session, w.ctx_a)
    assert detached.summary_embedding_id == row_a_id
    holder = await _one_row(db_session, w.ctx_b)
    assert holder.summary_embedding_id == w.point
    shared = await _point(w, w.point)
    assert shared.payload["context_id"] == str(w.ctx_b.id)
    assert shared.payload["memory_id"] == str(row_b_id)
    assert _same_vector(_dense(shared), _vec(_doc_text(_BRAVO)))
    if last_writer == "a":
        assert _same_vector(_dense(await _point(w, row_a_id)), _vec(_doc_text(_ALPHA)))
    else:
        assert detached.embedding_status == "pending"

    again = await _repair(db_session, w, dry_run=True)
    assert again.found == 0
    assert (await _repair(db_session, w, dry_run=False)).repaired == 0


# --------------------------------------------------------------------------
# The orphan sweep and a detach in flight; deletes of a released context
# --------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("writer", ["claim_detach", "rebuild"])
async def test_a_sweep_during_a_detach_keeps_the_rows_own_point(
    db_session, async_engine, world, monkeypatch, writer
):
    """Until the detach commits, the row still names the shared point, so its
    new own-id point looks like a stray to the sweep: the sweep's delete pass
    must wait for the writer instead of removing it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import services.orphan_vector_sweep as sweep
    from services.memory_service import write_memory_point
    from services.resource_indexer import detach_resource_rows

    w = world
    row_a = await _index_a_then_hand_over(db_session, w, "unpublish")
    row_a_id = row_a.id
    if writer == "claim_detach":
        assert await detach_resource_rows(db_session, w.point, holder_context_id=w.ctx_b.id) == 1
    else:
        await db_session.refresh(row_a)
        await write_memory_point(
            db_session,
            row_a,
            collection_name=w.collection,
            embedding_service=_embedder(),
            user_id=w.user_a,
            workspace_id=str(w.ws_a.id),
            context_id=str(w.ctx_a.id),
        )
    assert await _point(w, row_a_id) is not None

    monkeypatch.setattr(sweep, "POINT_WRITER_WAIT_SECONDS", 1.0)
    async with async_sessionmaker(async_engine, expire_on_commit=False)() as other:
        result = await sweep.sweep_orphan_points(
            other, dry_run=False, grace=timedelta(0), collections=[w.collection]
        )
    assert result.deleted == 0
    assert result.refused is not None
    await db_session.commit()

    assert (await _one_row(db_session, w.ctx_a)).summary_embedding_id == row_a_id
    assert await _point(w, row_a_id) is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_version_delete_in_a_released_context_drops_the_rows_own_point(db_session, world):
    w = world
    row_a_id = (await _index_a_then_hand_over(db_session, w, "unpublish")).id
    await _event(db_session, w.res_b, w.title_b)
    assert (await _index(db_session, w, w.ctx_b)).errors == 0
    assert await _point(w, row_a_id) is not None
    await _event(db_session, w.res_a, None, version=1, op="delete")

    metrics = await _index(db_session, w, w.ctx_a)

    assert metrics.errors == 0, metrics.reason
    assert metrics.applied_deletes == 1
    assert await _rows(db_session, w.ctx_a) == []
    assert await _point(w, row_a_id) is None
    shared = await _point(w, w.point)
    assert shared is not None and shared.payload["context_id"] == str(w.ctx_b.id)
