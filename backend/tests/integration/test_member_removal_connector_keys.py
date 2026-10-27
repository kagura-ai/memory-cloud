"""A removed admin's connector keys stay with the workspace (#2352).

``remove_member`` hands the member's live connector write keys in the workspace
being left to its owner — byte-identical but for ``user_id`` — so the worker's
stored plaintext keeps verifying, and deletes the member's other keys there.
Keys of other members, other workspaces and the member's own keys elsewhere
are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, WorkspaceMember
from utils.datetime import to_naive_utc
from utils.exceptions import ValidationError

from ._connector_world import _key_rows, _live_connector_key, _without_owner


def _removal_log(mock_logger) -> dict:
    (call,) = [
        c for c in mock_logger.info.call_args_list if c.args[0] == "removed_member_from_workspace"
    ]
    return call.kwargs


class TestConnectorKeysSurviveMemberRemoval:
    @pytest.mark.asyncio
    async def test_a_removed_admins_connector_key_moves_to_the_owner(
        self, db_session: AsyncSession, connector_world
    ):
        from auth.api_keys import APIKeyManager
        from services.connector_provisioning import ConnectorProvisioningService

        world = connector_world
        target = f"target_{uuid4().hex[:8]}"
        owner = f"owner_{uuid4().hex[:8]}"
        ws = await world.workspace(owner, **{target: WorkspaceRole.ADMIN})
        ws_id = ws.id

        # a connector the target provisioned and rotated once: the first key
        # is now revoked and still the target's
        provisioned = await world.connector(ws, by=target)
        connector_id = provisioned.connector.id
        revoked_key_id = (await _live_connector_key(db_session, connector_id)).id
        rotated = await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=ws_id, connector_id=connector_id, user_id=target
        )
        await db_session.flush()
        live = await _live_connector_key(db_session, connector_id)
        assert live.user_id == target
        live_id = live.id
        personal_id = await world.personal_key(target, ws)
        before = (await _key_rows(db_session, [live_id]))[live_id]

        with patch("services.workspace_service.logger") as mock_logger:
            await world.remove(ws_id, target)

        after = (await _key_rows(db_session, [live_id]))[live_id]
        assert after["user_id"] == owner
        assert after["revoked_at"] is None
        assert _without_owner(after) == _without_owner(before)
        # the target's personal key and revoked connector key are gone
        assert await _key_rows(db_session, [personal_id, revoked_key_id]) == {}
        log = _removal_log(mock_logger)
        assert log["connector_api_keys_reowned"] == 1
        assert log["api_keys_deleted"] == 2
        assert (
            await db_session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == ws_id, WorkspaceMember.user_id == target
                )
            )
        ).scalar_one_or_none() is None

        # the worker's stored plaintext still verifies, now as the owner
        verified = await APIKeyManager(db_session).verify_key(rotated.plaintext_kmc_api_key)
        assert verified is not None
        assert verified.user_id == owner
        assert verified.is_connector_key is True

        # and the owner can rotate it; the replacement stays the owner's
        again = await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=ws_id, connector_id=connector_id, user_id=owner
        )
        await db_session.flush()
        assert again.key_owner_user_id == owner
        assert (await _live_connector_key(db_session, connector_id)).user_id == owner

    @pytest.mark.asyncio
    async def test_other_members_and_workspaces_keys_are_untouched(
        self, db_session: AsyncSession, connector_world
    ):
        world = connector_world
        target = f"target_{uuid4().hex[:8]}"
        owner = f"owner_{uuid4().hex[:8]}"
        member = f"member_{uuid4().hex[:8]}"
        ws = await world.workspace(
            owner, **{target: WorkspaceRole.ADMIN, member: WorkspaceRole.ADMIN}
        )
        ws_id = ws.id
        mine = await world.connector(ws, by=target)
        theirs = await world.connector(ws, by=member)
        owners_key = await world.personal_key(owner, ws)
        members_key = await world.personal_key(member, ws)
        elsewhere_owner = f"else_{uuid4().hex[:8]}"
        elsewhere = await world.workspace(elsewhere_owner)
        unrelated = await world.connector(elsewhere, by=elsewhere_owner)
        bystanders = [
            (await _live_connector_key(db_session, theirs.connector.id)).id,
            (await _live_connector_key(db_session, unrelated.connector.id)).id,
            owners_key,
            members_key,
        ]
        mine_id = (await _live_connector_key(db_session, mine.connector.id)).id
        before = await _key_rows(db_session, bystanders)
        assert len(before) == 4

        await world.remove(ws_id, target)

        assert await _key_rows(db_session, bystanders) == before
        assert (await _key_rows(db_session, [mine_id]))[mine_id]["user_id"] == owner

    @pytest.mark.asyncio
    async def test_only_the_workspace_being_left_is_touched(
        self, db_session: AsyncSession, connector_world
    ):
        world = connector_world
        target = f"target_{uuid4().hex[:8]}"
        owner_1 = f"owner1_{uuid4().hex[:8]}"
        owner_2 = f"owner2_{uuid4().hex[:8]}"
        ws_1 = await world.workspace(owner_1, **{target: WorkspaceRole.ADMIN})
        ws_2 = await world.workspace(owner_2, **{target: WorkspaceRole.ADMIN})
        ws_1_id = ws_1.id
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
        personal_2 = await world.personal_key(target, ws_2)
        before = await _key_rows(db_session, [key_2, personal_2])

        await world.remove(ws_1_id, target)

        assert (await _key_rows(db_session, [key_1]))[key_1]["user_id"] == owner_1
        # still a member of ws_2: those keys stay the target's, unchanged
        assert await _key_rows(db_session, [key_2, personal_2]) == before
        assert before[key_2]["user_id"] == target

    @pytest.mark.asyncio
    async def test_an_expired_unrevoked_connector_key_moves_too(
        self, db_session: AsyncSession, connector_world
    ):
        world = connector_world
        target = f"target_{uuid4().hex[:8]}"
        owner = f"owner_{uuid4().hex[:8]}"
        ws = await world.workspace(owner, **{target: WorkspaceRole.ADMIN})
        ws_id = ws.id
        provisioned = await world.connector(ws, by=target)
        key_id = (await _live_connector_key(db_session, provisioned.connector.id)).id
        await db_session.execute(
            update(APIKey)
            .where(APIKey.id == key_id)
            .values(expires_at=to_naive_utc(datetime(2020, 1, 1, tzinfo=UTC)))
            .execution_options(synchronize_session=False)
        )

        await world.remove(ws_id, target)

        row = (await _key_rows(db_session, [key_id]))[key_id]
        assert row["user_id"] == owner
        assert row["revoked_at"] is None

    @pytest.mark.asyncio
    async def test_removing_the_owner_is_refused_and_touches_nothing(
        self, db_session: AsyncSession, connector_world
    ):
        world = connector_world
        owner = f"owner_{uuid4().hex[:8]}"
        member = f"member_{uuid4().hex[:8]}"
        ws = await world.workspace(owner, **{member: WorkspaceRole.ADMIN})
        ws_id = ws.id
        key_id = (
            await _live_connector_key(
                db_session, (await world.connector(ws, by=owner)).connector.id
            )
        ).id
        before = await _key_rows(db_session, [key_id])

        with pytest.raises(ValidationError):
            await world.remove(ws_id, owner)

        assert await _key_rows(db_session, [key_id]) == before
