"""#1804: restoring a deleted context from its soft-deleted rows.

The restore commits, so every test commits its rows and removes them again.
The vector store is not involved: the restore only marks rows ``pending`` and
the embedding sweep rebuilds their points. The resource tests (#1870) run that
rebuild too, against a mocked vector store client.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from config.constants import MAX_EMBEDDING_RETRIES
from models.auth import AuditLog, Context, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from models.resource import Resource, ResourceEvent, ResourceSchema
from services.context_restore import (
    AUDIT_ACTION,
    DELETION_WINDOW,
    restore_deleted_context,
)
from services.context_service import ContextService
from services.memory_service import MemoryService, process_pending_embedding
from services.resource_indexer import ResourceIndexer, ResourceRebuildError
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException, OpenAIError, ValidationError

_USER = "restore_user"


class _Seed:
    def __init__(self, workspace_id: UUID):
        self.workspace_id = workspace_id
        self.context_ids: list[UUID] = []

    def context(self, **overrides) -> Context:
        fields = {
            "id": uuid4(),
            "workspace_id": self.workspace_id,
            "name": f"restore_{uuid4().hex[:8]}",
            "display_name": "Restore",
            "created_by": _USER,
            "is_private": False,
        }
        fields.update(overrides)
        self.context_ids.append(fields["id"])
        return Context(**fields)

    def memory(self, context: Context, **overrides) -> Memory:
        fields = {
            "id": uuid4(),
            "user_id": _USER,
            "workspace_id": self.workspace_id,
            "context_id": context.id,
            "summary": "a summary long enough",
            "content": "c",
            "type": "note",
            "client": "pytest",
            "embedding_status": "success",
        }
        fields.update(overrides)
        return Memory(**fields)


@pytest_asyncio.fixture(loop_scope="session")
async def seed(db_session):
    workspace = Workspace(id=uuid4(), name="Restore", owner_user_id=_USER)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=_USER, role=WorkspaceRole.OWNER)
    )
    await db_session.commit()
    s = _Seed(workspace.id)
    yield s
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.workspace_id == s.workspace_id))
    await db_session.execute(
        delete(AuditLog).where(
            AuditLog.resource.in_([f"context:{cid}" for cid in s.context_ids] or [""])
        )
    )
    await db_session.execute(delete(Context).where(Context.workspace_id == s.workspace_id))
    await db_session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.workspace_id == s.workspace_id)
    )
    await db_session.execute(delete(Workspace).where(Workspace.id == s.workspace_id))
    await db_session.commit()


async def _delete(db_session, context_id: UUID) -> None:
    with patch("db.qdrant.delete_context_points_everywhere", AsyncMock(return_value={})):
        await ContextService(db_session).delete_context(_USER, context_id)


async def _memories(db_session, context_id: UUID) -> dict[UUID, Memory]:
    db_session.expire_all()
    rows = await db_session.execute(select(Memory).where(Memory.context_id == context_id))
    return {m.id: m for m in rows.scalars()}


@pytest.mark.asyncio
async def test_restores_the_context_and_what_its_deletion_tombstoned(db_session, seed):
    context = seed.context()
    db_session.add(context)
    await db_session.flush()
    kept = seed.memory(context, embedding_status="failed", embedding_retry_count=3)
    forgotten = seed.memory(context, deleted_at=utcnow() - timedelta(days=2), deleted_by=_USER)
    archived = seed.memory(
        context, deleted_at=utcnow() - timedelta(seconds=5), deleted_by=DELETED_BY_SLEEP_MERGE
    )
    db_session.add_all([kept, forgotten, archived])
    await db_session.commit()
    ids = (context.id, kept.id, forgotten.id, archived.id)
    context_id, kept_id, forgotten_id, archived_id = ids

    await _delete(db_session, context_id)
    result = await restore_deleted_context(db_session, context_id, dry_run=False)

    assert result.memories_restored == 1
    assert result.memories_left_deleted == 2
    db_session.expire_all()
    restored = (
        await db_session.execute(select(Context).where(Context.id == context_id))
    ).scalar_one()
    assert restored.deleted_at is None
    assert restored.deleted_by is None
    rows = await _memories(db_session, context_id)
    assert rows[kept_id].deleted_at is None
    assert rows[kept_id].embedding_status == "pending"
    assert rows[kept_id].embedding_retry_count == 0
    assert rows[forgotten_id].deleted_at is not None
    assert rows[archived_id].deleted_at is not None


@pytest.mark.asyncio
async def test_delete_context_stamps_its_memories_with_the_context_timestamp(db_session, seed):
    context = seed.context()
    db_session.add(context)
    await db_session.flush()
    db_session.add_all([seed.memory(context), seed.memory(context)])
    await db_session.commit()
    context_id = context.id

    await _delete(db_session, context_id)

    db_session.expire_all()
    deleted = (
        await db_session.execute(select(Context).where(Context.id == context_id))
    ).scalar_one()
    deleted_at = deleted.deleted_at
    rows = await _memories(db_session, context_id)
    assert deleted_at is not None
    assert {m.deleted_at for m in rows.values()} == {deleted_at}


@pytest.mark.asyncio
async def test_deletion_from_before_the_shared_timestamp_is_restored(db_session, seed):
    """v0.88/v0.89 stamped each memory a little before the context."""
    at = utcnow() - timedelta(days=1)
    context = seed.context(deleted_at=at, deleted_by=_USER)
    db_session.add(context)
    await db_session.flush()
    by_deletion = seed.memory(context, deleted_at=at - timedelta(seconds=3), deleted_by=_USER)
    earlier = seed.memory(
        context, deleted_at=at - DELETION_WINDOW - timedelta(minutes=1), deleted_by=_USER
    )
    someone_else = seed.memory(context, deleted_at=at, deleted_by="another_user")
    db_session.add_all([by_deletion, earlier, someone_else])
    await db_session.commit()
    context_id, restored_id = context.id, by_deletion.id

    result = await restore_deleted_context(db_session, context_id, dry_run=False)

    assert result.memories_restored == 1
    rows = await _memories(db_session, context_id)
    assert [m.id for m in rows.values() if m.deleted_at is None] == [restored_id]


@pytest.mark.asyncio
async def test_memory_forgotten_just_before_a_shared_timestamp_deletion_stays_deleted(
    db_session, seed
):
    """From v0.90.0 the window is not used: only the context's own timestamp counts."""
    context = seed.context()
    db_session.add(context)
    await db_session.flush()
    kept = seed.memory(context)
    forgotten = seed.memory(context, deleted_at=utcnow() - timedelta(minutes=3), deleted_by=_USER)
    db_session.add_all([kept, forgotten])
    await db_session.commit()
    context_id, kept_id, forgotten_id = context.id, kept.id, forgotten.id

    await _delete(db_session, context_id)
    result = await restore_deleted_context(db_session, context_id, dry_run=False)

    assert (result.memories_restored, result.memories_left_deleted) == (1, 1)
    rows = await _memories(db_session, context_id)
    assert rows[kept_id].deleted_at is None
    assert rows[forgotten_id].deleted_at is not None


