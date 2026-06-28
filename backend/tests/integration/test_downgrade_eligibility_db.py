"""DB-level pins for downgrade eligibility (#1123).

Exercises the workspace-scoped COUNT queries and the ``/internal`` route handler
against a real session (the pure eligibility math is unit-tested in
tests/services/test_downgrade_eligibility_service.py). Members / contexts /
shared-contexts are constructed here; the memory and resource-token counts reuse
the same ``func.count`` + workspace-filter shape and are covered by the unit
matrix.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.internal_billing import get_downgrade_eligibility
from models.auth import WorkspaceMember, WorkspaceRole
from services.downgrade_eligibility_service import DowngradeEligibilityService
from utils.exceptions import NotFoundException

from ._admin_helpers import make_context, make_user, make_workspace


@pytest_asyncio.fixture
async def pro_workspace(db_session: AsyncSession) -> dict:
    """A PRO workspace: 3 members, 2 contexts (1 shared, 1 private)."""
    owner = make_user()
    m2 = make_user()
    m3 = make_user()
    db_session.add_all([owner, m2, m3])
    await db_session.flush()

    ws = make_workspace(owner_user_id=owner.user_id, plan_name="pro")
    db_session.add(ws)
    await db_session.flush()

    for member in (owner, m2, m3):
        db_session.add(
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member.user_id,
                role=(WorkspaceRole.OWNER if member is owner else WorkspaceRole.MEMBER),
            )
        )
    db_session.add(make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=False))
    db_session.add(make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=True))
    await db_session.commit()
    return {"workspace_id": str(ws.id), "owner_id": owner.user_id}


@pytest.mark.asyncio
async def test_current_usage_counts_members_and_contexts(
    db_session: AsyncSession, pro_workspace: dict
):
    svc = DowngradeEligibilityService(db_session)
    from uuid import UUID

    usage = await svc.current_usage(UUID(pro_workspace["workspace_id"]))
    assert usage.members == 3
    assert usage.contexts == 2
    assert usage.shared_contexts == 1
    assert usage.memories == 0
    assert usage.resource_tokens == 0


@pytest.mark.asyncio
async def test_route_reports_blockers_for_each_lower_tier(
    db_session: AsyncSession, pro_workspace: dict
):
    view = await get_downgrade_eligibility(
        workspace_id=pro_workspace["workspace_id"], _=None, db=db_session
    )
    assert view.current_plan == "pro"
    by_plan = {t.target_plan: t for t in view.targets}
    assert set(by_plan) == {"free", "basic"}

    # free: 3 members > 1, 2 contexts > 1, 1 shared context disallowed.
    free_dims = {b.dimension for b in by_plan["free"].blockers}
    assert free_dims == {"members", "contexts", "shared_contexts"}
    assert by_plan["free"].eligible is False

    # basic: 3 members > 1 and shared disallowed, but 2 contexts <= 3 (fits).
    basic_dims = {b.dimension for b in by_plan["basic"].blockers}
    assert basic_dims == {"members", "shared_contexts"}
    assert by_plan["basic"].eligible is False


@pytest.mark.asyncio
async def test_route_404_for_soft_deleted_workspace(db_session: AsyncSession):
    owner = make_user()
    db_session.add(owner)
    await db_session.flush()
    ws = make_workspace(owner_user_id=owner.user_id, plan_name="pro", soft_deleted=True)
    db_session.add(ws)
    await db_session.commit()

    with pytest.raises(NotFoundException):
        await get_downgrade_eligibility(workspace_id=str(ws.id), _=None, db=db_session)
