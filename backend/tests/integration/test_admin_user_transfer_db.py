"""Ownership must survive an administrative user deletion (#2372)."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from api.routes.admin import UserDeleteRequest, delete_user
from models.auth import AuditLog, Context, ContextMember, OAuth2Client, User, Workspace
from models.memory import Memory


async def _context(db, workspace, creator, *, private: bool = False):
    """A context ``creator`` made; shared unless ``private`` (#2417: only a
    shared context takes a successor the admin picks)."""
    ctx = Context(
        id=uuid4(),
        name=f"ctx-{uuid4().hex[:8]}",
        workspace_id=workspace.id,
        created_by=creator,
        is_private=private,
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
    # Connectors create their write target private (#2417): it is still the
    # workspace's, so it takes a successor like a shared context.
    ctx_id = await _context(db_session, workspace, target, private=True)
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


@pytest.mark.asyncio
async def test_private_context_passes_to_a_linked_contributor_not_a_successor(
    connector_world, db_session
):
    """#2417: a private context is readable by its creator and the accounts
    linked to it only, so the #1784 hand-over decides who keeps it, not the
    admin — a selection naming one is refused."""
    from models.auth import IdentityLink

    world = connector_world
    target = await world.user("departing")
    linked = await world.user("linked")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin", linked: "admin"})
    ws_id = workspace.id
    ctx_id = await _context(db_session, workspace, target, private=True)
    group = uuid4()
    db_session.add_all(
        [
            IdentityLink(group_id=group, user_id=target, linked_by=target),
            IdentityLink(group_id=group, user_id=linked, linked_by=target),
        ]
    )
    theirs = Memory(
        id=uuid4(),
        user_id=linked,
        workspace_id=ws_id,
        context_id=ctx_id,
        content="theirs",
        summary="theirs",
        type="note",
        client="test",
    )
    db_session.add(theirs)
    theirs_id = theirs.id
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(context_transfers={ctx_id: owner}),
        )
    assert error.value.status_code == 409
    assert "Private contexts" in str(error.value.detail)
    await db_session.rollback()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == target

    response = await delete_user(target, {"user_id": "operator"}, db_session)
    assert response["deleted"]["contexts"] == 0
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == linked
    assert await db_session.get(Memory, theirs_id) is not None
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) is None


@pytest.mark.asyncio
async def test_private_context_nobody_linked_wrote_in_is_deleted_with_the_user(
    connector_world, db_session
):
    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin"})
    private_id = await _context(db_session, workspace, target, private=True)
    shared_id = await _context(db_session, workspace, target)
    await db_session.commit()

    response = await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(context_transfers={shared_id: owner}),
    )
    assert response["deleted"]["contexts"] == 1
    assert await db_session.scalar(select(Context.id).where(Context.id == private_id)) is None
    assert (
        await db_session.scalar(select(Context.created_by).where(Context.id == shared_id)) == owner
    )
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) is None


@pytest.mark.asyncio
async def test_a_running_subscription_refuses_the_delete_before_any_transfer(
    connector_world, db_session
):
    """#2417 / #1940: the subscription is the user's; it ends before the
    account goes, as account erasure and the CLI require."""
    from sqlalchemy import update

    from models.auth import ENTITLEMENT_SOURCE_EXTERNAL_BILLING
    from utils.exceptions import BillingContractActiveError

    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    workspace = await world.workspace(target, **{successor: "admin"})
    ws_id = workspace.id
    await db_session.execute(
        update(Workspace)
        .where(Workspace.id == ws_id)
        .values(entitlement_source=ENTITLEMENT_SOURCE_EXTERNAL_BILLING)
    )
    await db_session.commit()

    with pytest.raises(BillingContractActiveError) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(workspace_transfers={ws_id: successor}),
        )
    assert error.value.error_code == "BILLING-005"
    assert error.value.details["workspace_ids"] == [str(ws_id)]
    await db_session.rollback()
    assert (
        await db_session.scalar(select(Workspace.owner_user_id).where(Workspace.id == ws_id))
        == target
    )
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) == target
