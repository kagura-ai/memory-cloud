"""Storage scope filtering intersects access permissions before pagination."""

from datetime import timedelta
from uuid import uuid4

import pytest

from models.file_objects import FileObject
from services.file_storage_service import FileStorageService
from utils.datetime import utcnow

from ._admin_helpers import make_context, make_user, make_workspace


@pytest.mark.asyncio
async def test_shared_and_context_scope_filters_never_widen_access(db_session):
    owner = make_user()
    db_session.add(owner)
    await db_session.flush()
    workspace = make_workspace(owner_user_id=owner.user_id)
    other = make_workspace(owner_user_id=owner.user_id)
    db_session.add_all([workspace, other])
    await db_session.flush()
    allowed = make_context(workspace_id=workspace.id, created_by=owner.user_id, is_private=True)
    hidden = make_context(workspace_id=workspace.id, created_by=owner.user_id, is_private=True)
    foreign = make_context(workspace_id=other.id, created_by=owner.user_id)
    db_session.add_all([allowed, hidden, foreign])
    await db_session.flush()
    rows = []
    for index, (ws, context) in enumerate(
        [
            (workspace, allowed),
            (workspace, None),
            (workspace, hidden),
            (other, foreign),
        ]
    ):
        row = FileObject(
            workspace_id=ws.id,
            context_id=context.id if context else None,
            sha256=uuid4().hex + uuid4().hex,
            size_bytes=100,
            content_type="text/plain",
            filename=f"scope-{index}.txt",
            storage_backend="r2",
            storage_key=uuid4().hex,
            status="uploaded",
            created_by=owner.user_id,
            created_at=utcnow() + timedelta(seconds=index),
            uploaded_at=utcnow(),
        )
        rows.append(row)
    db_session.add_all(rows)
    await db_session.flush()
    service = FileStorageService(db_session)
    common = {"workspace_id": workspace.id, "accessible_context_ids": [allowed.id]}
    shared = await service.list_files(**common, workspace_only=True, limit=1)
    assert [row.id for row in shared] == [rows[1].id]
    scoped = await service.list_files(**common, context_id=allowed.id, limit=1)
    assert [row.id for row in scoped] == [rows[0].id]
    assert await service.list_files(**common, context_id=hidden.id) == []
    assert await service.list_files(**common, context_id=foreign.id) == []
    assert (
        await service.list_files(
            workspace_id=workspace.id, accessible_context_ids=[], context_id=allowed.id
        )
        == []
    )
    default = await service.list_files(**common)
    assert {row.id for row in default} == {rows[0].id, rows[1].id}