@pytest.mark.asyncio
async def test_the_window_is_flagged_when_no_memory_shares_the_timestamp(db_session, seed):
    """A context emptied by forget and then deleted looks like an old deletion."""
    context = seed.context()
    db_session.add(context)
    await db_session.flush()
    db_session.add(
        seed.memory(context, deleted_at=utcnow() - timedelta(minutes=2), deleted_by=_USER)
    )
    await db_session.commit()
    context_id = context.id
    await _delete(db_session, context_id)

    result = await restore_deleted_context(db_session, context_id)

    assert result.memories_restored == 1
    assert any("deletion time" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_no_window_warning_for_a_shared_timestamp_deletion(db_session, seed):
    context = seed.context()
    db_session.add(context)
    await db_session.flush()
    db_session.add(seed.memory(context))
    await db_session.commit()
    context_id = context.id
    await _delete(db_session, context_id)

    result = await restore_deleted_context(db_session, context_id)

    assert result.memories_restored == 1
    assert result.warnings == []


@pytest.mark.asyncio
async def test_dry_run_counts_and_changes_nothing(db_session, seed):
    context = seed.context()
    db_session.add(context)
    await db_session.flush()
    db_session.add_all([seed.memory(context), seed.memory(context)])
    await db_session.commit()
    context_id = context.id
    await _delete(db_session, context_id)

    result = await restore_deleted_context(db_session, context_id)

    assert result.dry_run is True
    assert result.memories_restored == 2
    assert not db_session.in_transaction()
    rows = await _memories(db_session, context_id)
    assert all(m.deleted_at is not None for m in rows.values())
    still = (await db_session.execute(select(Context).where(Context.id == context_id))).scalar_one()
    assert still.deleted_at is not None


@pytest.mark.asyncio
async def test_writes_an_audit_row(db_session, seed):
    context = seed.context()
    db_session.add(context)
    await db_session.commit()
    context_id = context.id
    await _delete(db_session, context_id)

    await restore_deleted_context(
        db_session, context_id, dry_run=False, actor_id="admin-1", actor_email="a@example.com"
    )

    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.resource == f"context:{context_id}")
        )
    ).scalar_one()
    assert audit.action == AUDIT_ACTION
    assert audit.user_id == "admin-1"


