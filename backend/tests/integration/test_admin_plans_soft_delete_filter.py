"""Regression tests for #687 — admin_plans routes must filter soft-deleted workspaces.

Sibling of ``test_admin_users_soft_delete_filter.py`` (#681) covering the
follow-up audit class identified in #687: ``backend/src/api/routes/admin_plans.py``
endpoints that fetch a single ``Workspace`` by ID for mutation or detail-view
purposes had no ``Workspace.deleted_at IS NULL`` filter, so admin operations
were silently applicable to soft-deleted workspaces.

Endpoints under test:

| Endpoint                                              | Classification         | Test type        |
|-------------------------------------------------------|------------------------|------------------|
| ``PUT /admin/plans/workspaces/{id}/plan``             | current-state (mut)    | 404 (filter)     |
| ``GET /admin/plans/workspaces/{id}/quotas``           | current-state (read)   | 404 (filter)     |
| ``PUT /admin/plans/workspaces/{id}/quotas``           | current-state (mut)    | 404 (filter)     |
| ``PUT /admin/plans/workspaces/{id}/spend-cap``        | current-state (mut)    | 404 (filter)     |
| ``GET /admin/plans/audit``                            | audit/history (intentional include) | pin (include)    |

The audit-endpoint pin test guards against a future "consistency patch"
reviewer adding the filter for symmetry — the audit log must keep entries
for workspaces that have since been soft-deleted (otherwise historical
plan changes disappear when an admin retires a workspace).

Hits a real Postgres test DB because the existing mock-DB tests in
``tests/api/test_admin_plans*.py`` cannot detect SQL ``WHERE`` clause
omissions.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import (
    UpdateAddonRequest,
    AdminUpdatePlanRequest,
    UpdateSpendCapRequest,
    get_plan_change_audit,
    get_workspace_quotas,
    update_workspace_plan,
    update_workspace_quotas,
    update_workspace_spend_cap,
)
from models.auth import PlanChange

from ._admin_helpers import make_user, make_workspace, mock_admin


@pytest_asyncio.fixture
async def soft_deleted_workspace(db_session: AsyncSession) -> dict:
    """A ``pro`` workspace with ``deleted_at`` set, owned by an active user."""
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, soft_deleted=True)
    db_session.add(ws)
    await db_session.commit()

    return {"user_id": user.user_id, "workspace_id": str(ws.id)}


class TestUpdateWorkspacePlan:
    """``PUT /admin/plans/workspaces/{id}/plan`` returns 404 for soft-deleted (#687)."""

    @pytest.mark.asyncio
    async def test_returns_404_for_soft_deleted_workspace(
        self,
        db_session: AsyncSession,
        soft_deleted_workspace: dict,
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await update_workspace_plan(
                workspace_id=soft_deleted_workspace["workspace_id"],
                request=AdminUpdatePlanRequest(plan_name="basic", reason="test"),
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 404, (
            "soft-deleted workspace must 404 on plan mutation (#687)"
        )


class TestGetWorkspaceQuotas:
    """``GET /admin/plans/workspaces/{id}/quotas`` returns 404 for soft-deleted (#687)."""

    @pytest.mark.asyncio
    async def test_returns_404_for_soft_deleted_workspace(
        self,
        db_session: AsyncSession,
        soft_deleted_workspace: dict,
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await get_workspace_quotas(
                workspace_id=soft_deleted_workspace["workspace_id"],
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 404, (
            "soft-deleted workspace must 404 on quota detail read (#687)"
        )


class TestUpdateWorkspaceQuotas:
    """``PUT /admin/plans/workspaces/{id}/quotas`` returns 404 for soft-deleted (#687)."""

    @pytest.mark.asyncio
    async def test_returns_404_for_soft_deleted_workspace(
        self,
        db_session: AsyncSession,
        soft_deleted_workspace: dict,
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await update_workspace_quotas(
                workspace_id=soft_deleted_workspace["workspace_id"],
                request=UpdateAddonRequest(),
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 404, (
            "soft-deleted workspace must 404 on addon-bonus mutation (#687)"
        )


class TestUpdateWorkspaceSpendCap:
    """``PUT /admin/plans/workspaces/{id}/spend-cap`` returns 404 for soft-deleted (#687)."""

    @pytest.mark.asyncio
    async def test_returns_404_for_soft_deleted_workspace(
        self,
        db_session: AsyncSession,
        soft_deleted_workspace: dict,
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await update_workspace_spend_cap(
                workspace_id=soft_deleted_workspace["workspace_id"],
                request=UpdateSpendCapRequest(),
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 404, (
            "soft-deleted workspace must 404 on spend-cap mutation (#687)"
        )


class TestGetPlanChangeAuditIncludesSoftDeleted:
    """``GET /admin/plans/audit`` intentionally INCLUDES soft-deleted workspaces (#687).

    Pin test against future "consistency patch" — the audit log records historical
    plan changes for workspaces that may have since been soft-deleted; entries
    must remain visible so admins can reconcile past billing / plan transitions.
    """

    @pytest.mark.asyncio
    async def test_audit_log_includes_plan_change_on_soft_deleted_workspace(
        self,
        db_session: AsyncSession,
        soft_deleted_workspace: dict,
    ) -> None:
        from uuid import UUID

        ws_uuid = UUID(soft_deleted_workspace["workspace_id"])
        db_session.add(
            PlanChange(
                workspace_id=ws_uuid,
                old_plan="free",
                new_plan="pro",
                changed_by=soft_deleted_workspace["user_id"],
                reason="pre-delete test fixture",
            )
        )
        await db_session.commit()

        entries = await get_plan_change_audit(
            admin_user=mock_admin(),
            db=db_session,
            limit=100,
        )

        matching = [e for e in entries if e.workspace_id == str(ws_uuid)]
        assert matching, (
            "plan-change audit log must include entries for soft-deleted "
            "workspaces (#687 audit/history classification — pin against "
            "future consistency-patch reviewer)"
        )
