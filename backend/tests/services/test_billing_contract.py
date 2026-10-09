"""Shared running-subscription checks (Issue #1940)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Workspace,
)
from services.billing_contract import (
    ensure_no_billing_contract,
    owned_workspaces_under_contract,
)
from services.workspace_service import WorkspaceService
from utils.exceptions import BillingContractActiveError


def _ws(plan_name: str, source: str = ENTITLEMENT_SOURCE_EXTERNAL_BILLING) -> Workspace:
    return Workspace(
        id=uuid4(), name="w", owner_user_id="u-1", plan_name=plan_name, entitlement_source=source
    )


@pytest.mark.asyncio
async def test_owned_query_keeps_only_live_workspaces_under_contract():
    paid = _ws("pro")
    rows = [paid, _ws("free"), _ws("pro", ENTITLEMENT_SOURCE_ADMIN_GRANT)]
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    assert await owned_workspaces_under_contract(db, "u-1") == [paid]

    sql = str(db.execute.await_args.args[0].compile()).lower()
    assert "workspaces.owner_user_id" in sql
    assert "workspaces.deleted_at is null" in sql


def test_ensure_no_billing_contract_names_the_workspaces():
    paid = _ws("basic")
    with pytest.raises(BillingContractActiveError) as exc:
        ensure_no_billing_contract([_ws("free"), paid])
    assert exc.value.status_code == 409
    assert exc.value.error_code == "BILLING-005"
    assert exc.value.details["workspace_ids"] == [str(paid.id)]


def test_ensure_no_billing_contract_passes_without_contract():
    ensure_no_billing_contract([_ws("free"), _ws("pro", ENTITLEMENT_SOURCE_ADMIN_GRANT)])


@pytest.mark.asyncio
async def test_delete_workspace_refuses_under_the_row_lock():
    """The check reads the locked row, so the service refuses before any delete."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    ws = _ws("pro")
    with patch(
        "services.workspace_service.lock_workspace_for_update",
        new=AsyncMock(return_value=ws),
    ) as lock:
        with pytest.raises(BillingContractActiveError):
            await WorkspaceService(db).delete_workspace(ws.id, deleted_by="u-1")

    lock.assert_awaited_once_with(db, ws.id)
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_workspace_soft_deletes_a_free_workspace():
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.rowcount = 0
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    ws = _ws("free")
    with patch(
        "services.workspace_service.lock_workspace_for_update",
        new=AsyncMock(return_value=ws),
    ):
        await WorkspaceService(db).delete_workspace(ws.id, deleted_by="u-1")

    assert ws.deleted_at is not None
    db.commit.assert_awaited()