@pytest.mark.asyncio
async def test_warns_when_the_workspace_is_at_its_context_cap(db_session, seed):
    workspace = await db_session.get(Workspace, seed.workspace_id)
    workspace.plan_name = "free"
    context = seed.context(deleted_at=utcnow(), deleted_by=_USER)
    db_session.add(context)
    cap = workspace.effective_max_contexts
    db_session.add_all([seed.context() for _ in range(cap)])
    await db_session.commit()

    result = await restore_deleted_context(db_session, context.id)

    assert any("over the cap" in w for w in result.warnings)


class TestRefusals:
    @pytest.mark.asyncio
    async def test_unknown_context(self, db_session, seed):
        with pytest.raises(NotFoundException):
            await restore_deleted_context(db_session, uuid4())

    @pytest.mark.asyncio
    async def test_live_context(self, db_session, seed):
        context = seed.context()
        db_session.add(context)
        await db_session.commit()

        with pytest.raises(ConflictError, match="not deleted"):
            await restore_deleted_context(db_session, context.id)

    @pytest.mark.asyncio
    async def test_name_taken_by_a_live_context_then_restored_under_a_new_name(
        self, db_session, seed
    ):
        context = seed.context(name="restore_taken")
        db_session.add(context)
        await db_session.commit()
        context_id = context.id
        await _delete(db_session, context_id)
        db_session.add(seed.context(name="restore_taken"))
        await db_session.commit()

        with pytest.raises(ConflictError, match="already named"):
            await restore_deleted_context(db_session, context_id, dry_run=False)

        result = await restore_deleted_context(
            db_session, context_id, dry_run=False, new_name="restore_taken-2"
        )
        assert result.renamed_from == "restore_taken"
        db_session.expire_all()
        restored = (
            await db_session.execute(select(Context).where(Context.id == context_id))
        ).scalar_one()
        assert (restored.name, restored.deleted_at) == ("restore_taken-2", None)

    @pytest.mark.asyncio
    async def test_deleted_workspace(self, db_session, seed):
        context = seed.context(deleted_at=utcnow(), deleted_by=_USER)
        db_session.add(context)
        await db_session.flush()
        workspace = await db_session.get(Workspace, seed.workspace_id)
        workspace.deleted_at = utcnow()
        await db_session.commit()

        with pytest.raises(ConflictError, match="workspace"):
            await restore_deleted_context(db_session, context.id)

    @pytest.mark.asyncio
    async def test_resource_taken_by_a_live_context(self, db_session, seed):
        resource = f"res-{uuid4().hex[:8]}"
        context = seed.context(resource_id=resource, deleted_at=utcnow(), deleted_by=_USER)
        db_session.add(context)
        db_session.add(seed.context(resource_id=resource))
        await db_session.commit()

        with pytest.raises(ConflictError, match="resource"):
            await restore_deleted_context(db_session, context.id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["Has Caps", "default", "x" * 101, ""])
    async def test_invalid_new_name(self, db_session, seed, name):
        with pytest.raises(ValidationError):
            await restore_deleted_context(db_session, uuid4(), new_name=name)


