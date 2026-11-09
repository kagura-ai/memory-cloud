"""A deleted user's soft-deleted workspaces are hard-deleted with it (#2406).

The owner already deleted them: handing them to a successor would widen access
to data the owner chose to remove, and a row left behind keeps pointing at an
account that no longer exists. ``DELETE /admin/users/{id}``, ``cli.delete_admin``
and account erasure all take them with the user, with the cascade the CLI uses
for a live workspace only the user is in (#2372): its contexts, memories, keys,
members and file rows go in the same transaction (its memories with no
context too), one ``workspace_purged`` audit row per workspace records it,
and its vector points are not asked for again: they went when it was
soft-deleted, and ``sweep_orphan_vectors`` removes any that call left.
A live owned workspace still needs a successor, and a soft-deleted workspace
someone else owns is left alone.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update

from api.routes.admin import UserDeleteRequest, delete_user
from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, AuditLog, Context, User, Workspace, WorkspaceMember
from models.file_objects import FileObject
from models.memory import Memory
from models.operation_history import OperationHistory
from services.operation_history_writer import record_operation
from services.user_deletion_service import WORKSPACE_PURGED_ACTION
from tests.integration.test_account_erasure_integration import (  # noqa: F401 - fixture
    patched_external_stores,
)
from utils.datetime import utcnow


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


async def _soft_delete(db, workspace_id) -> None:
    await db.execute(
        update(Workspace).where(Workspace.id == workspace_id).values(deleted_at=utcnow())
    )


async def _count(db, model, *where) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


@pytest.mark.asyncio
async def test_admin_delete_purges_a_soft_deleted_owned_workspace(
    connector_world, db_session, purge_audits, vectors, storage_keys
):
    world = connector_world
    target = await world.user("departing")
    other = await world.user("other")
    gone = await world.workspace(target, **{other: WorkspaceRole.MEMBER})
    gone_id = gone.id
    purge_audits.append(gone_id)
    shared = await _context(db_session, gone_id, target)
    theirs = await _context(db_session, gone_id, other, private=True)
    for ctx, author in ((shared, target), (shared, other), (theirs, other), (None, other)):
        await _memory(db_session, gone_id, ctx, author)
    await world.personal_key(other, gone)
    storage_keys.append(f"files/{uuid4()}")
    db_session.add(
        FileObject(
            workspace_id=gone_id,
            context_id=shared,
            sha256="a" * 64,
            size_bytes=3,
            content_type="text/plain",
            filename="a.txt",
            storage_key=storage_keys[0],
            status="uploaded",
            created_by=other,
            deleted_at=utcnow(),
        )
    )
    # A soft-deleted workspace someone else owns that the user is in stays.
    foreign = await world.workspace(other, **{target: WorkspaceRole.MEMBER})
    foreign_id = foreign.id
    foreign_ctx = await _context(db_session, foreign_id, other)
    # A private context of the user's own in a live workspace goes too, and
    # its points are deleted after the commit.
    live = await world.workspace(other, **{target: WorkspaceRole.MEMBER})
    live_id = live.id
    mine = await _context(db_session, live_id, target, private=True)
    for actor in (target, other):
        record_operation(
            db_session,
            operation="member.added",
            actor_user_id=actor,
            workspace_id=gone_id,
            target_id=other,
            subject_user_id=other,
        )
    await _soft_delete(db_session, gone_id)
    await _soft_delete(db_session, foreign_id)
    await db_session.commit()

    response = await delete_user(target, {"user_id": "operator"}, db_session)

    assert response["purged_workspaces"] == [
        {"id": str(gone_id), "name": gone.name, "contexts": 2, "memories": 4}
    ]
    db_session.expire_all()
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) is None
    assert await db_session.get(Workspace, gone_id) is None
    assert await _count(db_session, Context, Context.workspace_id == gone_id) == 0
    assert await _count(db_session, Memory, Memory.workspace_id == gone_id) == 0
    assert await _count(db_session, APIKey, APIKey.workspace_id == gone_id) == 0
    assert await _count(db_session, FileObject, FileObject.workspace_id == gone_id) == 0
    assert await _count(db_session, WorkspaceMember, WorkspaceMember.workspace_id == gone_id) == 0
    assert await _count(db_session, OperationHistory, OperationHistory.workspace_id == gone_id) == 0
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == WORKSPACE_PURGED_ACTION,
                AuditLog.resource == f"workspace:{gone_id}",
            )
        )
    ).scalar_one()
    assert audit.user_id == "operator"
    assert audit.user_metadata == {
        "previous_owner_id": target,
        "soft_deleted": True,
        "contexts": 2,
        "memories": 4,
        "billing_contract_active": False,
    }
    assert [call.args for call in vectors.await_args_list] == [(str(live_id), str(mine))]
    assert await db_session.get(Context, mine) is None
    # Untouched: the other owner's soft-deleted workspace and its context.
    foreign_row = await db_session.get(Workspace, foreign_id)
    assert foreign_row is not None and foreign_row.owner_user_id == other
    assert await db_session.get(Context, foreign_ctx) is not None

    # A re-run finds no user and changes nothing.
    with pytest.raises(HTTPException) as again:
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert again.value.status_code == 404
    await db_session.rollback()
    assert await db_session.get(Workspace, foreign_id) is not None


@pytest.mark.asyncio
async def test_a_purged_workspace_still_marked_as_billed_is_flagged(
    connector_world, db_session, purge_audits, vectors
):
    """A soft-deleted workspace never blocks the deletion (#1940), but one
    deleted before that rule may still be charged: its audit row says so."""
    from models.auth import ENTITLEMENT_SOURCE_EXTERNAL_BILLING

    world = connector_world
    target = await world.user("departing")
    gone = await world.workspace(target)
    gone_id = gone.id
    purge_audits.append(gone_id)
    await db_session.execute(
        update(Workspace)
        .where(Workspace.id == gone_id)
        .values(
            deleted_at=utcnow(),
            plan_name="pro",
            entitlement_source=ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
        )
    )
    await db_session.commit()

    await delete_user(target, {"user_id": "operator"}, db_session)

    db_session.expire_all()
    assert await db_session.get(Workspace, gone_id) is None
    metadata = await db_session.scalar(
        select(AuditLog.user_metadata).where(
            AuditLog.action == WORKSPACE_PURGED_ACTION,
            AuditLog.resource == f"workspace:{gone_id}",
        )
    )
    assert metadata["billing_contract_active"] is True


@pytest.mark.asyncio
async def test_a_live_owned_workspace_still_needs_a_successor(
    connector_world, db_session, purge_audits, vectors
):
    """Only the live workspace is listed: the soft-deleted one and the
    contexts the user created in it need no successor."""
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    live = await world.workspace(target, **{successor: WorkspaceRole.ADMIN})
    gone = await world.workspace(target, **{successor: WorkspaceRole.ADMIN})
    live_id, gone_id = live.id, gone.id
    purge_audits.append(gone_id)
    gone_ctx = await _context(db_session, gone_id, target)
    await _soft_delete(db_session, gone_id)
    await db_session.commit()

    with pytest.raises(HTTPException) as refused:
        await delete_user(target, {"user_id": "operator"}, db_session)
    assert refused.value.status_code == 409
    assert refused.value.detail["workspace_ids"] == [str(live_id)]
    assert refused.value.detail["context_ids"] == []
    await db_session.rollback()
    assert await db_session.get(Workspace, gone_id) is not None
    assert await db_session.get(Context, gone_ctx) is not None

    await delete_user(
        target,
        {"user_id": "operator"},
        db_session,
        UserDeleteRequest(workspace_transfers={live_id: successor}),
    )
    db_session.expire_all()
    assert (await db_session.get(Workspace, live_id)).owner_user_id == successor
    assert await db_session.get(Workspace, gone_id) is None
    assert await db_session.get(Context, gone_ctx) is None


@pytest.mark.asyncio
async def test_a_successor_for_a_soft_deleted_workspace_is_refused(connector_world, db_session):
    world = connector_world
    target = await world.user("departing")
    successor = await world.user("successor")
    gone = await world.workspace(target, **{successor: WorkspaceRole.ADMIN})
    gone_id = gone.id
    gone_ctx = await _context(db_session, gone_id, target)
    await _soft_delete(db_session, gone_id)
    await db_session.commit()

    for request in (
        UserDeleteRequest(workspace_transfers={gone_id: successor}),
        UserDeleteRequest(context_transfers={gone_ctx: successor}),
    ):
        with pytest.raises(HTTPException) as refused:
            await delete_user(target, {"user_id": "operator"}, db_session, request)
        assert refused.value.status_code == 409
        await db_session.rollback()
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) == target
    assert (await db_session.get(Workspace, gone_id)).owner_user_id == target


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_external_stores")
async def test_erasure_takes_a_soft_deleted_workspace_with_members_and_no_admin(
    connector_world, db_session
):
    """A soft-deleted workspace never blocks erasure for want of an admin to
    take it over: it is hard-deleted with the sole-owned ones."""
    from services.account_erasure_service import AccountErasureService

    world = connector_world
    target = await world.user("erased")
    member = await world.user("member")
    gone = await world.workspace(target, **{member: WorkspaceRole.MEMBER})
    gone_id = gone.id
    for actor in (target, member):
        record_operation(
            db_session,
            operation="member.added",
            actor_user_id=actor,
            workspace_id=gone_id,
            target_id=member,
            subject_user_id=member,
        )
    await _soft_delete(db_session, gone_id)
    await db_session.commit()

    await AccountErasureService(db_session)._check_no_blocking_workspace_transfers(target)
    await world.erase(target)

    assert await db_session.get(Workspace, gone_id) is None
    assert await _count(db_session, OperationHistory, OperationHistory.workspace_id == gone_id) == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_external_stores")
async def test_erasure_deletes_the_contextless_memories_of_the_workspaces_it_removes(
    connector_world, db_session
):
    """``memories.workspace_id`` has no foreign key, so the workspace cascade
    leaves a memory with no context behind: erasure deletes those of the
    sole-owned and soft-deleted workspaces it removes, as user deletion does
    (#2445). The subject's own keep their vector-erasure scope (captured
    before the delete); a handed-on workspace keeps its rows, the subject's
    own scrubbed rather than deleted."""
    from models.vector_erasure import VectorErasureJob
    from services.erasure_details import erasure_pseudonym

    world = connector_world
    target = await world.user("erased")
    other = await world.user("other")
    sole_id = (await world.workspace(target)).id
    gone_id = (await world.workspace(target)).id
    shared_id = (await world.workspace(target, **{other: WorkspaceRole.ADMIN})).id
    removed = {
        await _memory(db_session, ws_id, None, author): author
        for ws_id in (sole_id, gone_id)
        for author in (target, other)
    }
    kept = await _memory(db_session, shared_id, None, other)
    scrubbed = await _memory(db_session, shared_id, None, target)
    await _soft_delete(db_session, gone_id)
    await db_session.commit()

    try:
        await world.erase(target)

        assert await _count(db_session, Memory, Memory.id.in_(list(removed))) == 0
        assert await _count(db_session, Workspace, Workspace.id.in_([sole_id, gone_id])) == 0
        assert (await db_session.get(Workspace, shared_id)).owner_user_id == other
        assert (await db_session.get(Memory, kept)).user_id == other
        assert (await db_session.get(Memory, scrubbed)).user_id == erasure_pseudonym(target)
        own = {memory_id for memory_id, author in removed.items() if author == target}
        scoped = await db_session.scalars(
            select(VectorErasureJob.memory_id).where(VectorErasureJob.memory_id.in_(own))
        )
        assert set(scoped) == own
    finally:
        # no foreign key takes them with the workspaces the fixture deletes
        await db_session.rollback()
        await db_session.execute(delete(Memory).where(Memory.id.in_([*removed, kept, scrubbed])))
        await db_session.commit()


@pytest.mark.asyncio
async def test_create_admin_sees_a_workspace_left_under_its_user_id(db_session):
    """``create_admin`` refuses a login ID whose ``local:`` user_id a leftover
    workspace (here a soft-deleted one) still names as its owner."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from cli.create_admin import _leftover_owned_workspaces
    from config.database import to_sync_database_url
    from tests.conftest import TEST_DATABASE_URL

    owner = f"local:gone-{uuid4().hex[:8]}"
    leftover = Workspace(
        id=uuid4(),
        name="leftover",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=500,
        weekly_api_limit=2500,
        deleted_at=utcnow(),
    )
    db_session.add(leftover)
    await db_session.commit()
    engine = create_engine(to_sync_database_url(TEST_DATABASE_URL))
    try:
        with Session(engine) as sync_db:
            assert _leftover_owned_workspaces(sync_db, owner) == 1
            assert _leftover_owned_workspaces(sync_db, f"{owner}-other") == 0
            # An existing account is not a leftover's owner.
            sync_db.add(User(user_id=owner, email=f"{owner[6:]}@local"))
            sync_db.flush()
            assert _leftover_owned_workspaces(sync_db, owner) == 0
            sync_db.rollback()
    finally:
        engine.dispose()
        await db_session.execute(delete(Workspace).where(Workspace.id == leftover.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_a_locked_soft_deleted_workspace_is_waited_for(
    connector_world, db_session, purge_audits, vectors
):
    """The deleted-workspace file repair locks a soft-deleted workspace row
    while it works; the deletion waits for it instead of failing NOWAIT
    (which, in the CLI, would come after its transfers had committed)."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    world = connector_world
    target = await world.user("departing")
    gone = await world.workspace(target)
    gone_id = gone.id
    purge_audits.append(gone_id)
    await _soft_delete(db_session, gone_id)
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async with maker() as holder:
        await holder.execute(select(Workspace.id).where(Workspace.id == gone_id).with_for_update())

        async def remove():
            async with maker() as db:
                return await delete_user(target, {"user_id": "operator"}, db)

        task = asyncio.create_task(remove())
        async with maker() as observer:
            for _ in range(500):
                waiting = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype IN "
                        "('tuple', 'transactionid')"
                    )
                )
                if waiting or task.done():
                    break
                await asyncio.sleep(0.01)
        assert not task.done(), "the deletion did not wait for the row lock"
        await holder.rollback()
    response = await asyncio.wait_for(task, 15)

    assert [ws["id"] for ws in response["purged_workspaces"]] == [str(gone_id)]


@pytest.mark.asyncio
async def test_purge_counts_cover_only_each_workspace_s_own_rows(connector_world, db_session):
    """The counts in the prompt and the audit row are per workspace: another
    workspace's contexts and memories never add to them."""
    from services.user_deletion_service import workspace_purge_counts

    world = connector_world
    owner = await world.user("owner")
    counted = await world.workspace(owner)
    other = await world.workspace(owner)
    counted_id, other_id = counted.id, other.id
    mine = await _context(db_session, counted_id, owner)
    for ctx in (mine, mine, None):
        await _memory(db_session, counted_id, ctx, owner)
    theirs = await _context(db_session, other_id, owner)
    for ctx in (theirs, theirs, theirs, theirs, None):
        await _memory(db_session, other_id, ctx, owner)
    await db_session.flush()

    one = await workspace_purge_counts(db_session, Workspace.id == counted_id)
    both = await workspace_purge_counts(db_session, Workspace.id.in_([counted_id, other_id]))

    assert [(ws.contexts, ws.memories) for ws in one] == [(1, 3)]
    assert {ws.id: (ws.contexts, ws.memories) for ws in both} == {
        counted_id: (1, 3),
        other_id: (1, 5),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["admin", "cli", "erasure"])
@pytest.mark.usefixtures("patched_external_stores")
async def test_every_user_deletion_path_scrubs_retained_history(connector_world, db_session, path):
    from services.erasure_details import erasure_pseudonym
    from services.user_deletion_service import delete_user_rows

    world = connector_world
    target = await world.user("history-departing")
    owner = await world.user("history-owner")
    workspace = await world.workspace(owner, **{target: WorkspaceRole.MEMBER})
    workspace_id = workspace.id
    member = record_operation(
        db_session,
        operation="member.added",
        actor_user_id=target,
        workspace_id=workspace_id,
        target_id=target,
        subject_user_id=target,
    )
    member.context_owner_id = target
    credential = record_operation(
        db_session,
        operation="credential.issued",
        actor_user_id=target,
        workspace_id=workspace_id,
        target_id=target,  # A non-member target is opaque, not an account ID.
        subject_user_id=target,
    )
    member_id, credential_id = member.id, credential.id
    await db_session.commit()
    try:
        if path == "admin":
            await delete_user(target, {"user_id": "operator"}, db_session)
        elif path == "cli":
            await delete_user_rows(db_session, target, preserve_created_contexts=True)
            await db_session.commit()
        else:
            await world.erase(target)
        db_session.expire_all()
        pseudonym = erasure_pseudonym(target)
        stored_member = await db_session.get(OperationHistory, member_id)
        stored_credential = await db_session.get(OperationHistory, credential_id)
        assert stored_member.actor_user_id == pseudonym
        assert stored_member.subject_user_id == pseudonym
        assert stored_member.context_owner_id == pseudonym
        assert stored_member.target_id == pseudonym
        assert stored_credential.actor_user_id == pseudonym
        assert stored_credential.subject_user_id == pseudonym
        assert stored_credential.target_id == target
        assert await db_session.get(Workspace, workspace_id) is not None
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(OperationHistory).where(OperationHistory.workspace_id == workspace_id)
        )
        await db_session.commit()
