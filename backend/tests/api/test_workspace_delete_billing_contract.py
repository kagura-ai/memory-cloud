"""DELETE /api/v1/workspaces/{id} refuses a workspace under contract (Issue #1940)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.workspaces import delete_workspace
from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Workspace,
)
from utils.exceptions import BillingContractActiveError


def _db_returning(workspace: Workspace) -> MagicMock:
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = workspace
    db.execute = AsyncMock(return_value=result)
    return db


async def _call(workspace: Workspace) -> MagicMock:
    db = _db_returning(workspace)
    with (
        patch(
            "api.routes.workspaces.get_current_user",
            new=AsyncMock(return_value={"user_id": "owner-1"}),
        ),
        patch("api.routes.workspaces.PermissionService") as perm_cls,
        patch("api.routes.workspaces.WorkspaceService") as svc_cls,
    ):
        perm_cls.return_value.check_workspace_owner = AsyncMock()
        svc_cls.return_value.delete_workspace = AsyncMock()
        await delete_workspace(workspace.id, SimpleNamespace(), db)
    return svc_cls.return_value.delete_workspace


def _workspace(plan_name: str, source: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name="w",
        owner_user_id="owner-1",
        plan_name=plan_name,
        entitlement_source=source,
    )


@pytest.mark.asyncio
async def test_paid_billing_workspace_is_refused_with_409():
    ws = _workspace("pro", ENTITLEMENT_SOURCE_EXTERNAL_BILLING)
    with pytest.raises(BillingContractActiveError) as exc:
        await _call(ws)
    assert exc.value.status_code == 409
    assert exc.value.error_code == "BILLING-005"
    assert exc.value.details["workspace_ids"] == [str(ws.id)]


@pytest.mark.asyncio
async def test_free_billing_workspace_is_deleted():
    ws = _workspace("free", ENTITLEMENT_SOURCE_EXTERNAL_BILLING)
    delete = await _call(ws)
    delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_granted_paid_workspace_is_deleted():
    ws = _workspace("pro", ENTITLEMENT_SOURCE_ADMIN_GRANT)
    delete = await _call(ws)
    delete.assert_awaited_once()