# ---------------------------------------------------------------------------
# #1870: resource-ingested memories, and the scope of the counts
# ---------------------------------------------------------------------------

_SCHEMA_FIELDS = [
    {"name": "title", "classification": "public", "index_hint": "fulltext", "description": "Title"},
    {"name": "category", "classification": "public", "index_hint": "facet"},
    {"name": "price", "classification": "public", "index_hint": "sort"},
    {"name": "internal", "classification": "internal", "index_hint": "fulltext"},
]
_DOCS = {
    "doc_1": {"title": "Quarterly revenue report", "category": "finance", "price": 100},
    "doc_2": {"title": "Onboarding checklist", "category": "people", "price": 5, "internal": "x"},
}


async def _embed(text: str, *_args, **_kwargs) -> list[float]:
    """A vector that depends on the text embedded, to tell documents from labels."""
    return [float(len(text))] + [0.25] * 511


async def _index_resource(db_session, seed) -> tuple[Context, dict[str, object]]:
    """A published context whose documents went through the real indexer.

    Returns the context and the points the indexer wrote, by ``doc_id``.
    """
    slug = f"res-{uuid4().hex[:8]}"
    context = seed.context(resource_id=slug)
    resource = Resource(id=uuid4(), workspace_id=seed.workspace_id, resource_id=slug)
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

    client = AsyncMock()
    with patch("services.resource_indexer.get_qdrant_client", return_value=client):
        indexer = ResourceIndexer(db_session)
    service = AsyncMock()
    service.embed = AsyncMock(side_effect=_embed)
    for event in events:
        await indexer._apply_upsert(event, schema, context, "kagura_memories", service)
    await db_session.commit()
    indexed = {
        call.kwargs["points"][0].payload["doc_id"]: call.kwargs["points"][0]
        for call in client.upsert.await_args_list
    }
    assert set(indexed) == set(_DOCS)
    return context, indexed


async def _run_pending_embedding(
    db_session, memory_id: UUID, *, embed: AsyncMock | None = None
) -> tuple[AsyncMock, AsyncMock]:
    """Run the embedding sweep's worker on one row.

    Returns the resource indexer's vector store client and the generic
    ``add_memory_to_qdrant`` writer, both mocked. ``embed`` replaces the
    embedding call (default: ``_embed``).
    """

    async def _session():
        yield db_session

    client = AsyncMock()
    generic_writer = AsyncMock()
    with (
        patch("db.base.get_db", return_value=_session()),
        patch("services.resource_indexer.get_qdrant_client", return_value=client),
        patch("services.memory_service.add_memory_to_qdrant", generic_writer),
        patch(
            "services.embedding_service.EmbeddingService.embed",
            embed or AsyncMock(side_effect=_embed),
        ),
        patch("services.memory_service._create_knn_seed_edges", AsyncMock()),
        patch("services.memory_service._create_tag_cooccurrence_seed_edges", AsyncMock()),
    ):
        await process_pending_embedding(memory_id)
    return client, generic_writer


