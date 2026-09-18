"""#1561: admin ``extra_connectors`` grants on tiers without the feature warn, not block.

Since #1551 only tiers with the ``connectors`` feature (XL by default) may
create connectors. An admin may still grant ``extra_connectors`` seats to an
M/L workspace ahead of an upgrade — the grant must be stored as usual, and the
response must carry a structured warning so the admin UI can flag that the
seats are inert until the upgrade.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import UpdateAddonRequest, update_workspace_quotas
from models.auth import Workspace
from models.resource import WorkspaceAddon

from ._admin_helpers import make_user, make_workspace, mock_admin


@pytest_asyncio.fixture
async def plan_workspace(db_session: AsyncSession, request) -> dict:
    """A workspace on ``request.param`` (plan name); defaults to ``pro``."""
    plan_name = getattr(request, "param", "pro")
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name=plan_name)
    db_session.add(ws)
    await db_session.commit()

    return {"ws_uuid": ws.id, "workspace_id": str(ws.id), "plan_name": plan_name}


class TestConnectorGrantFeatureWarning:
    @pytest.mark.parametrize("plan_workspace", ["basic", "pro"], indirect=True)
    @pytest.mark.asyncio
    async def test_grant_is_stored_and_response_warns_on_tier_without_connectors(
        self,
        db_session: AsyncSession,
        plan_workspace: dict,
    ) -> None:
        result = await update_workspace_quotas(
            workspace_id=plan_workspace["workspace_id"],
            request=UpdateAddonRequest(addon_connector_bonus=2),
            admin_user=mock_admin(),
            db=db_session,
        )

        assert result["warnings"] == ["connectors_feature_missing"]

        # Not blocked: the admin_grant row exists and the cache was recalculated.
        row = (
            await db_session.execute(
                select(WorkspaceAddon).where(
                    WorkspaceAddon.workspace_id == plan_workspace["ws_uuid"],
                    WorkspaceAddon.addon_type == "extra_connectors",
                    WorkspaceAddon.source == "admin_grant",
                )
            )
        ).scalar_one()
        assert row.quantity == 2
        ws = await db_session.get(Workspace, plan_workspace["ws_uuid"])
        assert ws is not None
        assert ws.addon_connector_bonus == 2

    @pytest.mark.parametrize("plan_workspace", ["promax"], indirect=True)
    @pytest.mark.asyncio
    async def test_no_warning_on_tier_with_connectors(
        self,
        db_session: AsyncSession,
        plan_workspace: dict,
    ) -> None:
        result = await update_workspace_quotas(
            workspace_id=plan_workspace["workspace_id"],
            request=UpdateAddonRequest(addon_connector_bonus=1),
            admin_user=mock_admin(),
            db=db_session,
        )

        assert result["warnings"] == []

    @pytest.mark.asyncio
    async def test_unrelated_grant_on_pro_carries_no_warning(
        self,
        db_session: AsyncSession,
        plan_workspace: dict,
    ) -> None:
        result = await update_workspace_quotas(
            workspace_id=plan_workspace["workspace_id"],
            request=UpdateAddonRequest(addon_memory_bonus=10_000),
            admin_user=mock_admin(),
            db=db_session,
        )

        assert result["warnings"] == []
