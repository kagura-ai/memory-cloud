"""``cli.delete_admin`` keeps the admin's connector keys, on real Postgres (#2352).

The password admin P is an admin in a workspace O owns and provisioned a
connector there. Deleting P (the "other users exist" branch) hands that
connector's live write key to O — it still verifies, now as O — and deletes
P's other keys, the connector key of P's own workspace included. Other users'
keys are untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from cli import delete_admin
from config.database import to_sync_database_url
from models.auth import User
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


def _run_cli() -> None:
    with (
        patch.object(
            delete_admin,
            "get_sync_database_url",
            return_value=to_sync_database_url(TEST_DATABASE_URL),
        ),
        patch.object(delete_admin, "require_settings"),
        patch("builtins.input", side_effect=["y"]),
    ):
        delete_admin.delete_admin()


@pytest.mark.asyncio
async def test_cli_hands_the_admins_connector_key_to_the_owner(
    db_session: AsyncSession, connector_world, sole_password_admin, capsys
) -> None:
    from auth.api_keys import APIKeyManager

    world = connector_world
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
    # a second users row, so the CLI takes the "other users exist" branch
    owner = await world.user("owner")
    member = f"member_{uuid4().hex[:8]}"

    own_ws = await world.workspace(admin_id)
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
    before = await _key_rows(db_session, [moved_id, *bystanders])
    assert len(before) == 4
    await db_session.commit()

    await asyncio.to_thread(_run_cli)
    db_session.expire_all()

    assert "Connector write keys handed to their workspace owners: 1" in capsys.readouterr().out
    assert (
        await db_session.execute(select(User).where(User.user_id == admin_id))
    ).scalar_one_or_none() is None
    after = await _key_rows(db_session, [moved_id, *bystanders])
    assert after[moved_id]["user_id"] == owner
    assert _without_owner(after[moved_id]) == _without_owner(before[moved_id])
    assert {k: after[k] for k in bystanders} == {k: before[k] for k in bystanders}
    assert await _key_rows(db_session, [own_connector_key, own_personal, personal]) == {}

    verified = await APIKeyManager(db_session).verify_key(provisioned.plaintext_kmc_api_key)
    assert verified is not None
    assert verified.user_id == owner
    assert verified.is_connector_key is True
