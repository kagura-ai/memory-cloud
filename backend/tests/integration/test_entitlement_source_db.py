"""DB-level pins for entitlement_source provenance on the local set paths (#1095).

The internal billing push (→ ``external_billing``) is covered in
``test_internal_billing_plan_db.py``. This file pins the two **locally-owned**
paths that must mark ``admin_grant`` so the external billing reconciler never
reverts them:

- the system-admin manual set (``admin_plans.update_workspace_plan``), and
- the owner self-service legacy path (``workspace_plan.update_workspace_plan``).

Each test PRE-SETS ``entitlement_source = "external_billing"`` before calling the
handler, so the assertion proves the handler actively flips it back — the
``admin_grant`` server_default cannot mask a missing write.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import Workspace, WorkspaceMember

from ._admin_helpers import make_user, make_workspace, mock_admin


async def _reload_workspace(db: AsyncSession, workspace_id: str) -> Workspace:
    """Re-read the workspace under lock-free fresh state after a handler commit."""
    ws = (
        await db.execute(
            select(Workspace)
            .where(Workspace.id == UUID(workspace_id))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return ws


@pytest_asyncio.fixture
async def billed_workspace(db_session: AsyncSession) -> dict:
    """A FREE workspace already marked external_billing, owned by a real user."""
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name="free")
    ws.entitlement_source = "external_billing"  # pre-state the reconciler "owns"
    db_session.add(ws)
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role=WorkspaceRole.OWNER)
    )
    await db_session.commit()
    return {"workspace_id": str(ws.id), "user_id": user.user_id}


@pytest.mark.asyncio
async def test_admin_set_marks_admin_grant(db_session: AsyncSession, billed_workspace: dict):
    """A system-admin plan set flips external_billing → admin_grant (#1095)."""
    from api.routes.admin_plans import AdminUpdatePlanRequest, update_workspace_plan

    await update_workspace_plan(
        workspace_id=billed_workspace["workspace_id"],
        request=AdminUpdatePlanRequest(plan_name="pro"),  # upgrade — no member cascade
        admin_user=mock_admin(),
        db=db_session,
    )

    ws = await _reload_workspace(db_session, billed_workspace["workspace_id"])
    assert ws.plan_name == "pro"
    assert ws.entitlement_source == "admin_grant"


@pytest.mark.asyncio
async def test_owner_self_service_marks_admin_grant(
    db_session: AsyncSession, billed_workspace: dict
):
    """Owner self-service (legacy in-process path) flips external_billing →
    admin_grant so the reconciler doesn't revert a change it doesn't manage."""
    from api.routes.workspace_plan import UpdatePlanRequest, update_workspace_plan

    # Exercise the REAL owner gate — the fixture seeds a genuine OWNER member +
    # owner_user_id, so check_workspace_owner must pass without mocking it. Only the
    # billing-plugin flag is patched (the gate that makes self-service reachable).
    with patch("plugins.billing.is_billing_enabled", return_value=True):
        await update_workspace_plan(
            workspace_id=billed_workspace["workspace_id"],
            request=UpdatePlanRequest(plan_name="pro"),  # upgrade — no member removal
            user={"user_id": billed_workspace["user_id"], "email": "o@test.invalid"},
            db=db_session,
        )

    ws = await _reload_workspace(db_session, billed_workspace["workspace_id"])
    assert ws.plan_name == "pro"
    assert ws.entitlement_source == "admin_grant"


@pytest.mark.asyncio
async def test_stripe_checkout_marks_admin_grant(db_session: AsyncSession, billed_workspace: dict):
    """The legacy in-app Stripe checkout webhook marks admin_grant (locally-owned):
    the external reconciler doesn't manage in-app Stripe subs, so it must not
    revert them (#1095 sweep follow-up)."""
    from services.stripe_service import _apply_plan_change

    await _apply_plan_change(
        db=db_session,
        workspace_id=UUID(billed_workspace["workspace_id"]),
        new_plan_name="pro",
        customer_id="cus_test_123",
        subscription_id="sub_test_123",
    )

    ws = await _reload_workspace(db_session, billed_workspace["workspace_id"])
    assert ws.plan_name == "pro"
    assert ws.entitlement_source == "admin_grant"


@pytest.mark.asyncio
async def test_stripe_cancel_marks_admin_grant(db_session: AsyncSession, billed_workspace: dict):
    """A Stripe subscription cancellation downgrades to free AND marks admin_grant."""
    from services.stripe_service import _apply_plan_change, _handle_subscription_cancelled

    # First put it on pro via checkout (sets the stripe_customer_id the cancel reads).
    await _apply_plan_change(
        db=db_session,
        workspace_id=UUID(billed_workspace["workspace_id"]),
        new_plan_name="pro",
        customer_id="cus_cancel_1",
        subscription_id="sub_cancel_1",
    )
    # Re-dirty the provenance to prove the cancel path actively re-marks it.
    ws = await _reload_workspace(db_session, billed_workspace["workspace_id"])
    ws.entitlement_source = "external_billing"
    await db_session.commit()

    await _handle_subscription_cancelled(db=db_session, customer_id="cus_cancel_1")

    ws = await _reload_workspace(db_session, billed_workspace["workspace_id"])
    assert ws.plan_name == "free"
    assert ws.entitlement_source == "admin_grant"
