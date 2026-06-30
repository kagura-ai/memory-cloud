"""``GET /admin/plans/audit`` resolves the actor to email + keeps the user_id.

The "changed by" column must surface the actor's **email** (a stable, unique
display label) while keeping the raw **user_id** in ``changed_by`` so the admin
UI can link it to ``/admin/users/{user_id}``.

Regression guard for the bug where the endpoint joined to ``User.name`` — an
ambiguous display name (e.g. literally "admin") — instead of ``User.email``,
and overwrote ``changed_by`` with that label so the UI had no id to link to.

Hits a real Postgres test DB because the resolution is a SQL outer-join
projection that a mock-DB test cannot exercise.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import get_plan_change_audit
from models.auth import PlanChange

from ._admin_helpers import make_user, make_workspace, mock_admin


class TestPlanAuditActorResolution:
    """``changed_by`` = raw user_id (link target); ``changed_by_email`` = label."""

    @pytest.mark.asyncio
    async def test_resolves_actor_to_email_keeping_user_id_for_linking(
        self,
        db_session: AsyncSession,
    ) -> None:
        # Display name is intentionally noise ("admin") to prove we resolve the
        # *email*, not the name, and that changed_by stays the user_id.
        user = make_user(name="admin")
        db_session.add(user)
        await db_session.flush()

        ws = make_workspace(owner_user_id=user.user_id)
        db_session.add(ws)
        await db_session.flush()  # insert the workspace before its FK child
        db_session.add(
            PlanChange(
                workspace_id=ws.id,
                old_plan="free",
                new_plan="pro",
                changed_by=user.user_id,
                reason="test",
            )
        )
        await db_session.commit()

        entries = await get_plan_change_audit(admin_user=mock_admin(), db=db_session, limit=100)
        entry = next(e for e in entries if e.workspace_id == str(ws.id))

        # Link target: the raw user_id, NOT the display name.
        assert entry.changed_by == user.user_id
        assert entry.changed_by != "admin"
        # Display labels: resolved name + email (the UI stacks them).
        assert entry.changed_by_name == user.name == "admin"
        assert entry.changed_by_email == user.email

    @pytest.mark.asyncio
    async def test_unresolved_actor_yields_null_email_and_raw_id(
        self,
        db_session: AsyncSession,
    ) -> None:
        # An actor id with no matching User row (e.g. account erased →
        # pseudonymized changed_by) must fall back to the raw id with no email,
        # so the UI renders plain text without a dangling /admin/users link.
        user = make_user()
        db_session.add(user)
        await db_session.flush()

        ws = make_workspace(owner_user_id=user.user_id)
        db_session.add(ws)
        await db_session.flush()  # insert the workspace before its FK child
        db_session.add(
            PlanChange(
                workspace_id=ws.id,
                old_plan="free",
                new_plan="pro",
                changed_by="erased-pseudonym-xyz",
                reason="test",
            )
        )
        await db_session.commit()

        entries = await get_plan_change_audit(admin_user=mock_admin(), db=db_session, limit=100)
        entry = next(e for e in entries if e.workspace_id == str(ws.id))

        assert entry.changed_by == "erased-pseudonym-xyz"
        assert entry.changed_by_name is None
        assert entry.changed_by_email is None
