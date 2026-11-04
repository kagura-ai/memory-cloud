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
async def test_connector_write_target_goes_to_the_workspace_owner(connector_world, db_session):
    """#2420: a connector's write target goes to the workspace's owner, who
    also takes the connector's key, so the deletion asks no successor for it
    and refuses a selection naming it (the rule account erasure applies,
    #2557)."""
    from sqlalchemy import update

    from models.resource import WorkspaceConnector
    from services.user_deletion_service import CONNECTOR_CONTEXT_FOLLOWS_OWNER

    from ._connector_world import _live_connector_key

    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    member = await world.user("member")
    workspace = await world.workspace(owner, **{target: "admin", member: "admin"})
    # Connectors create their write target private (#2417): it is still the
    # workspace's, never the creator's own.
    ctx_id = await _context(db_session, workspace, target, private=True)
    connector = (await world.connector(workspace, by=target)).connector
    connector_id = connector.id
    await db_session.execute(
        update(WorkspaceConnector)
        .where(WorkspaceConnector.id == connector_id)
        .values(context_id=ctx_id)
    )
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(context_transfers={ctx_id: member}),
        )
    assert error.value.status_code == 409
    assert error.value.detail == CONNECTOR_CONTEXT_FOLLOWS_OWNER
    await db_session.rollback()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == target

    response = await delete_user(target, {"user_id": "operator"}, db_session)
    assert response["deleted"]["contexts"] == 0
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == owner
    assert (await _live_connector_key(db_session, connector_id)).user_id == owner
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
    assert "linked account" in str(error.value.detail)
    await db_session.rollback()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == target

    response = await delete_user(target, {"user_id": "operator"}, db_session)
    assert response["deleted"]["contexts"] == 0
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == linked
    assert await db_session.get(Memory, theirs_id) is not None
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) is None


@pytest.mark.asyncio
async def test_a_linked_contributor_does_not_take_a_connector_write_target(
    connector_world, db_session
):
    """#2422: the hand-over leaves a connector's write target to the
    workspace even where a linked admin wrote in it; the workspace's owner
    gets it with the connector's key (#2420), no selection asked."""
    from sqlalchemy import update

    from models.auth import IdentityLink
    from models.resource import WorkspaceConnector

    from ._connector_world import _live_connector_key

    world = connector_world
    target = await world.user("departing")
    linked = await world.user("linked")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin", linked: "admin"})
    ws_id = workspace.id
    ctx_id = await _context(db_session, workspace, target, private=True)
    connector_id = (await world.connector(workspace, by=target)).connector.id
    key_id = (await _live_connector_key(db_session, connector_id)).id
    await db_session.execute(
        update(WorkspaceConnector)
        .where(WorkspaceConnector.id == connector_id)
        .values(context_id=ctx_id)
    )
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

    response = await delete_user(target, {"user_id": "operator"}, db_session)
    assert response["deleted"]["contexts"] == 0
    assert response["reowned"] == {"connector_api_keys": 1, "agents": 0}
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == owner
    assert await db_session.get(Memory, theirs_id) is not None
    assert (await _live_connector_key(db_session, connector_id)).id == key_id
    assert (await _live_connector_key(db_session, connector_id)).user_id == owner


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

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(context_transfers={private_id: owner, shared_id: owner}),
        )
    assert error.value.status_code == 409
    assert "nobody else wrote in" in str(error.value.detail)
    await db_session.rollback()

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


@pytest.mark.asyncio
async def test_private_context_someone_else_wrote_in_takes_a_successor(connector_world, db_session):
    """#2417: a linked author the hand-over cannot pick (a viewer) still has
    memories in the private context, so it is not the user's own: it needs a
    successor instead of going with the user."""
    from models.auth import IdentityLink

    world = connector_world
    target = await world.user("departing")
    linked = await world.user("linked")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin", linked: "viewer"})
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
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert error.value.status_code == 409
    assert error.value.detail["context_ids"] == [str(ctx_id)]
    await db_session.rollback()

    response = await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(context_transfers={ctx_id: owner}),
    )
    assert response["deleted"]["contexts"] == 0
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == owner
    assert await db_session.get(Memory, theirs_id) is not None


