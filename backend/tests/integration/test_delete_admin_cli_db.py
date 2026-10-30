"""``cli.delete_admin`` on real Postgres (#2352, #2371).

The password admin P is an admin in a workspace O owns and provisioned a
connector there. Deleting P (the "other users exist" branch) hands that
connector's live write key to O — it still verifies, now as O — and deletes
P's other keys. Other users' keys are untouched.

#2371: a live workspace P owns that other users are in goes to
``--transfer-to`` (one of its members) first, so its connector keys follow
the new owner too; one only P is in is deleted with P. Without
``--transfer-to``, with an unknown, ambiguous or non-member target, nothing
changes. P's memberships are deleted. #2406: a soft-deleted workspace P owns
is hard-deleted with P, everything in it included.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from cli import delete_admin
from config.database import to_sync_database_url
from models.auth import AuditLog, User, Workspace, WorkspaceMember
from services.workspace_ownership_service import OWNERSHIP_TRANSFER_ACTION
from tests.conftest import TEST_DATABASE_URL
from utils.datetime import utcnow

from ._connector_world import _key_rows, _live_connector_key, _without_owner


@pytest_asyncio.fixture
async def sole_password_admin(db_session: AsyncSession) -> AsyncIterator[None]:
    """The CLI picks *the* password admin: demote any other one for the test
    and restore it afterwards."""
    others = list(
        (
            await db_session.execute(
                select(User.user_id).where(User.auth_method == "password", User.role == "admin")
            )
        )
        .scalars()
        .all()
    )
    if others:
        await db_session.execute(update(User).where(User.user_id.in_(others)).values(role="user"))
        await db_session.commit()
    yield
    await db_session.rollback()
    if others:
        await db_session.execute(update(User).where(User.user_id.in_(others)).values(role="admin"))
        await db_session.commit()


@pytest_asyncio.fixture
async def transfer_audits(db_session: AsyncSession) -> AsyncIterator[list]:
    """Workspace ids whose transfer audit rows are deleted afterwards."""
    workspace_ids: list = []
    yield workspace_ids
    await db_session.rollback()
    await db_session.execute(
        delete(AuditLog).where(
            AuditLog.action == OWNERSHIP_TRANSFER_ACTION,
            AuditLog.resource.in_([f"workspace:{ws_id}" for ws_id in workspace_ids]),
        )
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def purge_audits(db_session: AsyncSession) -> AsyncIterator[list]:
    """Workspace ids whose purge audit rows are deleted afterwards (#2406)."""
    from services.user_deletion_service import WORKSPACE_PURGED_ACTION

    workspace_ids: list = []
    yield workspace_ids
    await db_session.rollback()
    await db_session.execute(
        delete(AuditLog).where(
            AuditLog.action == WORKSPACE_PURGED_ACTION,
            AuditLog.resource.in_([f"workspace:{ws_id}" for ws_id in workspace_ids]),
        )
    )
    await db_session.commit()


async def _run(
    db_session: AsyncSession, transfer_to: str | None = None, ask=lambda prompt: True
) -> int:
    await db_session.commit()
    code = await delete_admin.delete_password_admin(db_session, transfer_to=transfer_to, ask=ask)
    await db_session.rollback()
    db_session.expire_all()
    return code


async def _password_admin(db_session: AsyncSession, world) -> str:
    suffix = uuid4().hex[:10]
    admin_id = f"local:cli-{suffix}"
    db_session.add(
        User(
            user_id=admin_id,
            email=f"cli-{suffix}@local",
            name="CLI admin",
            role="admin",
            is_initial_admin=False,
            auth_method="password",
            login_id=f"cli-{suffix}",
        )
    )
    await db_session.flush()
    world.targets.append(admin_id)
    return admin_id


@pytest.mark.asyncio
async def test_cli_accepts_a_different_successor_for_each_workspace(
    db_session, connector_world, sole_password_admin, transfer_audits
):
    from models.auth import Context
    from models.resource import WorkspaceConnector

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    first, second = await world.user("first"), await world.user("second")
    one = await world.workspace(admin_id, **{first: "admin"})
    two = await world.workspace(admin_id, **{second: "admin"})
    ids = [one.id, two.id]
    transfer_audits.extend(ids)
    connector = (await world.connector(one, by=admin_id)).connector
    connector_id, context_id = connector.id, connector.context_id
    await db_session.commit()
    assert (
        await delete_admin.delete_password_admin(
            db_session,
            transfer_to=None,
            ask=lambda _: True,
            workspace_transfers={ids[0]: first, ids[1]: second},
        )
        == 0
    )
    db_session.expire_all()
    assert await _owner(db_session, ids[0]) == first
    assert await _owner(db_session, ids[1]) == second
    assert (
        await db_session.scalar(select(Context.created_by).where(Context.id == context_id)) == first
    )
    assert (
        await db_session.scalar(
            select(WorkspaceConnector.context_id).where(WorkspaceConnector.id == connector_id)
        )
        == context_id
    )


async def _exists(db_session: AsyncSession, user_id: str) -> bool:
    row = await db_session.execute(select(User.user_id).where(User.user_id == user_id))
    return row.scalar_one_or_none() is not None


async def _owner(db_session: AsyncSession, workspace_id) -> str | None:
    row = await db_session.execute(
        select(Workspace.owner_user_id).where(Workspace.id == workspace_id)
    )
    return row.scalar_one_or_none()


async def _memberships(db_session: AsyncSession, user_id: str) -> dict:
    rows = await db_session.execute(
        select(WorkspaceMember.workspace_id, WorkspaceMember.role).where(
            WorkspaceMember.user_id == user_id
        )
    )
    return dict(rows.tuples().all())


@pytest.mark.asyncio
async def test_cli_hands_the_admins_workspace_and_connector_keys_over(
    db_session: AsyncSession, connector_world, sole_password_admin, transfer_audits, capsys
) -> None:
    from auth.api_keys import APIKeyManager

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    owner = await world.user("owner")
    member = f"member_{uuid4().hex[:8]}"

    own_ws = await world.workspace(admin_id, **{owner: WorkspaceRole.MEMBER})
    own_ws_id, epoch_before = own_ws.id, own_ws.ownership_epoch
    transfer_audits.append(own_ws_id)
    own_connector_key = (
        await _live_connector_key(
            db_session, (await world.connector(own_ws, by=admin_id)).connector.id
        )
    ).id
    own_personal = await world.personal_key(admin_id, own_ws)

    ws = await world.workspace(
        owner, **{admin_id: WorkspaceRole.ADMIN, member: WorkspaceRole.ADMIN}
    )
    ws_id = ws.id
    provisioned = await world.connector(ws, by=admin_id)
    moved_id = (await _live_connector_key(db_session, provisioned.connector.id)).id
    personal = await world.personal_key(admin_id, ws)
    bystanders = [
        (
            await _live_connector_key(
                db_session, (await world.connector(ws, by=member)).connector.id
            )
        ).id,
        await world.personal_key(owner, ws),
        await world.personal_key(member, ws),
    ]
    before = await _key_rows(db_session, [moved_id, own_connector_key, *bystanders])
    assert len(before) == 5

    assert await _run(db_session, owner) == 0

    out = capsys.readouterr().out
    assert f"Workspaces transferred to {owner}: 1" in out
    assert "Connector write keys handed to their workspace owners: 2" in out
    assert not await _exists(db_session, admin_id)
    assert await _memberships(db_session, admin_id) == {}

    transferred = await db_session.get(Workspace, own_ws_id)
    assert transferred is not None
    assert transferred.owner_user_id == owner
    assert transferred.ownership_epoch == epoch_before + 1
    assert (await _memberships(db_session, owner))[own_ws_id] == WorkspaceRole.OWNER
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == OWNERSHIP_TRANSFER_ACTION,
                    AuditLog.resource == f"workspace:{own_ws_id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [a.user_metadata["new_owner_id"] for a in audits] == [owner]

    after = await _key_rows(db_session, [moved_id, own_connector_key, *bystanders])
    assert after[moved_id]["user_id"] == owner
    assert _without_owner(after[moved_id]) == _without_owner(before[moved_id])
    assert after[own_connector_key]["user_id"] == owner
    assert {k: after[k] for k in bystanders} == {k: before[k] for k in bystanders}
    assert await _key_rows(db_session, [own_personal, personal]) == {}
    assert await _owner(db_session, ws_id) == owner

    verified = await APIKeyManager(db_session).verify_key(provisioned.plaintext_kmc_api_key)
    assert verified is not None
    assert verified.user_id == owner
    assert verified.is_connector_key is True


@pytest.mark.asyncio
async def test_a_workspace_only_the_admin_is_in_goes_with_it(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys, purge_audits
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    solo = await world.workspace(admin_id)
    solo_id = solo.id
    purge_audits.append(solo_id)
    elsewhere = await world.workspace(other, **{admin_id: WorkspaceRole.MEMBER})
    elsewhere_id = elsewhere.id

    # nothing shared is owned, so no --transfer-to is needed
    assert await _run(db_session) == 0

    assert str(solo_id) in capsys.readouterr().out
    assert not await _exists(db_session, admin_id)
    assert await db_session.get(Workspace, solo_id) is None
    assert await _memberships(db_session, admin_id) == {}
    assert await _owner(db_session, elsewhere_id) == other


@pytest.mark.asyncio
async def test_soft_deleted_workspaces_are_deleted_with_the_admin(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys, purge_audits
) -> None:
    from services.user_deletion_service import WORKSPACE_PURGED_ACTION

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    owner = await world.user("owner")
    gone = await world.workspace(admin_id, **{owner: WorkspaceRole.MEMBER})
    gone_id = gone.id
    purge_audits.append(gone_id)
    await db_session.execute(
        update(Workspace).where(Workspace.id == gone_id).values(deleted_at=utcnow())
    )

    assert await _run(db_session) == 0

    assert f"({gone_id}): 0 contexts, 0 memories" in capsys.readouterr().out
    assert not await _exists(db_session, admin_id)
    assert await db_session.get(Workspace, gone_id) is None
    assert gone_id not in await _memberships(db_session, owner)
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == WORKSPACE_PURGED_ACTION,
                AuditLog.resource == f"workspace:{gone_id}",
            )
        )
    ).scalar_one()
    assert (audit.user_email, audit.user_id) == ("cli@local", admin_id)
    assert audit.user_metadata["soft_deleted"] is True


