"""Integration tests for context-scoped file access control (Issue #1136).

Exercises ``FileStorageService`` against a REAL Postgres test database with the
REAL ``PermissionService``, so the per-role × per-context access matrix is
verified end-to-end (not just the wiring). Blob storage / Redis quota are mocked
only where a *permitted* operation would otherwise call out to R2 / Redis — every
*denied* path raises before reaching them.

Matrix under test:
- a file bound to a PRIVATE context is readable only by its creator (even a
  workspace owner/admin is denied — private = creator-only, #165);
- a file bound to a SHARED context follows the context ACL (a workspace viewer
  may read it, a member whose ``allowed_context_ids`` excludes it may not, #234);
- a NULL-context (workspace-scoped) file keeps the legacy workspace-role gate;
- reserve requires WRITE access to the target context, and the context must live
  in the upload's workspace;
- list returns NULL-context files + files in the caller's accessible contexts,
  and hides files scoped to contexts the caller cannot see.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, Workspace, WorkspaceMember
from models.file_objects import FileObject
from services.file_storage_service import FileStorageService
from tests.storage._fakes import FakeBlobStorage
from utils.exceptions import AuthorizationError, NotFoundException, ValidationError

OWNER = "owner-1136"
MEMBER = "member-1136"  # creator of the private context
VIEWER = "viewer-1136"
OTHER = "other-1136"  # a member with no access to either context


def _file(
    *,
    workspace_id: uuid.UUID,
    context_id: uuid.UUID | None,
    sha: str,
    status: str = "uploaded",
) -> FileObject:
    return FileObject(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        context_id=context_id,
        sha256=sha,
        size_bytes=1024,
        content_type="application/octet-stream",
        filename="f.bin",
        storage_backend="r2",
        storage_key=f"{workspace_id}/{sha[:2]}/{sha}",
        status=status,
        created_by=OWNER,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def scenario(db_session: AsyncSession):
    """Workspace + owner/member/viewer/other + a private and a shared context."""
    ws_id = uuid.uuid4()
    db_session.add(Workspace(id=ws_id, name=f"acl-{ws_id}", owner_user_id=OWNER))
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws_id, user_id=OWNER, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws_id, user_id=MEMBER, role=WorkspaceRole.MEMBER),
            WorkspaceMember(workspace_id=ws_id, user_id=VIEWER, role=WorkspaceRole.VIEWER),
            WorkspaceMember(workspace_id=ws_id, user_id=OTHER, role=WorkspaceRole.MEMBER),
        ]
    )
    private_ctx = Context(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        name="private",
        created_by=MEMBER,
        is_private=True,
    )
    shared_ctx = Context(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        name="shared",
        created_by=OWNER,
        is_private=False,
    )
    db_session.add_all([private_ctx, shared_ctx])
    # OTHER is a member but is scoped (allowed_context_ids = []) → no context access.
    # MEMBER (a workspace member, not owner/admin) is "suspended" by default
    # (allowed_context_ids = NULL → get_accessible_contexts returns nothing), so
    # whitelist both contexts for them to exercise the creator-sees-their-private
    # listing path (the privacy filter still keeps the private one creator-only).
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE workspace_members SET allowed_context_ids = '{}' "
            "WHERE workspace_id = :w AND user_id = :u"
        ),
        {"w": str(ws_id), "u": OTHER},
    )
    await db_session.execute(
        text(
            "UPDATE workspace_members SET allowed_context_ids = ARRAY[:c1, :c2]::uuid[] "
            "WHERE workspace_id = :w AND user_id = :u"
        ),
        {"c1": str(private_ctx.id), "c2": str(shared_ctx.id), "w": str(ws_id), "u": MEMBER},
    )
    svc = FileStorageService(db_session, storage=FakeBlobStorage())
    yield {
        "ws": ws_id,
        "private": private_ctx.id,
        "shared": shared_ctx.id,
        "svc": svc,
        "db": db_session,
    }
    await db_session.rollback()
    await db_session.execute(
        text("DELETE FROM file_objects WHERE workspace_id = :w"), {"w": str(ws_id)}
    )
    await db_session.execute(
        text("DELETE FROM contexts WHERE workspace_id = :w"), {"w": str(ws_id)}
    )
    await db_session.execute(
        text("DELETE FROM workspace_members WHERE workspace_id = :w"), {"w": str(ws_id)}
    )
    await db_session.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": str(ws_id)})
    await db_session.commit()


# --- download (read) -----------------------------------------------------------


async def test_private_context_file_readable_only_by_creator(scenario):
    """A private-context file: creator reads; a non-creator member, the viewer,
    and even the workspace OWNER are all denied (private = creator-only)."""
    svc, db, ws = scenario["svc"], scenario["db"], scenario["ws"]
    f = _file(workspace_id=ws, context_id=scenario["private"], sha="a" * 64)
    db.add(f)
    await db.flush()

    # Creator (MEMBER) can read.
    url = await svc.get_presigned_download(workspace_id=ws, file_id=f.id, actor_user_id=MEMBER)
    assert url  # FakeBlobStorage returns a non-empty presigned URL

    # Non-creator member, viewer, and workspace owner are all denied. A read
    # denial is existence-hiding: NotFoundException (404), not 403.
    for actor in (OTHER, VIEWER, OWNER):
        with pytest.raises(NotFoundException):
            await svc.get_presigned_download(workspace_id=ws, file_id=f.id, actor_user_id=actor)


async def test_shared_context_file_readable_by_viewer_not_by_scoped_member(scenario):
    """A shared-context file: a workspace viewer may read it; a member scoped
    away by allowed_context_ids=[] may not."""
    svc, db, ws = scenario["svc"], scenario["db"], scenario["ws"]
    f = _file(workspace_id=ws, context_id=scenario["shared"], sha="b" * 64)
    db.add(f)
    await db.flush()

    assert await svc.get_presigned_download(workspace_id=ws, file_id=f.id, actor_user_id=VIEWER)
    # Scoped member is denied — reported as not-found (existence-hiding).
    with pytest.raises(NotFoundException):
        await svc.get_presigned_download(workspace_id=ws, file_id=f.id, actor_user_id=OTHER)


async def test_null_context_file_keeps_legacy_workspace_read(scenario):
    """A workspace-scoped (NULL context) file is downloadable without any context
    check — the route's workspace-viewer gate is the only access control."""
    svc, db, ws = scenario["svc"], scenario["db"], scenario["ws"]
    f = _file(workspace_id=ws, context_id=None, sha="c" * 64)
    db.add(f)
    await db.flush()
    # No actor needed for a NULL-context file (legacy path).
    assert await svc.get_presigned_download(workspace_id=ws, file_id=f.id, actor_user_id=VIEWER)