@pytest.mark.asyncio
async def test_another_accounts_soft_deleted_memory_keeps_the_private_context(
    connector_world, db_session
):
    """#2417: a row another account can still restore makes the private
    context more than the user's own, so it takes a successor instead of
    being deleted over that row."""
    from utils.datetime import utcnow

    world = connector_world
    target = await world.user("departing")
    member = await world.user("member")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin", member: "admin"})
    ws_id = workspace.id
    ctx_id = await _context(db_session, workspace, target, private=True)
    theirs = Memory(
        id=uuid4(),
        user_id=member,
        workspace_id=ws_id,
        context_id=ctx_id,
        content="theirs",
        summary="theirs",
        type="note",
        client="test",
        deleted_at=utcnow(),
    )
    db_session.add(theirs)
    theirs_id = theirs.id
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert error.value.detail["context_ids"] == [str(ctx_id)]
    await db_session.rollback()

    response = await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(context_transfers={ctx_id: member}),
    )
    assert response["deleted"]["contexts"] == 0
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == member
    assert await db_session.get(Memory, theirs_id) is not None


async def _write_target(db, world, workspace, creator) -> tuple:
    """A private context ``creator`` made that a connector ``creator``
    provisioned writes into; returns (context id, connector id)."""
    from sqlalchemy import update

    from models.resource import WorkspaceConnector

    ctx_id = await _context(db, workspace, creator, private=True)
    connector_id = (await world.connector(workspace, by=creator)).connector.id
    await db.execute(
        update(WorkspaceConnector)
        .where(WorkspaceConnector.id == connector_id)
        .values(context_id=ctx_id)
    )
    await db.flush()
    return ctx_id, connector_id


@pytest.mark.asyncio
async def test_connector_write_target_follows_the_workspace_successor(connector_world, db_session):
    """#2420: in a workspace the user owns, its successor becomes the owner,
    so the connector's write target follows that successor with its key."""
    from services.user_deletion_service import CONNECTOR_CONTEXT_FOLLOWS_OWNER

    from ._connector_world import _live_connector_key

    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    workspace = await world.workspace(target, **{successor: "admin"})
    ws_id = workspace.id
    ctx_id, connector_id = await _write_target(db_session, world, workspace, target)
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert error.value.status_code == 409
    detail = error.value.detail
    assert detail["workspace_ids"] == [str(ws_id)]
    assert detail["context_ids"] == []
    assert detail["contexts"] == []
    assert [(c["id"], c["successor_user_id"]) for c in detail["owner_fixed_contexts"]] == [
        (str(ctx_id), None)
    ]
    await db_session.rollback()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(
                workspace_transfers={ws_id: successor}, context_transfers={ctx_id: successor}
            ),
        )
    assert error.value.detail == CONNECTOR_CONTEXT_FOLLOWS_OWNER
    await db_session.rollback()

    await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(workspace_transfers={ws_id: successor}),
    )
    db_session.expire_all()
    assert (
        await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == successor
    )
    assert (await _live_connector_key(db_session, connector_id)).user_id == successor