@pytest.mark.asyncio
async def test_restored_resource_rows_get_the_indexers_point_back(db_session, seed):
    context, indexed = await _index_resource(db_session, seed)
    context_id, slug = context.id, context.resource_id
    note = seed.memory(context, summary="a note the API wrote")
    db_session.add(note)
    await db_session.commit()
    note_id = note.id
    before = await _memories(db_session, context_id)
    by_doc = {m.resource_doc_id: m.id for m in before.values() if m.resource_doc_id}
    assert set(by_doc) == set(_DOCS)
    point_ids = {doc_id: before[memory_id].point_id for doc_id, memory_id in by_doc.items()}

    await _delete(db_session, context_id)
    plan = await restore_deleted_context(db_session, context_id)
    assert plan.dry_run is True
    assert plan.memories_restored == 3
    assert plan.warnings == []

    result = await restore_deleted_context(db_session, context_id, dry_run=False)
    assert result.memories_restored == 3
    assert result.warnings == []
    rows = await _memories(db_session, context_id)
    assert {m.embedding_status for m in rows.values()} == {"pending"}

    for doc_id, memory_id in by_doc.items():
        client, generic_writer = await _run_pending_embedding(db_session, memory_id)

        # Not the generic path: that embeds the label and writes under the row id.
        generic_writer.assert_not_awaited()
        assert client.upsert.await_count == 1
        rebuilt = client.upsert.await_args.kwargs["points"][0]
        original = indexed[doc_id]
        # The point the row names, with the document's vector and the
        # resource payload: what the indexer wrote before the deletion.
        assert rebuilt.id == str(point_ids[doc_id]) == original.id
        assert rebuilt.id != str(memory_id)
        assert rebuilt.vector == original.vector
        # ``indexed_at`` is each point's own write time (#1869): the rebuilt
        # point is written later than the original, everything else is equal.
        assert rebuilt.payload["indexed_at"] >= original.payload["indexed_at"]
        assert {k: v for k, v in rebuilt.payload.items() if k != "indexed_at"} == {
            k: v for k, v in original.payload.items() if k != "indexed_at"
        }
        assert rebuilt.payload["content"] == f"Title: {_DOCS[doc_id]['title']}"
        assert rebuilt.payload["facets"] == {"category": _DOCS[doc_id]["category"]}
        assert rebuilt.payload["sortable"] == {"price": _DOCS[doc_id]["price"]}
        assert rebuilt.payload["memory_id"] == str(memory_id)

    rows = await _memories(db_session, context_id)
    for doc_id, memory_id in by_doc.items():
        assert rows[memory_id].embedding_status == "success"
        assert rows[memory_id].point_id == point_ids[doc_id]
        assert rows[memory_id].summary == f"[{slug}] {doc_id} v2"

    # A memory the API wrote still goes the generic way, under its own id.
    client, generic_writer = await _run_pending_embedding(db_session, note_id)
    client.upsert.assert_not_awaited()
    assert generic_writer.await_args.kwargs["memory_id"] == note_id

    # Forgetting a restored resource memory removes the point that was rebuilt.
    forgotten = rows[by_doc["doc_1"]]
    with patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()) as delete_point:
        await MemoryService(db_session)._delete_memory_point(_USER, forgotten)
    assert delete_point.await_args.args[1] == point_ids["doc_1"]


@pytest.mark.asyncio
async def test_resource_row_that_cannot_be_rebuilt_fails_instead_of_a_label_vector(
    db_session, seed
):
    context, _ = await _index_resource(db_session, seed)
    context_id, slug = context.id, context.resource_id
    await _delete(db_session, context_id)
    # The resource loses its schema while the context is deleted.
    await db_session.execute(delete(ResourceSchema).where(ResourceSchema.resource_id == slug))
    await db_session.commit()
    await restore_deleted_context(db_session, context_id, dry_run=False)
    memory_id = next(iter(await _memories(db_session, context_id)))

    client, generic_writer = await _run_pending_embedding(db_session, memory_id)

    client.upsert.assert_not_awaited()
    generic_writer.assert_not_awaited()
    row = (await _memories(db_session, context_id))[memory_id]
    assert row.embedding_status == "failed"
    assert "ingest the document again" in (row.embedding_error or "")