# --- reserve (write) -----------------------------------------------------------


async def test_reserve_to_context_requires_write_access(scenario):
    """Reserve denies a caller without write access to the target context (raises
    before touching the quota), and rejects a context from another workspace."""
    svc, ws = scenario["svc"], scenario["ws"]
    common = {
        "filename": "f.bin",
        "content_type": "application/octet-stream",
        "size_bytes": 1024,
        "sha256": "d" * 64,
    }
    # VIEWER cannot write to the shared context.
    with pytest.raises(AuthorizationError):
        await svc.reserve_upload(
            workspace_id=ws, created_by=VIEWER, context_id=scenario["shared"], **common
        )
    # OTHER (scoped member) cannot write to the private context they don't own.
    with pytest.raises(AuthorizationError):
        await svc.reserve_upload(
            workspace_id=ws, created_by=OTHER, context_id=scenario["private"], **common
        )


async def test_reserve_rejects_context_from_another_workspace(scenario, db_session):
    """A context_id that belongs to a DIFFERENT workspace is rejected — a file
    can't be bound across the workspace boundary."""
    svc, ws = scenario["svc"], scenario["ws"]
    other_ws = uuid.uuid4()
    db_session.add(Workspace(id=other_ws, name=f"otherws-{other_ws}", owner_user_id=OWNER))
    await db_session.flush()
    foreign_ctx = Context(
        id=uuid.uuid4(), workspace_id=other_ws, name="foreign", created_by=OWNER, is_private=False
    )
    db_session.add(foreign_ctx)
    await db_session.flush()
    try:
        # OWNER is an owner of `other_ws`? No — only of `ws`. So this also fails
        # the access check; bind a member of other_ws to isolate the ws-mismatch.
        db_session.add(
            WorkspaceMember(workspace_id=other_ws, user_id=OWNER, role=WorkspaceRole.OWNER)
        )
        await db_session.flush()
        with pytest.raises(ValidationError, match="does not belong to this workspace"):
            await svc.reserve_upload(
                workspace_id=ws,
                created_by=OWNER,
                context_id=foreign_ctx.id,
                filename="f.bin",
                content_type="application/octet-stream",
                size_bytes=1024,
                sha256="e" * 64,
            )
    finally:
        await db_session.execute(
            text("DELETE FROM contexts WHERE workspace_id = :w"), {"w": str(other_ws)}
        )
        await db_session.execute(
            text("DELETE FROM workspace_members WHERE workspace_id = :w"), {"w": str(other_ws)}
        )
        await db_session.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": str(other_ws)})


