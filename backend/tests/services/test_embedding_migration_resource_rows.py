"""#1896: the embedding-model migration on a context with resource-ingested rows.

A row the resource indexer wrote owns the indexer's point: the document's
uuid5 id (``Memory.point_id``), embedded from the document text, with the
resource payload. Its summary is only the label ``[resource] doc vN``. These
tests run the real indexer and the real migration against Postgres and an
in-memory stand-in for the vector store, through re-embed, verify and switch,
on a context that also holds a memory the API wrote.

The migration commits, so every test commits its rows and removes them again.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory
from models.resource import Resource, ResourceEvent, ResourceSchema
from services import embedding_migration_service as svc
from services.context_routing import resolve_context_embedding
from services.resource_indexer import ResourceIndexer
from utils.datetime import utcnow

_USER = "migration_user"
_SOURCE = "kagura_memories"
_TARGET_MODEL = "qwen3-embedding:4b"

_SCHEMA_FIELDS = [
    {"name": "title", "classification": "public", "index_hint": "fulltext", "description": "Title"},
    {"name": "category", "classification": "public", "index_hint": "facet"},
    {"name": "price", "classification": "public", "index_hint": "sort"},
]
_DOCS = {
    "doc_1": {"title": "Quarterly revenue report", "category": "finance", "price": 100},
    "doc_2": {"title": "Onboarding checklist", "category": "people", "price": 5},
}


def _vector(text: str) -> list[float]:
    """A vector that depends on the text embedded, to tell documents from labels."""
    return [float(len(text)), 0.25]


class _Embedder:
    """The target model's embedding service; records every text it embeds."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, text: str, *_args: Any, **_kwargs: Any) -> list[float]:
        self.texts.append(text)
        return _vector(text)

    async def embed_batch(self, texts: list[str], *_args: Any, **_kwargs: Any) -> list[list[float]]:
        self.texts.extend(texts)
        return [_vector(text) for text in texts]


