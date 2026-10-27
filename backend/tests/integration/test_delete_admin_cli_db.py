"""``cli.delete_admin`` keeps the admin's connector keys, on real Postgres (#2352).

The password admin P is an admin in a workspace O owns and provisioned a
connector there. Deleting P (the "other users exist" branch) hands that
connector's live write key to O — it still verifies, now as O — and deletes
P's other keys. Other users' keys are untouched.

#2371: P's own workspace is transferred to ``--transfer-to`` first, so its
connector key follows the new owner too; without ``--transfer-to``, or when
the target is not a member, nothing changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from cli import delete_admin
from config.database import to_sync_database_url
from models.auth import AuditLog, User, Workspace
from services.workspace_ownership_service import OWNERSHIP_TRANSFER_ACTION
from tests.conftest import TEST_DATABASE_URL

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


def _run_cli(transfer_to: str | None = None) -> None:
    with (
        patch.object(
            delete_admin,
            "get_sync_database_url",
            return_value=to_sync_database_url(TEST_DATABASE_URL),
        ),
        patch.object(delete_admin, "require_settings"),
        patch("builtins.input", side_effect=["y"]),
    ):
        delete_admin.delete_admin(transfer_to)


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


async def _drop_transfer_audits(db_session: AsyncSession, workspace_ids) -> None:
    await db_session.rollback()
    await db_session.execute(
        delete(AuditLog).where(
            AuditLog.action == OWNERSHIP_TRANSFER_ACTION,
            AuditLog.resource.in_([f"workspace:{ws_id}" for ws_id in workspace_ids]),
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_cli_hands_the_admins_connector_key_to_the_owner(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    from auth.api_keys import APIKeyManager

    world = connector_world
    admin_id = await _password_admin(db_session, world)
    # a second users row, so the CLI takes the "other users exist" branch
    owner = await world.user("owner")
    member = f"member_{uuid4().hex[:8]}"

    own_ws = await world.workspace(admin_id, **{owner: WorkspaceRole.MEMBER})
    own_connector_key = (
        await _live_connector_key(
            db_session, (await world.connector(own_ws, by=admin_id)).connector.id
        )
    ).id
    own_personal = await world.personal_key(admin_id, own_ws)

    ws = await world.workspace(
        owner, **{admin_id: WorkspaceRole.ADMIN, member: WorkspaceRole.ADMIN}
    )
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
    own_ws_id, epoch_before = own_ws.id, own_ws.ownership_epoch
    await db_session.commit()

    try:
        await asyncio.to_thread(_run_cli, owner)
        db_session.expire_all()

        out = capsys.readouterr().out
        assert f"Workspaces transferred to {owner}: 1" in out
        assert "Connector write keys handed to their workspace owners: 2" in out
        transferred = await db_session.get(Workspace, own_ws_id)
        assert transferred is not None
        assert transferred.owner_user_id == owner
        assert transferred.ownership_epoch == epoch_before + 1
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
    finally:
        await _drop_transfer_audits(db_session, [own_ws_id])

    assert (
        await db_session.execute(select(User).where(User.user_id == admin_id))
    ).scalar_one_or_none() is None
    after = await _key_rows(db_session, [moved_id, *bystanders])
    assert after[moved_id]["user_id"] == owner
    assert _without_owner(after[moved_id]) == _without_owner(before[moved_id])
    assert {k: after[k] for k in bystanders} == {k: before[k] for k in bystanders}
    # #2371: the own workspace's connector key followed the transfer
    own_after = await _key_rows(db_session, [own_connector_key])
    assert own_after[own_connector_key]["user_id"] == owner
    assert await _key_rows(db_session, [own_personal, personal]) == {}

    verified = await APIKeyManager(db_session).verify_key(provisioned.plaintext_kmc_api_key)
    assert verified is not None
    assert verified.user_id == owner
    assert verified.is_connector_key is True


async def _refused(db_session: AsyncSession, admin_id: str, transfer_to: str | None, capsys):
    with pytest.raises(SystemExit) as exc_info:
        await asyncio.to_thread(_run_cli, transfer_to)
    assert exc_info.value.code == 1
    db_session.expire_all()
    assert (
        await db_session.execute(select(User).where(User.user_id == admin_id))
    ).scalar_one_or_none() is not None
    return capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_refuses_without_transfer_to_and_changes_nothing(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    own_ws = await world.workspace(admin_id, **{other: WorkspaceRole.ADMIN})
    key_id = (
        await _live_connector_key(
            db_session, (await world.connector(own_ws, by=admin_id)).connector.id
        )
    ).id
    before = await _key_rows(db_session, [key_id])
    own_ws_id = own_ws.id
    await db_session.commit()

    out = await _refused(db_session, admin_id, None, capsys)

    assert str(own_ws_id) in out
    assert "--transfer-to" in out
    assert (await db_session.get(Workspace, own_ws_id)).owner_user_id == admin_id
    assert await _key_rows(db_session, [key_id]) == before


@pytest.mark.asyncio
async def test_cli_refuses_a_target_outside_an_owned_workspace(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    other = await world.user("other")
    shared = await world.workspace(admin_id, **{other: WorkspaceRole.MEMBER})
    solo = await world.workspace(admin_id)
    shared_id, solo_id = shared.id, solo.id
    await db_session.commit()

    out = await _refused(db_session, admin_id, other, capsys)

    assert str(solo_id) in out
    assert str(shared_id) not in out.split("is not a member of:")[1]
    for ws_id in (shared_id, solo_id):
        assert (await db_session.get(Workspace, ws_id)).owner_user_id == admin_id


@pytest.mark.asyncio
async def test_cli_refuses_an_unknown_or_self_target(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    world = connector_world
    admin_id = await _password_admin(db_session, world)
    await world.user("other")
    await world.workspace(admin_id)
    await db_session.commit()

    for target in (f"ghost_{uuid4().hex[:8]}", admin_id):
        out = await _refused(db_session, admin_id, target, capsys)
        assert "no such user" in out