# --- delete (write) ------------------------------------------------------------


async def test_delete_denied_for_non_writer_allowed_after_context_deleted(scenario):
    """Deleting a private-context file is denied for a non-creator; but if the
    owning context is (soft-)deleted, delete falls back to the workspace gate so
    the file can still be cleaned up."""
    svc, db, ws = scenario["svc"], scenario["db"], scenario["ws"]
    f = _file(workspace_id=ws, context_id=scenario["private"], sha="f" * 64)
    db.add(f)
    await db.flush()

    with pytest.raises(AuthorizationError):
        await svc.delete_file(workspace_id=ws, file_id=f.id, actor_user_id=OTHER)

    # Soft-delete the owning context → check_context_write raises NotFoundException
    # → delete falls back to the workspace gate (already enforced by the route).
    await db.execute(
        text("UPDATE contexts SET deleted_at = now() WHERE id = :c"),
        {"c": str(scenario["private"])},
    )
    with patch(
        "services.file_storage_service.storage_quota_service.release_storage_bytes",
        AsyncMock(),
    ):
        await svc.delete_file(workspace_id=ws, file_id=f.id, actor_user_id=OTHER)
    refreshed = (await db.execute(select(FileObject).where(FileObject.id == f.id))).scalar_one()
    assert refreshed.deleted_at is not None


# --- list (filter) -------------------------------------------------------------


async def test_list_fail_closed_without_accessible_ids(scenario):
    """list_files with no accessible_context_ids (None) is fail-closed: it returns
    ONLY workspace-scoped (NULL-context) files, never context-bound ones."""
    svc, db, ws = scenario["svc"], scenario["db"], scenario["ws"]
    db.add_all(
        [
            _file(workspace_id=ws, context_id=None, sha="7" * 64),
            _file(workspace_id=ws, context_id=scenario["private"], sha="8" * 64),
            _file(workspace_id=ws, context_id=scenario["shared"], sha="9" * 64),
        ]
    )
    await db.flush()
    # Default accessible_context_ids=None ⇒ empty accessible set.
    out = await svc.list_files(workspace_id=ws, limit=50)
    assert {f.context_id for f in out} == {None}


async def test_list_filters_by_accessible_contexts_end_to_end(scenario):
    """list, fed the caller's accessible-context set, returns NULL-context files +
    files in accessible contexts, and hides private-context files from a viewer."""
    from services.permission_service import PermissionService

    svc, db, ws = scenario["svc"], scenario["db"], scenario["ws"]
    db.add_all(
        [
            _file(workspace_id=ws, context_id=None, sha="1" * 64),
            _file(workspace_id=ws, context_id=scenario["shared"], sha="2" * 64),
            _file(workspace_id=ws, context_id=scenario["private"], sha="3" * 64),
        ]
    )
    await db.flush()
    perms = PermissionService(db)

    # Viewer: accessible = shared (not the private context they didn't create).
    viewer_ids = [c.id for c in await perms.get_accessible_contexts(VIEWER, ws)]
    viewer_files = await svc.list_files(
        workspace_id=ws, accessible_context_ids=viewer_ids, limit=50
    )
    viewer_ctx = {f.context_id for f in viewer_files}
    assert None in viewer_ctx  # NULL-context file visible
    assert scenario["shared"] in viewer_ctx  # shared-context file visible
    assert scenario["private"] not in viewer_ctx  # private hidden from non-creator

    # Even the workspace OWNER does not see the private-context file in the
    # listing — private is creator-only, so it is hidden from everyone but MEMBER.
    owner_ids = [c.id for c in await perms.get_accessible_contexts(OWNER, ws)]
    owner_ctx = {
        f.context_id
        for f in await svc.list_files(workspace_id=ws, accessible_context_ids=owner_ids, limit=50)
    }
    assert scenario["shared"] in owner_ctx
    assert scenario["private"] not in owner_ctx

    # Creator (MEMBER) of the private context: sees all three.
    member_ids = [c.id for c in await perms.get_accessible_contexts(MEMBER, ws)]
    member_files = await svc.list_files(
        workspace_id=ws, accessible_context_ids=member_ids, limit=50
    )
    member_ctx = {f.context_id for f in member_files}
    assert {None, scenario["shared"], scenario["private"]} <= member_ctx