class _VectorStore:
    """Points by collection and id: what the migration reads and writes."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = defaultdict(dict)

    # -- the client surface ResourceIndexer and verify use
    async def upsert(self, *, collection_name: str, points: list[Any], **_kwargs: Any) -> None:
        for point in points:
            self.collections[collection_name][str(point.id)] = point

    async def retrieve(self, *, collection_name: str, ids: list[str], **_kwargs: Any) -> list[Any]:
        stored = self.collections[collection_name]
        return [stored[point_id] for point_id in ids if point_id in stored]

    # -- the db.qdrant helpers the migration calls
    async def add_memory(self, *, memory_id: UUID, vector: list[float], payload: dict, **kwargs):
        self.collections[kwargs["collection_name"]][str(memory_id)] = SimpleNamespace(
            id=str(memory_id), vector=vector, payload=payload
        )

    async def list_ids(self, workspace_id: str, context_id: str, collection_name: str) -> list[str]:
        return list(self.collections[collection_name])

    async def delete(self, point_ids: list[str], collection_name: str) -> None:
        for point_id in point_ids:
            self.collections[collection_name].pop(point_id, None)

    def patched(self):
        """Every seam of the migration service and the indexer, on this store."""
        return (
            patch.object(svc, "get_qdrant_client", return_value=self),
            patch("services.resource_indexer.get_qdrant_client", return_value=self),
            patch.object(svc, "add_memory_to_qdrant", AsyncMock(side_effect=self.add_memory)),
            patch.object(svc, "list_context_point_ids", AsyncMock(side_effect=self.list_ids)),
            patch.object(svc, "delete_points_from_qdrant", AsyncMock(side_effect=self.delete)),
            patch.object(svc, "ensure_kagura_memories_collection", AsyncMock()),
        )


class _Seed:
    def __init__(self, workspace_id: UUID):
        self.workspace_id = workspace_id
        self.resource_pks: list[UUID] = []


@pytest_asyncio.fixture(loop_scope="session")
async def seed(db_session):
    workspace = Workspace(id=uuid4(), name="Migration", owner_user_id=_USER)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=_USER, role=WorkspaceRole.OWNER)
    )
    await db_session.commit()
    s = _Seed(workspace.id)
    yield s
    await db_session.rollback()
    contexts = select(Context.id).where(Context.workspace_id == s.workspace_id)
    await db_session.execute(delete(Memory).where(Memory.workspace_id == s.workspace_id))
    await db_session.execute(
        delete(ContextSearchConfig).where(ContextSearchConfig.context_id.in_(contexts))
    )
    for model in (ResourceEvent, ResourceSchema):
        await db_session.execute(delete(model).where(model.resource_pk.in_(s.resource_pks or [])))
    await db_session.execute(delete(Context).where(Context.workspace_id == s.workspace_id))
    await db_session.execute(delete(Resource).where(Resource.workspace_id == s.workspace_id))
    await db_session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.workspace_id == s.workspace_id)
    )
    await db_session.execute(delete(Workspace).where(Workspace.id == s.workspace_id))
    await db_session.commit()


@pytest.fixture
def store():
    vector_store = _VectorStore()
    patches = vector_store.patched()
    for p in patches:
        p.start()
    yield vector_store
    for p in reversed(patches):
        p.stop()


async def _mixed_context(db_session, seed, store) -> tuple[UUID, str, UUID, dict[str, UUID]]:
    """A published context: two documents through the real indexer (their
    points in the source collection) and one memory the API wrote.

    Returns the context id, the resource slug, the API-written memory's id and
    the resource rows' ids by ``doc_id``.
    """
    slug = f"res-{uuid4().hex[:8]}"
    context = Context(
        id=uuid4(),
        workspace_id=seed.workspace_id,
        name=f"migration_{uuid4().hex[:8]}",
        display_name="Migration",
        created_by=_USER,
        is_private=False,
        resource_id=slug,
    )
    resource = Resource(id=uuid4(), workspace_id=seed.workspace_id, resource_id=slug)
    seed.resource_pks.append(resource.id)
    db_session.add_all([context, resource])
    await db_session.flush()
    schema = ResourceSchema(
        resource_pk=resource.id,
        resource_id=slug,
        schema_version=1,
        field_definitions=_SCHEMA_FIELDS,
    )
    events = [
        ResourceEvent(
            resource_pk=resource.id, resource_id=slug, op="upsert", doc_id=doc_id, version=2,
            payload=payload, created_at=utcnow() - timedelta(days=3),
        )
        for doc_id, payload in _DOCS.items()
    ]  # fmt: skip
    db_session.add_all([schema, *events])
    await db_session.flush()

    indexer = ResourceIndexer(db_session)
    for event in events:
        await indexer._apply_upsert(event, schema, context, _SOURCE, _Embedder())

    note_id = uuid4()
    db_session.add(
        Memory(
            id=note_id,
            user_id=_USER,
            workspace_id=seed.workspace_id,
            context_id=context.id,
            summary="a note the API wrote",
            content="c",
            type="note",
            client="pytest",
            embedding_status="success",
            summary_embedding_id=note_id,
        )
    )
    await db_session.commit()

    context_id = context.id
    rows = await _live_rows(db_session, context_id)
    by_doc = {m.resource_doc_id: m.id for m in rows.values() if m.resource_doc_id}
    assert set(by_doc) == set(_DOCS)
    return context_id, slug, note_id, by_doc


async def _live_rows(db_session, context_id: UUID) -> dict[UUID, Memory]:
    db_session.expire_all()
    rows = await db_session.execute(
        select(Memory).where(Memory.context_id == context_id, Memory.deleted_at.is_(None))
    )
    return {m.id: m for m in rows.scalars()}


@pytest.mark.asyncio
async def test_mixed_context_through_reembed_verify_and_switch(db_session, seed, store):
    context_id, slug, note_id, by_doc = await _mixed_context(db_session, seed, store)
    rows = await _live_rows(db_session, context_id)
    point_ids = {doc_id: rows[memory_id].point_id for doc_id, memory_id in by_doc.items()}
    source_points = dict(store.collections[_SOURCE])
    assert set(source_points) == {str(p) for p in point_ids.values()}

    plan = await svc.plan_context_migration(db_session, context_id, _TARGET_MODEL)
    assert plan.source_collection == _SOURCE
    assert plan.memory_count == 3
    target = store.collections[plan.target_collection]

    # Nothing copied yet: every row is missing, the resource rows among them
    # (they CAN be rebuilt, so they are not "unrebuildable").
    before = await svc.verify_context_migration(db_session, plan)
    assert before.missing == sorted([note_id, *by_doc.values()])
    assert before.unrebuildable == []

    embedder = _Embedder()
    result = await svc.reembed_context(db_session, plan, embedding_service=embedder)

    assert result.embedded == 3
    assert result.unrebuildable == []
    # Each resource row: a point under Memory.point_id, built from the
    # document text with the resource payload, in the target collection.
    assert set(target) == {str(note_id), *(str(p) for p in point_ids.values())}
    for doc_id, memory_id in by_doc.items():
        point = target[str(point_ids[doc_id])]
        document_text = f"Title: {_DOCS[doc_id]['title']}"
        assert point.vector["dense"] == _vector(document_text)
        assert point.payload["content"] == document_text
        assert point.payload["facets"] == {"category": _DOCS[doc_id]["category"]}
        assert point.payload["sortable"] == {"price": _DOCS[doc_id]["price"]}
        assert point.payload["doc_id"] == doc_id
        assert point.payload["version"] == 2
        assert point.payload["memory_id"] == str(memory_id)
        # The indexed point, but for the model and the write time.
        original = source_points[str(point_ids[doc_id])]
        assert {k: v for k, v in point.payload.items() if k != "indexed_at"} == {
            k: v for k, v in original.payload.items() if k != "indexed_at"
        }
        # No label vector under the row id.
        assert str(memory_id) not in target
    # The labels were never embedded; the API-written memory still is, by summary.
    assert not any(text.startswith(f"[{slug}]") for text in embedder.texts)
    assert target[str(note_id)].vector == _vector("a note the API wrote")
    # Routing and the source collection are untouched.
    assert await resolve_context_embedding(db_session, context_id) == (plan.source_model, 512)
    assert store.collections[_SOURCE] == source_points

    # verify: the resource rows are present and keep their points; a point
    # with no live row is still removed.
    leftover = str(uuid4())
    target[leftover] = SimpleNamespace(id=leftover)
    verified = await svc.verify_context_migration(db_session, plan)
    assert (verified.expected, verified.present) == (3, 3)
    assert verified.ok and verified.missing == [] and verified.unrebuildable == []
    assert verified.stale_removed == 1
    assert set(target) == {str(note_id), *(str(p) for p in point_ids.values())}

    # A document forgotten between verify and switch: its point leaves the new
    # collection under the document's point id.
    forgotten = (await _live_rows(db_session, context_id))[by_doc["doc_1"]]
    forgotten.deleted_at = utcnow()
    forgotten.deleted_by = _USER
    await db_session.commit()

    switched = await svc.switch_context_embedding(
        db_session,
        context_id,
        plan.target_model,
        plan.target_dimensions,
        requeue_since=result.started_at,
    )

    assert switched.stale_removed == 1
    assert set(target) == {str(note_id), str(point_ids["doc_2"])}
    assert await resolve_context_embedding(db_session, context_id) == (
        plan.target_model,
        plan.target_dimensions,
    )
    # The new routing verifies clean: nothing missing, nothing removed.
    after = await svc.verify_context_migration(db_session, plan)
    assert after.ok and (after.expected, after.present, after.stale_removed) == (2, 2, 0)


@pytest.mark.asyncio
async def test_resource_row_that_no_longer_holds_a_document_is_skipped(db_session, seed, store):
    context_id, _slug, note_id, by_doc = await _mixed_context(db_session, seed, store)
    rows = await _live_rows(db_session, context_id)
    doc_1_point = rows[by_doc["doc_1"]].point_id
    not_json_id = by_doc["doc_2"]
    not_json_point = rows[not_json_id].point_id
    rows[not_json_id].content = "free text, no longer a document"
    await db_session.commit()

    plan = await svc.plan_context_migration(db_session, context_id, _TARGET_MODEL)
    embedder = _Embedder()
    seen: list[tuple[int, int]] = []
    result = await svc.reembed_context(
        db_session, plan, embedding_service=embedder, progress=lambda d, t: seen.append((d, t))
    )

    # The migration went on, and reports the row it could not rebuild.
    assert result.embedded == 2
    assert result.unrebuildable == [not_json_id]
    assert seen[-1] == (3, 3)
    target = store.collections[plan.target_collection]
    # No point for it: neither the document's nor a label vector under the row id.
    assert set(target) == {str(note_id), str(doc_1_point)}
    assert str(not_json_point) not in target and str(not_json_id) not in target

    texts_before_verify = list(embedder.texts)
    verified = await svc.verify_context_migration(db_session, plan)

    assert (verified.expected, verified.present) == (3, 2)
    assert verified.unrebuildable == [not_json_id]
    # It does not block the switch, and verify embedded and wrote nothing.
    assert verified.ok and verified.missing == []
    assert embedder.texts == texts_before_verify
    assert set(target) == {str(note_id), str(doc_1_point)}


@pytest.mark.asyncio
async def test_a_point_from_an_earlier_migration_does_not_hide_an_unrebuildable_row(
    db_session, seed, store
):
    # A -> B, back to A, A -> B again: the target already holds the points.
    context_id, _slug, note_id, by_doc = await _mixed_context(db_session, seed, store)
    plan = await svc.plan_context_migration(db_session, context_id, _TARGET_MODEL)
    await svc.reembed_context(db_session, plan, embedding_service=_Embedder())
    target = store.collections[plan.target_collection]
    rows = await _live_rows(db_session, context_id)
    edited_id = by_doc["doc_2"]
    edited_point = str(rows[edited_id].point_id)
    kept_point = str(rows[by_doc["doc_1"]].point_id)
    assert edited_point in target

    rows[edited_id].content = "free text, no longer a document"
    await db_session.commit()
    result = await svc.reembed_context(db_session, plan, embedding_service=_Embedder())

    # The old point was not built from what the row holds now: it goes, and
    # verify reports the row instead of counting that point as present.
    assert result.unrebuildable == [edited_id]
    assert set(target) == {str(note_id), kept_point}
    verified = await svc.verify_context_migration(db_session, plan)
    assert verified.unrebuildable == [edited_id]
    assert verified.ok and (verified.expected, verified.present) == (3, 2)


@pytest.mark.asyncio
async def test_resource_rows_of_a_resource_without_a_schema_are_skipped(db_session, seed, store):
    context_id, slug, note_id, by_doc = await _mixed_context(db_session, seed, store)
    await db_session.execute(delete(ResourceSchema).where(ResourceSchema.resource_id == slug))
    await db_session.commit()

    plan = await svc.plan_context_migration(db_session, context_id, _TARGET_MODEL)
    result = await svc.reembed_context(db_session, plan, embedding_service=_Embedder())

    assert result.embedded == 1
    assert sorted(result.unrebuildable) == sorted(by_doc.values())
    assert set(store.collections[plan.target_collection]) == {str(note_id)}

    verified = await svc.verify_context_migration(db_session, plan)
    assert verified.ok
    assert sorted(verified.unrebuildable) == sorted(by_doc.values())
    assert (verified.expected, verified.present) == (3, 1)