async def _refused(db_session: AsyncSession, admin_id: str, transfer_to: str | None, capsys):
    assert await _run(db_session, transfer_to) == 1
    assert await _exists(db_session, admin_id)
    return capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_refuses_without_transfer_to_and_changes_nothing(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    own_ws = await world.workspace(admin_id, **{other: WorkspaceRole.ADMIN})
    own_ws_id = own_ws.id
    key_id = (
        await _live_connector_key(
            db_session, (await world.connector(own_ws, by=admin_id)).connector.id
        )
    ).id
    before = await _key_rows(db_session, [key_id])

    out = await _refused(db_session, admin_id, None, capsys)

    assert str(own_ws_id) in out
    assert "--transfer-to" in out
    assert await _owner(db_session, own_ws_id) == admin_id
    assert await _key_rows(db_session, [key_id]) == before


@pytest.mark.asyncio
async def test_cli_refuses_a_target_outside_a_shared_workspace(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    target = await world.user("target")
    other = await world.user("other")
    with_target = await world.workspace(admin_id, **{target: WorkspaceRole.MEMBER})
    without = await world.workspace(admin_id, **{other: WorkspaceRole.MEMBER})
    solo = await world.workspace(admin_id)
    ids = [with_target.id, without.id, solo.id]

    out = await _refused(db_session, admin_id, target, capsys)

    refused = out.split("is not a member of")[1]
    assert str(ids[1]) in refused
    assert str(ids[0]) not in refused
    assert str(ids[2]) not in refused  # only the admin is in it: no target needed
    for ws_id in ids:
        assert await _owner(db_session, ws_id) == admin_id


@pytest.mark.asyncio
async def test_cli_refuses_an_unknown_self_or_ambiguous_target(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    first = await world.user("first")
    second = await world.user("second")
    shared = f"shared-{uuid4().hex[:8]}@example.com"
    await db_session.execute(update(User).where(User.user_id == first).values(login_id=shared))
    await db_session.execute(
        update(User).where(User.user_id == second).values(email=shared.upper())
    )
    await world.workspace(admin_id, **{first: WorkspaceRole.MEMBER})

    for target in (f"ghost_{uuid4().hex[:8]}", admin_id):
        assert "no such user" in await _refused(db_session, admin_id, target, capsys)
    out = await _refused(db_session, admin_id, shared, capsys)
    assert "matches 2 users" in out
    assert first in out and second in out


@pytest.mark.asyncio
async def test_email_target_matches_case_insensitively(
    db_session: AsyncSession, connector_world, sole_password_admin, transfer_audits
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    shared = await world.workspace(admin_id, **{other: WorkspaceRole.MEMBER})
    shared_id = shared.id
    transfer_audits.append(shared_id)

    assert await _run(db_session, f" {other}@EXAMPLE.com") == 0

    assert await _owner(db_session, shared_id) == other


def _join_while_prompting(workspace_id, user_id: str):
    """An ``ask`` that answers yes after ``user_id`` joined the workspace on
    another connection, as a web session would while the prompt waits."""

    def ask(prompt: str) -> bool:
        engine = create_engine(to_sync_database_url(TEST_DATABASE_URL))
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO workspace_members (workspace_id, user_id, role) "
                        "VALUES (:ws, :uid, 'member')"
                    ),
                    {"ws": workspace_id, "uid": user_id},
                )
        finally:
            engine.dispose()
        return True

    return ask


@pytest.mark.asyncio
async def test_a_member_joining_while_the_prompt_waits_stops_the_delete_and_a_rerun_finishes(
    db_session: AsyncSession, connector_world, sole_password_admin, transfer_audits, capsys
) -> None:
    """The re-check stops the command before anything is written; the re-run
    hands over both workspaces (the target is now in each) and deletes the
    admin."""
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    target = await world.user("target")
    shared = await world.workspace(admin_id, **{target: WorkspaceRole.MEMBER})
    solo = await world.workspace(admin_id)
    shared_id, solo_id = shared.id, solo.id
    transfer_audits.extend([shared_id, solo_id])

    code = await _run(db_session, target, ask=_join_while_prompting(solo_id, target))

    assert code == 1
    assert "changed while the prompt waited" in capsys.readouterr().out
    assert await _exists(db_session, admin_id)
    assert await _owner(db_session, shared_id) == admin_id
    assert await _owner(db_session, solo_id) == admin_id

    assert await _run(db_session, target) == 0

    assert not await _exists(db_session, admin_id)
    assert await _owner(db_session, shared_id) == target
    assert await _owner(db_session, solo_id) == target


@pytest.mark.asyncio
async def test_cli_notifies_committed_transfer_when_later_successor_has_left(
    db_session, connector_world, transfer_audits
):
    from unittest.mock import AsyncMock, patch

    from utils.exceptions import BadRequestError

    world = connector_world
    owner = await world.user("owner")
    target = await world.user("successor")
    first = await world.workspace(owner, **{target: "admin"})
    second = await world.workspace(owner)
    first_id, second_id = first.id, second.id
    transfer_audits.extend([first_id, second_id])
    workspaces = [
        delete_admin._Ws(first.id, first.name),
        delete_admin._Ws(second.id, second.name),
    ]
    await db_session.commit()
    notified = AsyncMock()
    with patch("services.ownership_notifications.notify_new_owner_best_effort", notified):
        with pytest.raises(BadRequestError):
            await delete_admin._transfer(
                db_session, workspaces, owner, {first_id: target, second_id: target}
            )
    await db_session.rollback()
    assert await _owner(db_session, first_id) == target
    assert await _owner(db_session, second_id) == owner
    notified.assert_awaited_once_with(db_session, first_id, target)


@pytest.mark.asyncio
async def test_contexts_in_a_soft_deleted_owned_workspace_are_deleted_with_it(
    db_session: AsyncSession, connector_world, sole_password_admin, purge_audits
) -> None:
    """#2406: a soft-deleted workspace the admin owns goes with it, with its
    shared contexts and other authors' memories: none of them points at the
    deleted admin afterwards, and none passes to anyone else."""
    from models.auth import Context
    from models.memory import Memory

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    owner = await world.user("owner")
    gone = await world.workspace(admin_id, **{owner: WorkspaceRole.MEMBER})
    gone_id = gone.id
    kept = Context(
        id=uuid4(), name="kept", workspace_id=gone_id, created_by=admin_id, is_private=False
    )
    mine = Context(id=uuid4(), name="mine", workspace_id=gone_id, created_by=admin_id)
    db_session.add_all([kept, mine])
    await db_session.flush()
    theirs = Memory(
        id=uuid4(),
        user_id=owner,
        workspace_id=gone_id,
        context_id=kept.id,
        content="theirs",
        summary="theirs",
        type="note",
        client="test",
    )
    db_session.add(theirs)
    kept_id, mine_id, theirs_id = kept.id, mine.id, theirs.id
    purge_audits.append(gone_id)
    await db_session.execute(
        update(Workspace).where(Workspace.id == gone_id).values(deleted_at=utcnow())
    )

    assert await _run(db_session) == 0

    assert not await _exists(db_session, admin_id)
    assert await db_session.get(Workspace, gone_id) is None
    assert await db_session.scalar(select(Context.id).where(Context.id == kept_id)) is None
    assert await db_session.scalar(select(Memory.id).where(Memory.id == theirs_id)) is None
    assert await db_session.scalar(select(Context.id).where(Context.id == mine_id)) is None


@pytest.mark.asyncio
async def test_a_private_context_elsewhere_is_deleted_and_a_shared_one_reowned(
    db_session: AsyncSession, connector_world, sole_password_admin
) -> None:
    """#2417: only a shared context passes to the surviving workspace owner; a
    private one nobody linked wrote in goes with the admin."""
    from models.auth import Context

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    elsewhere = await world.workspace(other, **{admin_id: WorkspaceRole.ADMIN})
    private = Context(
        id=uuid4(), name="mine", workspace_id=elsewhere.id, created_by=admin_id, is_private=True
    )
    shared = Context(
        id=uuid4(), name="ours", workspace_id=elsewhere.id, created_by=admin_id, is_private=False
    )
    db_session.add_all([private, shared])
    await db_session.flush()
    private_id, shared_id = private.id, shared.id

    assert await _run(db_session) == 0

    assert await db_session.scalar(select(Context.id).where(Context.id == private_id)) is None
    assert (
        await db_session.scalar(select(Context.created_by).where(Context.id == shared_id)) == other
    )


@pytest.mark.asyncio
async def test_quota_is_released_for_surviving_workspaces_only(
    db_session: AsyncSession, connector_world, sole_password_admin
) -> None:
    """#2417: a hard-deleted workspace has no quota counter left to release."""
    from unittest.mock import AsyncMock, patch

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    solo = await world.workspace(admin_id)
    elsewhere = await world.workspace(other, **{admin_id: WorkspaceRole.ADMIN})
    solo_id, elsewhere_id = solo.id, elsewhere.id

    release = AsyncMock()
    with (
        patch("services.user_deletion_service.FileStorageService") as storage,
        patch("services.storage_quota_service.release_storage_bytes", release),
    ):
        storage.return_value.purge_files_for_contexts = AsyncMock(
            return_value={solo_id: 10, elsewhere_id: 20}
        )
        assert await _run(db_session) == 0

    release.assert_awaited_once_with(workspace_id=elsewhere_id, size_bytes=20)


@pytest.mark.asyncio
async def test_a_private_context_someone_else_wrote_in_passes_to_the_owner(
    db_session: AsyncSession, connector_world, sole_password_admin
) -> None:
    """#2417: another account's memories make a private context more than the
    admin's own, so the CLI hands it to the workspace owner instead."""
    from models.auth import Context
    from models.memory import Memory

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    elsewhere = await world.workspace(other, **{admin_id: WorkspaceRole.ADMIN})
    shared_by_use = Context(
        id=uuid4(), name="ours", workspace_id=elsewhere.id, created_by=admin_id, is_private=True
    )
    db_session.add(shared_by_use)
    await db_session.flush()
    theirs = Memory(
        id=uuid4(),
        user_id=other,
        workspace_id=elsewhere.id,
        context_id=shared_by_use.id,
        content="theirs",
        summary="theirs",
        type="note",
        client="test",
    )
    db_session.add(theirs)
    ctx_id, theirs_id = shared_by_use.id, theirs.id

    assert await _run(db_session) == 0

    assert await db_session.scalar(select(Context.created_by).where(Context.id == ctx_id)) == other
    assert await db_session.get(Memory, theirs_id) is not None
