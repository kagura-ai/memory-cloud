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
