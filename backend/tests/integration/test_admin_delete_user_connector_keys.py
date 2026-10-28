"""The admin user delete keeps the user's connector keys (#2352).

``DELETE /admin/users/{id}`` hands the user's live connector write keys in a
workspace someone else owns to that owner, unchanged but for ``user_id``, and
deletes the rest; a workspace the user owns is deleted with its keys. Keys of
other users and workspaces are untouched.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import User, Workspace
from models.resource import WorkspaceConnector

from ._connector_world import _key_rows, _live_connector_key, _without_owner

_ADMIN = {"user_id": "admin-runner", "email": "admin-runner@example.com"}


async def _delete(db_session: AsyncSession, user_id: str) -> dict:
    from api.routes.admin import delete_user

    await db_session.commit()
    response = await delete_user(user_id, _ADMIN, db_session)
    db_session.expire_all()
    return response


class TestConnectorKeysSurviveAdminDelete:
    @pytest.mark.asyncio
    async def test_a_deleted_admins_connector_key_moves_to_the_owner(
        self, db_session: AsyncSession, connector_world
    ):
        from auth.api_keys import APIKeyManager
        from services.connector_provisioning import ConnectorProvisioningService

        world = connector_world
        target = await world.user("target")
        owner = await world.user("owner")
        ws = await world.workspace(owner, **{target: WorkspaceRole.ADMIN})
        ws_id = ws.id
        provisioned = await world.connector(ws, by=target)
        connector_id = provisioned.connector.id
        live_id = (await _live_connector_key(db_session, connector_id)).id
        personal_id = await world.personal_key(target, ws)
        before = (await _key_rows(db_session, [live_id]))[live_id]

        response = await _delete(db_session, target)

        assert response["deleted"]["api_keys"] == 1
        assert response["reowned"] == {"connector_api_keys": 1}
        after = (await _key_rows(db_session, [live_id]))[live_id]
        assert after["user_id"] == owner
        assert _without_owner(after) == _without_owner(before)
        assert await _key_rows(db_session, [personal_id]) == {}
        assert (
            await db_session.execute(select(User).where(User.user_id == target))
        ).scalar_one_or_none() is None

        verified = await APIKeyManager(db_session).verify_key(provisioned.plaintext_kmc_api_key)
        assert verified is not None
        assert verified.user_id == owner
        assert verified.is_connector_key is True

        again = await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=ws_id, connector_id=connector_id, user_id=owner
        )
        await db_session.flush()
        assert again.key_owner_user_id == owner

        # the connector keeps its write target: the auto-created context
        # belongs to the workspace owner, so the user's context delete spares it
        connector = (
            await db_session.execute(
                select(WorkspaceConnector).where(WorkspaceConnector.id == connector_id)
            )
        ).scalar_one()
        assert connector.context_id is not None

    @pytest.mark.asyncio
    async def test_an_owned_workspace_requires_transfer_without_losing_its_key(
        self, db_session: AsyncSession, connector_world
    ):
        from fastapi import HTTPException

        world = connector_world
        target = await world.user("target")
        member = await world.user("member")
        ws = await world.workspace(target, **{member: WorkspaceRole.ADMIN})
        ws_id = ws.id
        key_id = (
            await _live_connector_key(
                db_session, (await world.connector(ws, by=target)).connector.id
            )
        ).id
        with pytest.raises(HTTPException) as error:
            await _delete(db_session, target)
        assert error.value.status_code == 409
        await db_session.rollback()
        assert await db_session.get(Workspace, ws_id) is not None
        assert key_id in await _key_rows(db_session, [key_id])

    @pytest.mark.asyncio
    async def test_keys_in_two_workspaces_go_to_each_workspaces_owner(
        self, db_session: AsyncSession, connector_world
    ):
        world = connector_world
        target = await world.user("target")
        owner_1 = f"owner1_{uuid4().hex[:8]}"
        owner_2 = f"owner2_{uuid4().hex[:8]}"
        ws_1 = await world.workspace(owner_1, **{target: WorkspaceRole.ADMIN})
        ws_2 = await world.workspace(owner_2, **{target: WorkspaceRole.ADMIN})
        key_1 = (
            await _live_connector_key(
                db_session, (await world.connector(ws_1, by=target)).connector.id
            )
        ).id
        key_2 = (
            await _live_connector_key(
                db_session, (await world.connector(ws_2, by=target)).connector.id
            )
        ).id

        response = await _delete(db_session, target)

        rows = await _key_rows(db_session, [key_1, key_2])
        assert rows[key_1]["user_id"] == owner_1
        assert rows[key_2]["user_id"] == owner_2
        assert response["reowned"] == {"connector_api_keys": 2}

    @pytest.mark.asyncio
    async def test_other_users_and_workspaces_keys_are_untouched(
        self, db_session: AsyncSession, connector_world
    ):
        world = connector_world
        target = await world.user("target")
        owner = await world.user("owner")
        member = f"member_{uuid4().hex[:8]}"
        ws = await world.workspace(
            owner, **{target: WorkspaceRole.ADMIN, member: WorkspaceRole.ADMIN}
        )
        mine = await world.connector(ws, by=target)
        theirs = await world.connector(ws, by=member)
        owners_key = await world.personal_key(owner, ws)
        elsewhere_owner = f"else_{uuid4().hex[:8]}"
        elsewhere = await world.workspace(elsewhere_owner)
        unrelated = await world.connector(elsewhere, by=elsewhere_owner)
        bystanders = [
            (await _live_connector_key(db_session, theirs.connector.id)).id,
            (await _live_connector_key(db_session, unrelated.connector.id)).id,
            owners_key,
        ]
        mine_id = (await _live_connector_key(db_session, mine.connector.id)).id
        before = await _key_rows(db_session, bystanders)
        assert len(before) == 3

        await _delete(db_session, target)

        assert await _key_rows(db_session, bystanders) == before
        assert (await _key_rows(db_session, [mine_id]))[mine_id]["user_id"] == owner