@pytest.mark.asyncio
async def test_resource_row_is_rebuilt_from_what_it_holds_now(db_session, seed):
    context, indexed = await _index_resource(db_session, seed)
    context_id = context.id
    rows = await _memories(db_session, context_id)
    edited = next(m for m in rows.values() if m.resource_doc_id == "doc_1")
    not_json = next(m for m in rows.values() if m.resource_doc_id == "doc_2")
    edited_id, not_json_id = edited.id, not_json.id
    # Edited in place: one into another JSON document, one into plain text.
    edited.content = '{"title": "Annual revenue report", "category": "finance", "price": 7}'
    not_json.content = "free text, no longer a document"
    await db_session.commit()
    await _delete(db_session, context_id)
    await restore_deleted_context(db_session, context_id, dry_run=False)

    client, generic_writer = await _run_pending_embedding(db_session, edited_id)
    generic_writer.assert_not_awaited()
    rebuilt = client.upsert.await_args.kwargs["points"][0]
    assert rebuilt.id == indexed["doc_1"].id
    assert rebuilt.payload["content"] == "Title: Annual revenue report"
    assert rebuilt.payload["sortable"] == {"price": 7}

    client, generic_writer = await _run_pending_embedding(db_session, not_json_id)
    client.upsert.assert_not_awaited()
    generic_writer.assert_not_awaited()
    rows = await _memories(db_session, context_id)
    assert rows[edited_id].embedding_status == "success"
    assert rows[not_json_id].embedding_status == "failed"
    assert "does not hold a JSON document" in (rows[not_json_id].embedding_error or "")


@pytest.mark.asyncio
async def test_rebuild_point_refuses_a_row_that_owns_no_resource_point(db_session, seed):
    context = seed.context()
    db_session.add(context)
    await db_session.flush()
    # ``remember(external_id=...)`` sets details.resource_id on an API-written row.
    memory = seed.memory(context, details={"resource_id": "ext-1"})
    db_session.add(memory)
    await db_session.commit()

    with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
        indexer = ResourceIndexer(db_session)
    with pytest.raises(ResourceRebuildError):
        await indexer.rebuild_point(
            memory, collection_name="kagura_memories", embedding_service=AsyncMock()
        )


@pytest.mark.asyncio
async def test_counts_cover_the_rows_the_update_touches(db_session, seed):
    context = seed.context()
    other_workspace = Workspace(id=uuid4(), name="Restore other", owner_user_id=_USER)
    db_session.add_all([context, other_workspace])
    await db_session.flush()
    forgotten = seed.memory(context, deleted_at=utcnow() - timedelta(days=2), deleted_by=_USER)
    # A row that names the context from another workspace is outside the
    # restore's scope: neither restored nor counted as left deleted.
    stray = seed.memory(
        context,
        workspace_id=other_workspace.id,
        deleted_at=utcnow() - timedelta(days=2),
        deleted_by=_USER,
    )
    db_session.add_all([seed.memory(context), seed.memory(context), forgotten, stray])
    await db_session.commit()
    context_id, other_workspace_id, stray_id = context.id, other_workspace.id, stray.id
    try:
        await _delete(db_session, context_id)

        plan = await restore_deleted_context(db_session, context_id)
        result = await restore_deleted_context(db_session, context_id, dry_run=False)

        rows = await _memories(db_session, context_id)
        live = [m for m in rows.values() if m.deleted_at is None]
        in_scope = [m for m in rows.values() if m.workspace_id == seed.workspace_id]
        assert plan.memories_restored == result.memories_restored == len(live) == 2
        assert all(m.embedding_status == "pending" for m in live)
        assert plan.memories_left_deleted == result.memories_left_deleted == 1
        assert result.memories_restored + result.memories_left_deleted == len(in_scope)
        assert rows[stray_id].deleted_at is not None
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Memory).where(Memory.id == stray_id))
        await db_session.execute(delete(Workspace).where(Workspace.id == other_workspace_id))
        await db_session.commit()


