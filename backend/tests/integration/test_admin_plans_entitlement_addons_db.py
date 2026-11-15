"""``entitlement_source`` and ``addon_records`` in the admin plan reads (#2829).

Real rows over HTTP: the workspace list row and the quotas read answer where a
workspace's plan comes from, and the quotas read answers the active add-on
records its add-on limits are computed from.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import _ADDON_FIELD_SPECS
from api.routes.internal_billing import BillingPlanPush, set_workspace_plan_from_billing
from auth.workspace_roles import WorkspaceRole
from models.auth import Workspace, WorkspaceMember
from models.resource import WorkspaceAddon
from services.addon_calculator_service import ADDON_UNIT_VALUES
from utils.datetime import utcnow

from ._admin_helpers import make_user, make_workspace
from ._admin_http import ADMIN_PRINCIPAL, admin_http

pytestmark = pytest.mark.asyncio

LIST = "/api/v1/admin/plans/workspaces"


def _quotas(workspace: Workspace) -> str:
    return f"/api/v1/admin/plans/workspaces/{workspace.id}/quotas"


async def _workspace(db_session: AsyncSession, plan_name: str = "pro") -> Workspace:
    user = make_user(name="Entitlement Owner")
    db_session.add(user)
    await db_session.flush()
    workspace = make_workspace(owner_user_id=user.user_id, plan_name=plan_name)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.user_id, role=WorkspaceRole.OWNER)
    )
    await db_session.commit()
    return workspace


async def _list_row(client, workspace: Workspace) -> dict:
    response = await client.get(LIST, params={"owner_user_id": workspace.owner_user_id})
    assert response.status_code == 200, response.text
    (row,) = response.json()["items"]
    assert row["id"] == str(workspace.id)
    return row


async def _both_sources(client, workspace: Workspace) -> tuple[str, str]:
    row = await _list_row(client, workspace)
    quotas = await client.get(_quotas(workspace))
    assert quotas.status_code == 200, quotas.text
    return row["entitlement_source"], quotas.json()["entitlement_source"]


async def test_untouched_workspace_answers_admin_grant(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session)
    async with admin_http(db_session) as client:
        assert await _both_sources(client, workspace) == ("admin_grant", "admin_grant")


async def test_internal_billing_push_answers_external_billing(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session, plan_name="free")
    await set_workspace_plan_from_billing(
        workspace_id=str(workspace.id),
        body=BillingPlanPush(plan_name="pro"),
        _=None,
        db=db_session,
    )
    async with admin_http(db_session) as client:
        assert await _both_sources(client, workspace) == ("external_billing", "external_billing")


async def test_admin_plan_put_answers_admin_grant(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session, plan_name="free")
    await set_workspace_plan_from_billing(
        workspace_id=str(workspace.id),
        body=BillingPlanPush(plan_name="pro"),
        _=None,
        db=db_session,
    )
    async with admin_http(db_session) as client:
        put = await client.put(
            f"/api/v1/admin/plans/workspaces/{workspace.id}/plan",
            json={"plan_name": "basic", "reason": "comp"},
        )
        assert put.status_code == 200, put.text
        assert await _both_sources(client, workspace) == ("admin_grant", "admin_grant")


async def test_quotas_put_shows_the_admin_grant_record_and_zero_removes_it(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session)
    unit = ADDON_UNIT_VALUES["extra_memory"]
    async with admin_http(db_session) as client:
        put = await client.put(_quotas(workspace), json={"addon_memory_bonus": 3 * unit})
        assert put.status_code == 200, put.text

        body = (await client.get(_quotas(workspace))).json()
        (record,) = body["addon_records"]
        assert set(record) == {
            "id",
            "addon_type",
            "source",
            "quantity",
            "bonus",
            "active_from",
            "active_until",
            "created_at",
            "created_by",
        }
        assert record["addon_type"] == "extra_memory"
        assert record["source"] == "admin_grant"
        assert record["quantity"] == 3
        assert record["bonus"] == 3 * unit
        assert record["created_by"] == ADMIN_PRINCIPAL["user_id"]
        assert record["active_until"] is None
        assert record["active_from"].endswith("Z")
        assert record["created_at"].endswith("Z")

        put = await client.put(_quotas(workspace), json={"addon_memory_bonus": 0})
        assert put.status_code == 200, put.text
        assert (await client.get(_quotas(workspace))).json()["addon_records"] == []


async def test_bonus_sums_equal_the_drift_check_expected_values(db_session: AsyncSession) -> None:
    """Rows written without a recalculation: every add-on type drifts, so the
    drift check reports its ``expected_value`` for each of them."""
    workspace = await _workspace(db_session)
    for index, spec in enumerate(_ADDON_FIELD_SPECS, start=1):
        db_session.add(
            WorkspaceAddon(
                workspace_id=workspace.id,
                addon_type=spec.addon_type,
                quantity=index,
                source="admin_grant",
                created_by="seed",
            )
        )
        db_session.add(
            WorkspaceAddon(
                workspace_id=workspace.id,
                addon_type=spec.addon_type,
                quantity=2,
                source="stripe",
                created_by="seed",
            )
        )
    await db_session.commit()

    async with admin_http(db_session) as client:
        records = (await client.get(_quotas(workspace))).json()["addon_records"]
        drift = (await client.get("/api/v1/admin/plans/addon-cache-consistency")).json()

    assert [(r["addon_type"], r["source"]) for r in records] == sorted(
        (spec.addon_type, source)
        for spec in _ADDON_FIELD_SPECS
        for source in ("admin_grant", "stripe")
    )
    sums: dict[str, int] = defaultdict(int)
    for record in records:
        sums[record["addon_type"]] += record["bonus"]
    expected = {
        entry["addon_type"]: entry["expected_value"]
        for entry in drift
        if entry["workspace_id"] == str(workspace.id)
    }
    assert set(expected) == {spec.addon_type for spec in _ADDON_FIELD_SPECS}
    assert dict(sums) == expected


async def test_rows_outside_the_active_window_are_not_records(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session)
    now = utcnow()
    db_session.add_all(
        [
            WorkspaceAddon(
                workspace_id=workspace.id,
                addon_type="extra_memory",
                quantity=7,
                source="stripe",
                active_from=now - timedelta(days=30),
                active_until=now - timedelta(days=1),
                created_by="expired-row",
            ),
            WorkspaceAddon(
                workspace_id=workspace.id,
                addon_type="extra_contexts",
                quantity=8,
                source="stripe",
                active_from=now + timedelta(days=1),
                created_by="future-row",
            ),
            WorkspaceAddon(
                workspace_id=workspace.id,
                addon_type="extra_members",
                quantity=9,
                source="stripe",
                active_from=now - timedelta(days=1),
                active_until=now + timedelta(days=1),
                created_by="active-row",
            ),
        ]
    )
    await db_session.commit()

    async with admin_http(db_session) as client:
        records = (await client.get(_quotas(workspace))).json()["addon_records"]

    assert [r["created_by"] for r in records] == ["active-row"]
    assert records[0]["active_until"].endswith("Z")


async def test_price_and_product_id_are_in_neither_answer(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session)
    product = f"prod_sentinel_{uuid4().hex}"
    price = 987_654_321
    db_session.add(
        WorkspaceAddon(
            workspace_id=workspace.id,
            addon_type="extra_memory",
            quantity=1,
            source="stripe",
            purchase_price_cents=price,
            stripe_product_id=product,
            created_by="buyer",
        )
    )
    await db_session.commit()

    async with admin_http(db_session) as client:
        quotas = await client.get(_quotas(workspace))
        listing = await client.get(LIST, params={"owner_user_id": workspace.owner_user_id})

    assert len(quotas.json()["addon_records"]) == 1
    for response in (quotas, listing):
        assert response.status_code == 200
        assert product not in response.text
        assert str(price) not in response.text
        assert "purchase_price_cents" not in response.text
        assert "stripe_product_id" not in response.text


async def test_quotas_read_answers_422_for_a_bad_id_and_404_for_an_unknown_one(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session)
    async with admin_http(db_session) as client:
        bad = await client.get(f"{LIST}/not-a-uuid/quotas")
        unknown = await client.get(f"{LIST}/{uuid4()}/quotas")
        # The id is read as a UUID, so its other spellings name the same workspace.
        spelled = await client.get(f"{LIST}/{str(workspace.id).replace('-', '').upper()}/quotas")

    assert bad.status_code == 422, bad.text
    assert bad.json()["error"] == "VAL-001"
    assert unknown.status_code == 404, unknown.text
    assert spelled.status_code == 200, spelled.text
    assert spelled.json()["workspace_id"] == str(workspace.id)
