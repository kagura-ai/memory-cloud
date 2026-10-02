"""#1804: restoring a deleted context from its soft-deleted rows.

The restore commits, so every test commits its rows and removes them again.
The vector store is not involved: the restore only marks rows ``pending`` and
the embedding sweep rebuilds their points.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from models.auth import AuditLog, Context, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from services.context_restore import (
    AUDIT_ACTION,
    DELETION_WINDOW,
    restore_deleted_context,
)
from services.context_service import ContextService
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException, ValidationError

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