# ---------------------------------------------------------------------------
# #1897: a row that cannot be rebuilt is final at once, and the restore says so
# ---------------------------------------------------------------------------


def _events(logger: MagicMock, level: str) -> list[str]:
    return [call.args[0] for call in getattr(logger, level).call_args_list]


async def _restore_without_schema(db_session, seed) -> tuple[UUID, str, dict[str, UUID]]:
    """A restored resource context whose resource lost its schema meanwhile.

    Returns the context id, the resource slug and the memory ids by ``doc_id``.
    """
    context, _ = await _index_resource(db_session, seed)
    context_id, slug = context.id, context.resource_id
    await _delete(db_session, context_id)
    await db_session.execute(delete(ResourceSchema).where(ResourceSchema.resource_id == slug))
    await db_session.commit()
    await restore_deleted_context(db_session, context_id, dry_run=False)
    rows = await _memories(db_session, context_id)
    return context_id, slug, {m.resource_doc_id: m.id for m in rows.values()}


@pytest.mark.asyncio
async def test_unrebuildable_resource_row_is_final_after_one_attempt(db_session, seed):
    context_id, _, by_doc = await _restore_without_schema(db_session, seed)
    memory_id = by_doc["doc_1"]

    with patch("services.memory_service.logger") as log:
        await _run_pending_embedding(db_session, memory_id)

    row = (await _memories(db_session, context_id))[memory_id]
    assert row.embedding_status == "failed"
    assert "has no schema" in (row.embedding_error or "")
    # Out of the sweep's reach (it claims `failed` rows below the ceiling).
    assert row.embedding_retry_count == MAX_EMBEDDING_RETRIES
    attempted_at = row.embedding_attempted_at
    # Its own event, at warning level; not the provider-kept-failing one.
    assert "embedding_resource_unrebuildable" in _events(log, "warning")
    assert "embedding_failed" not in _events(log, "warning")
    assert "embedding_budget_exhausted" not in _events(log, "error")
    unrebuildable = next(
        call
        for call in log.warning.call_args_list
        if call.args[0] == "embedding_resource_unrebuildable"
    )
    assert unrebuildable.kwargs["memory_id"] == str(memory_id)
    assert unrebuildable.kwargs["error_class"] == "ResourceRebuildError"

    # No second attempt: the claim does not take the row again.
    embed = AsyncMock(side_effect=_embed)
    with patch("services.memory_service.logger") as log:
        await _run_pending_embedding(db_session, memory_id, embed=embed)
    embed.assert_not_awaited()
    assert _events(log, "warning") == [] and _events(log, "error") == []
    row = (await _memories(db_session, context_id))[memory_id]
    assert row.embedding_status == "failed"
    assert row.embedding_attempted_at == attempted_at


@pytest.mark.asyncio
async def test_row_whose_content_is_no_document_is_final_after_one_attempt(db_session, seed):
    context, _ = await _index_resource(db_session, seed)
    context_id = context.id
    rows = await _memories(db_session, context_id)
    not_json = next(m for m in rows.values() if m.resource_doc_id == "doc_2")
    not_json_id = not_json.id
    not_json.content = "free text, no longer a document"
    not_json.embedding_status = "pending"
    await db_session.commit()

    with patch("services.memory_service.logger") as log:
        await _run_pending_embedding(db_session, not_json_id)

    row = (await _memories(db_session, context_id))[not_json_id]
    assert row.embedding_status == "failed"
    assert row.embedding_retry_count == MAX_EMBEDDING_RETRIES
    assert "embedding_resource_unrebuildable" in _events(log, "warning")
    assert _events(log, "error") == []


