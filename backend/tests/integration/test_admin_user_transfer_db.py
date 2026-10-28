"""Ownership must survive an administrative user deletion (#2372)."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from api.routes.admin import UserDeleteRequest, delete_user
from models.auth import AuditLog, Context, ContextMember, OAuth2Client, User, Workspace
from models.memory import Memory


async def _context(db, workspace, creator):
    ctx = Context(
        id=uuid4(),
        name=f"ctx-{uuid4().hex[:8]}",
        workspace_id=workspace.id,
        created_by=creator,
        is_private=True,
    )
    db.add(ctx)
    await db.flush()
    return ctx.id


@pytest.mark.asyncio
async def test_delete_requires_transfer_without_mutating_owned_data(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    workspace = await world.workspace(target, **{successor: "admin"})
    ctx_id = await _context(db_session, workspace, target)
    ws_id = workspace.id
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert error.value.status_code == 409
    await db_session.rollback()
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) == target
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id))
        == target
    )
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == target


@pytest.mark.asyncio
async def test_transfer_keeps_context_and_other_authors_memories(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    workspace = await world.workspace(target, **{successor: "admin"})
    ctx_id = await _context(db_session, workspace, target)
    ws_id = workspace.id
    survivor = Memory(
        id=uuid4(),
        user_id=successor,
        workspace_id=ws_id,
        context_id=ctx_id,
        content="survives",
        summary="survives",
        type="note",
        client="test",
    )
    departed = Memory(
        id=uuid4(),
        user_id=target,
        workspace_id=ws_id,
        context_id=ctx_id,
        content="private",
        summary="private",
        type="note",
        client="test",
    )
    client_id = f"owned-{uuid4()}"
    db_session.add_all(
        [
            ContextMember(context_id=ctx_id, user_id=target, role="editor"),
            ContextMember(context_id=ctx_id, user_id=successor, role="editor"),
            OAuth2Client(
                client_id=client_id,
                client_name="retiring client",
                client_secret_hash="a" * 64,
                owner_id=target,
                workspace_id=ws_id,
                redirect_uris=["https://example.test/callback"],
            ),
        ]
    )
    db_session.add_all([survivor, departed])
    survivor_id, departed_id = survivor.id, departed.id
    await db_session.commit()
    await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(workspace_transfers={ws_id: successor}),
    )
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id))
        == successor
    )
    assert (
        await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == successor
    )
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "workspace_ownership_transfer",
                AuditLog.resource == f"workspace:{ws_id}",
            )
        )
    ).scalar_one()
    assert audit.user_id == "operator"
    assert (
        await db_session.scalar(select(OAuth2Client.id).where(OAuth2Client.client_id == client_id))
        is None
    )
    assert set(
        (
            await db_session.execute(
                select(ContextMember.user_id).where(ContextMember.context_id == ctx_id)
            )
        ).scalars()
    ) == {successor}
    assert await db_session.get(Memory, survivor_id) is not None
    db_session.expire_all()
    assert await db_session.scalar(select(Memory.id).where(Memory.id == departed_id)) is None


@pytest.mark.asyncio
async def test_invalid_later_successor_rolls_back_all_transfers(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    first = await world.workspace(target, **{successor: "admin"})
    second = await world.workspace(target)
    ids = [first.id, second.id]
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(workspace_transfers=dict.fromkeys(ids, successor)),
        )
    assert error.value.status_code == 409
    await db_session.rollback()
    assert set(
        (
            await db_session.execute(select(Workspace.owner_user_id).where(Workspace.id.in_(ids)))
        ).scalars()
    ) == {target}


@pytest.mark.asyncio
async def test_cleanup_failure_rolls_back_transfer_and_epoch(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    workspace = await world.workspace(target, **{successor: "admin"})
    ws_id, epoch = workspace.id, workspace.ownership_epoch
    await db_session.commit()
    with patch(
        "services.user_deletion_service.delete_user_rows",
        AsyncMock(side_effect=RuntimeError("failed cleanup")),
    ):
        with pytest.raises(RuntimeError, match="failed cleanup"):
            await delete_user(
                target,
                {"user_id": "operator"},
                db_session,
                UserDeleteRequest(workspace_transfers={ws_id: successor}),
            )
    row = await db_session.get(Workspace, ws_id)
    assert row.owner_user_id == target and row.ownership_epoch == epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("role,allowed", [("viewer", True), ("member", False)])
async def test_context_target_must_be_eligible(connector_world, db_session, role, allowed):
    from sqlalchemy import update

    from models.auth import WorkspaceMember

    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin", successor: role})
    ctx_id = await _context(db_session, workspace, target)
    await db_session.execute(
        update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == successor)
        .values(allowed_context_ids=[ctx_id] if allowed else [])
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(context_transfers={ctx_id: successor}),
        )
    assert error.value.status_code == 409
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == target


@pytest.mark.asyncio
async def test_context_only_successor_keeps_connector_target(connector_world, db_session):
    from sqlalchemy import update

    from models.resource import WorkspaceConnector

    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin"})
    ctx_id = await _context(db_session, workspace, target)
    connector = (await world.connector(workspace, by=target)).connector
    connector_id = connector.id
    await db_session.execute(
        update(WorkspaceConnector)
        .where(WorkspaceConnector.id == connector_id)
        .values(context_id=ctx_id)
    )
    await db_session.commit()
    await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(context_transfers={ctx_id: owner}),
    )
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == owner
    assert (
        await db_session.scalar(
            select(WorkspaceConnector.context_id).where(WorkspaceConnector.id == connector_id)
        )
        == ctx_id
    )