@pytest.mark.asyncio
async def test_transfer_required_payload_describes_the_picker(connector_world, db_session):
    """#2420: the 409 carries what the admin's successor picker shows: the
    workspaces with their members (the user excluded), each context's
    candidates exactly as the validation accepts them (no viewer, no member
    the context is closed to), and the connector write targets fixed to the
    workspace's owner (``None``: the workspace successor picked)."""
    from sqlalchemy import update

    from models.auth import WorkspaceMember

    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    viewer = await world.user("viewer")
    other_owner = await world.user("owner")
    reaching = await world.user("reaching")
    closed = await world.user("closed")
    owned = await world.workspace(target, **{successor: "admin", viewer: "viewer"})
    theirs = await world.workspace(
        other_owner,
        **{target: "admin", viewer: "viewer", reaching: "member", closed: "member"},
    )
    owned_id, theirs_id = owned.id, theirs.id
    owned_ctx = await _context(db_session, owned, target)
    their_ctx = await _context(db_session, theirs, target)
    owned_fixed, _ = await _write_target(db_session, world, owned, target)
    their_fixed, their_connector = await _write_target(db_session, world, theirs, target)
    for user_id, allowed in ((reaching, [their_ctx]), (closed, [])):
        await db_session.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == theirs_id, WorkspaceMember.user_id == user_id)
            .values(allowed_context_ids=allowed)
        )
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert error.value.status_code == 409
    detail = error.value.detail
    await db_session.rollback()
    assert detail["message"] == "Ownership transfer required"
    assert detail["workspace_ids"] == [str(owned_id)]
    assert set(detail["context_ids"]) == {str(owned_ctx), str(their_ctx)}

    workspaces = {w["id"]: w for w in detail["workspaces"]}
    assert set(workspaces) == {str(owned_id), str(theirs_id)}
    assert workspaces[str(owned_id)]["owner_user_id"] == target
    assert workspaces[str(theirs_id)]["owner_user_id"] == other_owner
    assert {(m["user_id"], m["role"]) for m in workspaces[str(owned_id)]["members"]} == {
        (successor, "admin"),
        (viewer, "viewer"),
    }
    assert {m["user_id"] for m in workspaces[str(theirs_id)]["members"]} == {
        other_owner,
        viewer,
        reaching,
        closed,
    }
    member = next(m for m in workspaces[str(owned_id)]["members"] if m["user_id"] == successor)
    assert member["name"] == "successor"
    assert member["email"] == f"{successor}@example.com"

    contexts = {c["id"]: c for c in detail["contexts"]}
    assert set(contexts) == {str(owned_ctx), str(their_ctx)}
    assert contexts[str(owned_ctx)]["workspace_id"] == str(owned_id)
    assert contexts[str(owned_ctx)]["is_private"] is False
    assert contexts[str(owned_ctx)]["name"].startswith("ctx-")
    assert "display_name" in contexts[str(owned_ctx)]
    assert set(contexts[str(owned_ctx)]["candidate_user_ids"]) == {successor}
    assert set(contexts[str(their_ctx)]["candidate_user_ids"]) == {other_owner, reaching}

    fixed = {c["id"]: c for c in detail["owner_fixed_contexts"]}
    assert set(fixed) == {str(owned_fixed), str(their_fixed)}
    assert fixed[str(owned_fixed)]["successor_user_id"] is None
    assert fixed[str(their_fixed)]["successor_user_id"] == other_owner
    assert fixed[str(their_fixed)]["workspace_id"] == str(theirs_id)

    # A candidate the payload leaves out is refused by the validation.
    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(
                workspace_transfers={owned_id: successor}, context_transfers={their_ctx: closed}
            ),
        )
    assert error.value.detail == "Context successor must have workspace/context access"
    await db_session.rollback()

    await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(
            workspace_transfers={owned_id: successor}, context_transfers={their_ctx: reaching}
        ),
    )
    db_session.expire_all()
    created_by = dict(
        (
            await db_session.execute(
                select(Context.id, Context.created_by).where(
                    Context.id.in_([owned_ctx, their_ctx, owned_fixed, their_fixed])
                )
            )
        ).all()
    )
    assert created_by == {
        owned_ctx: successor,
        their_ctx: reaching,
        owned_fixed: successor,
        their_fixed: other_owner,
    }


