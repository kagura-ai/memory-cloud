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
async def test_missing_workspace_raises_not_found(db_session):
    from utils.exceptions import NotFoundException

    with pytest.raises(NotFoundException) as exc:
        await set_workspace_plan_from_billing(
            workspace_id=str(uuid4()),
            body=BillingPlanPush(plan_name="pro"),
            _=None,
            db=db_session,
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# entitlement_source provenance (#1095)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_workspace_defaults_admin_grant(db_session):
    """The server_default is the protective 'admin_grant' — a never-billed
    workspace is locally-owned, so a reconcile pass leaves it untouched."""
    ws = await _make_workspace(db_session, plan_name="free")
    await db_session.refresh(ws)
    assert ws.entitlement_source == "admin_grant"


@pytest.mark.asyncio
async def test_push_marks_external_billing_and_echoes(db_session):
    """A billing push flips provenance to 'external_billing' (default is
    'admin_grant', so this genuinely proves the handler set it) and echoes it."""
    ws = await _make_workspace(db_session, plan_name="free")
    assert ws.entitlement_source == "admin_grant"  # precondition

    result = await set_workspace_plan_from_billing(
        workspace_id=str(ws.id),
        body=BillingPlanPush(plan_name="pro"),
        _=None,
        db=db_session,
    )

    await db_session.refresh(ws)
    assert ws.entitlement_source == "external_billing"
    assert result.entitlement_source == "external_billing"


@pytest.mark.asyncio
async def test_get_entitlement_returns_source(db_session):
    """The reconciler read surface returns plan + entitlement_source; it reflects
    admin_grant before any push and external_billing after."""
    from api.routes.internal_billing import get_workspace_entitlement

    ws = await _make_workspace(db_session, plan_name="basic")

    before = await get_workspace_entitlement(workspace_id=str(ws.id), _=None, db=db_session)
    assert before.plan_name == "basic"
    assert before.entitlement_source == "admin_grant"

    await set_workspace_plan_from_billing(
        workspace_id=str(ws.id),
        body=BillingPlanPush(plan_name="pro"),
        _=None,
        db=db_session,
    )
    after = await get_workspace_entitlement(workspace_id=str(ws.id), _=None, db=db_session)
    assert after.plan_name == "pro"
    assert after.entitlement_source == "external_billing"


@pytest.mark.asyncio
async def test_get_entitlement_missing_workspace_raises_not_found(db_session):
    from api.routes.internal_billing import get_workspace_entitlement
    from utils.exceptions import NotFoundException

    with pytest.raises(NotFoundException) as exc:
        await get_workspace_entitlement(workspace_id=str(uuid4()), _=None, db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_check_constraint_rejects_invalid_source(db_session):
    """The valid_entitlement_source CHECK rejects an out-of-enum value."""
    from sqlalchemy.exc import IntegrityError

    ws = await _make_workspace(db_session, plan_name="free")
    ws.entitlement_source = "bogus_source"
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_get_entitlement_skips_soft_deleted(db_session):
    """A soft-deleted workspace is 'gone' to the reconciler → 404 (not stale
    entitlement it might resurrect). The GET filters deleted_at (#1095)."""
    from api.routes.internal_billing import get_workspace_entitlement
    from utils.datetime import utcnow
    from utils.exceptions import NotFoundException

    ws = await _make_workspace(db_session, plan_name="pro")
    ws.deleted_at = utcnow()
    await db_session.commit()

    with pytest.raises(NotFoundException) as exc:
        await get_workspace_entitlement(workspace_id=str(ws.id), _=None, db=db_session)
    assert exc.value.status_code == 404