@pytest.mark.asyncio
async def test_admin_retry_rebuilds_the_row_once_the_schema_is_back(db_session, seed):
    from api.routes.admin import retry_failed_embeddings

    context_id, slug, by_doc = await _restore_without_schema(db_session, seed)
    memory_id = by_doc["doc_1"]
    await _run_pending_embedding(db_session, memory_id)
    assert (await _memories(db_session, context_id))[memory_id].embedding_status == "failed"

    # The operator publishes the schema again and retries the failed rows.
    resource_pk = (
        await db_session.execute(select(Resource.id).where(Resource.resource_id == slug))
    ).scalar_one()
    db_session.add(
        ResourceSchema(
            resource_pk=resource_pk,
            resource_id=slug,
            schema_version=2,
            field_definitions=_SCHEMA_FIELDS,
        )
    )
    await db_session.commit()
    response = await retry_failed_embeddings(
        user={"user_id": _USER}, db=db_session, context_id=str(context_id), workspace_id=None
    )
    assert response["reset_count"] == 1
    row = (await _memories(db_session, context_id))[memory_id]
    assert row.embedding_status == "pending"
    assert row.embedding_retry_count == 0
    assert row.embedding_error is None

    client, generic_writer = await _run_pending_embedding(db_session, memory_id)

    generic_writer.assert_not_awaited()
    assert client.upsert.await_count == 1
    rebuilt = client.upsert.await_args.kwargs["points"][0]
    row = (await _memories(db_session, context_id))[memory_id]
    assert rebuilt.id == str(row.point_id)
    assert rebuilt.payload["content"] == f"Title: {_DOCS['doc_1']['title']}"
    assert row.embedding_status == "success"
    assert row.embedding_retry_count == 0
    assert row.embedding_error is None


@pytest.mark.asyncio
async def test_transient_failure_of_a_resource_row_keeps_its_retry_budget(db_session, seed):
    context, _ = await _index_resource(db_session, seed)
    context_id = context.id
    await _delete(db_session, context_id)
    await restore_deleted_context(db_session, context_id, dry_run=False)
    memory_id = next(iter(await _memories(db_session, context_id)))

    provider_down = AsyncMock(side_effect=OpenAIError("connection reset"))
    with patch("services.memory_service.logger") as log:
        await _run_pending_embedding(db_session, memory_id, embed=provider_down)

    row = (await _memories(db_session, context_id))[memory_id]
    assert row.embedding_status == "failed"
    # The first attempt of a pending row spends nothing; the sweep retries it.
    assert row.embedding_retry_count == 0
    assert _events(log, "warning") == ["embedding_failed"]
    assert _events(log, "error") == []


@pytest.mark.asyncio
async def test_restore_warns_of_resource_rows_whose_resource_has_no_schema(db_session, seed):
    context, _ = await _index_resource(db_session, seed)
    context_id, slug = context.id, context.resource_id
    # A memory the API wrote is embedded from its summary and needs no schema,
    # also when ``remember(external_id=...)`` put a resource_id in its details.
    db_session.add_all(
        [
            seed.memory(context, summary="a note the API wrote"),
            seed.memory(context, details={"resource_id": slug, "doc_id": "doc_9", "version": 1}),
        ]
    )
    await db_session.commit()
    await _delete(db_session, context_id)
    await db_session.execute(delete(ResourceSchema).where(ResourceSchema.resource_id == slug))
    await db_session.commit()

    plan = await restore_deleted_context(db_session, context_id)

    assert plan.dry_run is True
    assert plan.memories_restored == 4
    assert len(plan.warnings) == 1
    warning = plan.warnings[0]
    assert warning.startswith("2 resource-ingested memories")
    assert slug in warning
    # The two ways out.
    assert "retry" in warning
    assert "newer version" in warning

    result = await restore_deleted_context(db_session, context_id, dry_run=False)
    assert result.warnings == plan.warnings
