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


def _db_returning(rows: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_owned_query_keeps_only_live_workspaces_under_contract():
    from utils.datetime import utcnow

    paid = _ws("pro")
    deleted_paid = _ws("pro")
    deleted_paid.deleted_at = utcnow()
    db = _db_returning(
        [paid, deleted_paid, _ws("free"), _ws("pro", ENTITLEMENT_SOURCE_ADMIN_GRANT)]
    )

    assert await owned_workspaces_under_contract(db, "u-1") == [paid]
    assert "workspaces.owner_user_id" in str(db.execute.await_args.args[0].compile()).lower()


@pytest.mark.asyncio
async def test_owned_query_can_include_soft_deleted():
    """The admin override log covers workspaces the hard delete removes."""
    from utils.datetime import utcnow

    paid = _ws("pro")
    deleted_paid = _ws("basic")
    deleted_paid.deleted_at = utcnow()
    db = _db_returning([paid, deleted_paid, _ws("free")])

    assert await owned_workspaces_under_contract(db, "u-1", include_deleted=True) == [
        paid,
        deleted_paid,
    ]


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


def test_ensure_ignores_soft_deleted_workspaces():
    """Raw owned lists can be passed: a deleted workspace never blocks."""
    from utils.datetime import utcnow

    deleted_paid = _ws("pro")
    deleted_paid.deleted_at = utcnow()
    ensure_no_billing_contract([deleted_paid])


@pytest.mark.asyncio
async def test_delete_workspace_commits_before_the_qdrant_cleanup():
    """The row lock is released before network I/O, so billing pushes and
    owner mutations do not wait on Qdrant."""
    from types import SimpleNamespace

    order: list[str] = []
    ctx = SimpleNamespace(id=uuid4(), name="c")

    def _rows(rows: list) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    db = MagicMock()
    # external API keys, users on this workspace, then (after commit) contexts
    db.execute = AsyncMock(side_effect=[_rows([]), _rows([]), _rows([ctx])])
    db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    ws = _ws("free")

    async def _drop(*_a, **_k):
        order.append("qdrant")

    with (
        patch(
            "services.workspace_service.lock_workspace_for_update",
            new=AsyncMock(return_value=ws),
        ),
        patch("db.qdrant.list_memory_collections", new=AsyncMock(return_value=[])),
        patch(
            "services.context_service.ContextService._delete_context_collection",
            new=_drop,
        ),
    ):
        await WorkspaceService(db).delete_workspace(ws.id, deleted_by="u-1")

    assert order == ["commit", "qdrant"]
