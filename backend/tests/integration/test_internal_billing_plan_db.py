"""DB-level pins for the internal billing entitlement push (Issue #954).

Calls the handler directly with a real session (auth is unit-tested separately)
to verify the actual plan_name + addon persistence and idempotency on a real
Workspace row.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from api.routes.internal_billing import BillingPlanPush, set_workspace_plan_from_billing
from models.auth import Workspace


async def _make_workspace(db_session, plan_name="free"):
    ws = Workspace(
        id=uuid4(),
        name=f"billing-test-{uuid4().hex[:8]}",
        plan_name=plan_name,
        owner_user_id="owner-954",
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.commit()
    await db_session.refresh(ws)
    return ws


@pytest.mark.asyncio
async def test_sets_plan_and_addon_on_real_workspace(db_session):
    ws = await _make_workspace(db_session, plan_name="free")

    result = await set_workspace_plan_from_billing(
        workspace_id=str(ws.id),
        body=BillingPlanPush(plan_name="pro", addons={"sleep_contexts": 3}),
        _=None,
        db=db_session,
    )

    await db_session.refresh(ws)
    assert result.plan_name == "pro"
    assert ws.plan_name == "pro"
    assert ws.addon_sleep_contexts_bonus == 3
    assert result.addons["sleep_contexts"] == 3


@pytest.mark.asyncio
async def test_idempotent_repeated_push(db_session):
    """Re-delivery (reconciliation) yields the same state — no increment, no error."""
    ws = await _make_workspace(db_session, plan_name="basic")
    body = BillingPlanPush(plan_name="pro", addons={"sleep_contexts": 2})

    await set_workspace_plan_from_billing(workspace_id=str(ws.id), body=body, _=None, db=db_session)
    await set_workspace_plan_from_billing(workspace_id=str(ws.id), body=body, _=None, db=db_session)

    await db_session.refresh(ws)
    assert ws.plan_name == "pro"
    # Absolute set, not increment — two pushes leave the bonus at 2, not 4.
    assert ws.addon_sleep_contexts_bonus == 2


@pytest.mark.asyncio
async def test_partial_addon_update_leaves_others_unchanged(db_session):
    ws = await _make_workspace(db_session, plan_name="pro")
    ws.addon_memory_bonus = 100
    await db_session.commit()

    await set_workspace_plan_from_billing(
        workspace_id=str(ws.id),
        body=BillingPlanPush(plan_name="pro", addons={"sleep_contexts": 1}),
        _=None,
        db=db_session,
    )

    await db_session.refresh(ws)
    assert ws.addon_sleep_contexts_bonus == 1
    assert ws.addon_memory_bonus == 100  # untouched dimension preserved


@pytest.mark.asyncio
async def test_missing_workspace_raises_404(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await set_workspace_plan_from_billing(
            workspace_id=str(uuid4()),
            body=BillingPlanPush(plan_name="pro"),
            _=None,
            db=db_session,
        )
    assert exc.value.status_code == 404
