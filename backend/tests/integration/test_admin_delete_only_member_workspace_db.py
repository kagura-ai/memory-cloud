"""An owned workspace only the user is in can go with the user (#2640).

``DELETE /admin/users/{id}`` requires a successor for each owned workspace, and
a successor must already be a member, so a workspace nobody else is in (the
409 lists it with ``members: []``) could never be handed over. The request may
name it in ``delete_workspaces`` instead: it is purged with the user the way a
soft-deleted owned workspace is (#2406), everything in it included, after the
same billing check. A workspace with another member still takes a successor.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from api.routes.admin import UserDeleteRequest, delete_user
from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from models.file_objects import FileObject
from models.memory import Memory
from services.user_deletion_service import (
    WORKSPACE_DELETE_AND_TRANSFER,
    WORKSPACE_HAS_OTHER_MEMBERS,
    WORKSPACE_PURGED_ACTION,
)

NO_LONGER_MATCH = "Transfer selections no longer match the user's resources"


async def _context(db, workspace_id, creator, *, private: bool = False) -> UUID:
    ctx = Context(
        id=uuid4(),
        name=f"ctx-{uuid4().hex[:8]}",
        workspace_id=workspace_id,
        created_by=creator,
        is_private=private,
    )
    db.add(ctx)
    await db.flush()
    return ctx.id


async def _memory(db, workspace_id, context_id: UUID | None, author) -> UUID:
    memory = Memory(
        id=uuid4(),
        user_id=author,
        workspace_id=workspace_id,
        context_id=context_id,
        content="row",
        summary="row",
        type="note",
        client="test",
    )
    db.add(memory)
    await db.flush()
    return memory.id


async def _count(db, model, *where) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _unchanged(db, target: str, ws_id: UUID) -> None:
    await db.rollback()
    assert await db.scalar(select(User.user_id).where(User.user_id == target)) == target
    assert await db.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id)) == target


@pytest.mark.asyncio
async def test_an_only_member_workspace_goes_with_the_user(
    connector_world, db_session, purge_audits, vectors, storage_keys
):
    world = connector_world
    target = await world.user("departing")
    solo = await world.workspace(target)
    solo_id, solo_name = solo.id, solo.name
    purge_audits.append(solo_id)
    shared = await _context(db_session, solo_id, target)
    private = await _context(db_session, solo_id, target, private=True)
    for ctx in (shared, shared, private, None):
        await _memory(db_session, solo_id, ctx, target)
    storage_keys.append(f"files/{uuid4()}")
    db_session.add(
        FileObject(
            workspace_id=solo_id,
            context_id=shared,
            sha256="b" * 64,
            size_bytes=3,
            content_type="text/plain",
            filename="b.txt",
            storage_key=storage_keys[0],
            status="uploaded",
            created_by=target,
        )
    )
    await db_session.commit()

    # Without the field the workspace still needs a successor, and the picker
    # payload says nobody else is in it.
    with pytest.raises(HTTPException) as error:
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert error.value.status_code == 409
    detail = error.value.detail
    await _unchanged(db_session, target, solo_id)
    assert detail["workspace_ids"] == [str(solo_id)]
    assert detail["context_ids"] == [str(shared)]
    assert [(w["id"], w["members"]) for w in detail["workspaces"]] == [(str(solo_id), [])]

    response = await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(delete_workspaces=[solo_id]),
    )

    assert response["purged_workspaces"] == [
        {"id": str(solo_id), "name": solo_name, "contexts": 2, "memories": 4}
    ]
    assert response["transferred_workspaces"] == {}
    db_session.expire_all()
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) is None
    assert await db_session.get(Workspace, solo_id) is None
    assert await _count(db_session, Context, Context.workspace_id == solo_id) == 0
    assert await _count(db_session, Memory, Memory.workspace_id == solo_id) == 0
    assert await _count(db_session, FileObject, FileObject.workspace_id == solo_id) == 0
    assert await _count(db_session, WorkspaceMember, WorkspaceMember.workspace_id == solo_id) == 0
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == WORKSPACE_PURGED_ACTION,
                AuditLog.resource == f"workspace:{solo_id}",
            )
        )
    ).scalar_one()
    assert audit.user_id == "operator"
    assert audit.user_metadata == {
        "previous_owner_id": target,
        "soft_deleted": False,
        "contexts": 2,
        "memories": 4,
        "billing_contract_active": False,
    }
    # A live workspace's points are still there: both contexts are asked for.
    assert {call.args for call in vectors.await_args_list} == {
        (str(solo_id), str(shared)),
        (str(solo_id), str(private)),
    }


@pytest.mark.asyncio
async def test_a_shared_workspace_is_handed_over_while_the_solo_one_goes(
    connector_world, db_session, purge_audits, vectors
):
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    solo = await world.workspace(target)
    kept = await world.workspace(target, **{successor: WorkspaceRole.ADMIN})
    solo_id, kept_id = solo.id, kept.id
    purge_audits.append(solo_id)
    kept_ctx = await _context(db_session, kept_id, target)
    await db_session.commit()

    response = await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(delete_workspaces=[solo_id], workspace_transfers={kept_id: successor}),
    )

    assert [ws["id"] for ws in response["purged_workspaces"]] == [str(solo_id)]
    assert response["transferred_workspaces"] == {str(kept_id): successor}
    db_session.expire_all()
    assert await db_session.get(Workspace, solo_id) is None
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == kept_id))
        == successor
    )
    assert (
        await db_session.scalar(select(Context.created_by).where(Context.id == kept_ctx))
        == successor
    )


@pytest.mark.asyncio
async def test_a_workspace_with_another_member_is_not_deleted(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    other = await world.user("other")
    workspace = await world.workspace(target, **{other: WorkspaceRole.VIEWER})
    ws_id = workspace.id
    ctx_id = await _context(db_session, ws_id, target)
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(delete_workspaces=[ws_id]),
        )
    assert error.value.status_code == 409
    assert error.value.detail == WORKSPACE_HAS_OTHER_MEMBERS
    await _unchanged(db_session, target, ws_id)
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == target
    assert await _count(db_session, WorkspaceMember, WorkspaceMember.workspace_id == ws_id) == 2


@pytest.mark.asyncio
async def test_a_workspace_is_handed_over_or_deleted_not_both(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    workspace = await world.workspace(target, **{successor: WorkspaceRole.ADMIN})
    ws_id = workspace.id
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(delete_workspaces=[ws_id], workspace_transfers={ws_id: successor}),
        )
    assert error.value.status_code == 409
    assert error.value.detail == WORKSPACE_DELETE_AND_TRANSFER
    await _unchanged(db_session, target, ws_id)


@pytest.mark.asyncio
async def test_a_context_in_a_deleted_workspace_takes_no_successor(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    solo = await world.workspace(target)
    solo_id = solo.id
    solo_ctx = await _context(db_session, solo_id, target)
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(delete_workspaces=[solo_id], context_transfers={solo_ctx: owner}),
        )
    assert error.value.status_code == 409
    assert error.value.detail == NO_LONGER_MATCH
    await _unchanged(db_session, target, solo_id)
    assert (
        await db_session.scalar(select(Context.created_by).where(Context.id == solo_ctx)) == target
    )


@pytest.mark.asyncio
async def test_a_workspace_the_user_does_not_own_live_cannot_be_named(connector_world, db_session):
    """Someone else's workspace, or one the user owns that is already
    soft-deleted (it goes anyway), no longer matches."""
    from tests.integration.test_soft_deleted_workspace_purge_db import _soft_delete

    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    solo = await world.workspace(target)
    gone = await world.workspace(target)
    theirs = await world.workspace(owner, **{target: WorkspaceRole.MEMBER})
    solo_id, gone_id, theirs_id = solo.id, gone.id, theirs.id
    await _soft_delete(db_session, gone_id)
    await db_session.commit()

    for named in (theirs_id, gone_id):
        with pytest.raises(HTTPException) as error:
            await delete_user(
                target,
                {"user_id": "operator"},
                db_session,
                UserDeleteRequest(delete_workspaces=[solo_id, named]),
            )
        assert error.value.status_code == 409
        assert error.value.detail == NO_LONGER_MATCH
        await _unchanged(db_session, target, solo_id)
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == theirs_id))
        == owner
    )