@pytest.mark.asyncio
async def test_the_write_targets_owner_stays_locked_until_the_deletion_commits(
    connector_world, db_session
):
    """#2420 review: the owner a connector's write target goes to is locked
    with the successors, so it can neither be deleted nor hand its workspace
    on (an ownership transfer takes FOR KEY SHARE on the owner's row) before
    the deletion commits: the context never lands on a gone account."""
    import asyncio

    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from services.user_deletion_service import delete_user_rows

    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin"})
    ctx_id, _ = await _write_target(db_session, world, workspace, target)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gap, resume = asyncio.Event(), asyncio.Event()

    async def pause_cleanup(db, subject, **kwargs):
        gap.set()
        await asyncio.wait_for(resume.wait(), 15)
        return await delete_user_rows(db, subject, **kwargs)

    async def remove():
        async with maker() as db:
            await delete_user(target, {"user_id": "operator"}, db)

    with patch("services.user_deletion_service.delete_user_rows", pause_cleanup):
        deletion = asyncio.create_task(remove())
        try:
            await asyncio.wait_for(gap.wait(), 15)
            async with maker() as other:
                with pytest.raises(DBAPIError):
                    await other.execute(
                        select(User.user_id)
                        .where(User.user_id == owner)
                        .with_for_update(key_share=True, nowait=True)
                    )
                await other.rollback()
        finally:
            resume.set()
            await asyncio.wait_for(deletion, 15)
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == owner


@pytest.mark.asyncio
async def test_a_write_target_validated_as_fixed_goes_to_the_owner_even_if_unbound_meanwhile(
    connector_world, db_session
):
    """#2420 review: the owner gets the contexts the validation found to be
    write targets, whatever happens to the connector before the apply step;
    otherwise a private one nobody else wrote in would be left to the user
    and deleted with it."""
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from models.resource import WorkspaceConnector
    from services.connector_contexts import connector_write_targets_among as real_among

    world = connector_world
    target = await world.user("departing")
    owner = await world.user("owner")
    workspace = await world.workspace(owner, **{target: "admin"})
    ctx_id, connector_id = await _write_target(db_session, world, workspace, target)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def unbind_after_the_read(db, context_ids):
        found = await real_among(db, context_ids)
        async with maker() as other:
            await other.execute(
                update(WorkspaceConnector)
                .where(WorkspaceConnector.id == connector_id)
                .values(context_id=None)
            )
            await other.commit()
        return found

    with patch(
        "services.user_deletion_service.connector_write_targets_among", unbind_after_the_read
    ):
        response = await delete_user(target, {"user_id": "operator"}, db_session)
    assert response["deleted"]["contexts"] == 0
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == owner


@pytest.mark.asyncio
async def test_a_target_unbound_before_the_resend_takes_a_successor_again(
    connector_world, db_session
):
    """#2420 review: a connector unbound from its write target between the 409
    and the resend leaves an ordinary shared context: the resend lists it as
    needing a successor instead of handing it to the owner or deleting it."""
    from sqlalchemy import update

    from models.resource import WorkspaceConnector

    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    owner = await world.user("owner")
    member = await world.user("member")
    owned = await world.workspace(target, **{successor: "admin"})
    theirs = await world.workspace(owner, **{target: "admin", member: "admin"})
    owned_id = owned.id
    ctx_id = await _context(db_session, theirs, target)
    connector_id = (await world.connector(theirs, by=target)).connector.id
    await db_session.execute(
        update(WorkspaceConnector)
        .where(WorkspaceConnector.id == connector_id)
        .values(context_id=ctx_id)
    )
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(target, {"user_id": "operator"}, db_session)
    detail = error.value.detail
    assert detail["context_ids"] == []
    assert [(c["id"], c["successor_user_id"]) for c in detail["owner_fixed_contexts"]] == [
        (str(ctx_id), owner)
    ]
    await db_session.rollback()

    await db_session.execute(
        update(WorkspaceConnector)
        .where(WorkspaceConnector.id == connector_id)
        .values(context_id=None)
    )
    await db_session.commit()

    with pytest.raises(HTTPException) as error:
        await delete_user(
            target,
            {"user_id": "operator"},
            db_session,
            UserDeleteRequest(workspace_transfers={owned_id: successor}),
        )
    detail = error.value.detail
    assert detail["context_ids"] == [str(ctx_id)]
    assert detail["owner_fixed_contexts"] == []
    assert set(detail["contexts"][0]["candidate_user_ids"]) == {owner, member}
    await db_session.rollback()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == target

    await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(
            workspace_transfers={owned_id: successor}, context_transfers={ctx_id: member}
        ),
    )
    db_session.expire_all()
    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == member
