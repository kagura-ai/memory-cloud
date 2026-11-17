"""Atomic plan/quota/paid-right snapshots and fencing of late legacy writers."""

import asyncio
from datetime import UTC, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from api.routes import internal_billing
from config.billing_addons import ADDON_COLUMNS
from models.auth import Context, Workspace
from models.integration_entitlement import WorkspaceIntegrationEvent, WorkspaceIntegrationState
from models.public_usage_budget import PublicUsageBudget
from models.workspace_billing_schemas import WorkspaceBillingPush
from services import integration_entitlements as service
from tests.integration.test_integration_entitlements_db import NOW, snapshot
from tests.integration.test_integration_entitlements_db import store as store

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def ordered(store):
    store.app.include_router(internal_billing.router)
    return store


def complete(revision=1, *, plan="promax", addons=None, services=None):
    return {
        **snapshot(revision, services=services),
        "contract_version": "workspace-billing-snapshot-v1",
        "plan": {
            "plan_name": plan,
            "addons": {**dict.fromkeys(ADDON_COLUMNS, 0), **(addons or {})},
        },
    }


async def call(store, body=None, *, suffix="billing-snapshot", workspace=None):
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://testserver"
    ) as client:
        return await client.request(
            "GET" if body is None else "PUT",
            f"/internal/workspaces/{workspace or store.wid}/{suffix}",
            json=body,
            headers={"Authorization": "Bearer test-billing-service"},
        )


async def test_read_does_not_opt_in_and_legacy_workspaces_keep_existing_behavior(ordered):
    s = ordered
    before = await call(s)
    assert before.status_code == 200 and not before.json()["ordered_billing"]
    assert before.json()["revision"] == 0
    legacy = await call(s, {"plan_name": "pro", "addons": {"memory": 8}}, suffix="plan")
    assert legacy.status_code == 200
    assert legacy.json()["addons"]["memory"] == 8
    async with s.factory() as db:
        assert await db.get(WorkspaceIntegrationState, s.wid) is None


async def test_atomic_write_persists_complete_plan_quotas_and_all_service_rights(ordered):
    s = ordered
    body = complete(addons={key: index + 1 for index, key in enumerate(ADDON_COLUMNS)})
    response = await call(s, body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ordered_billing"] and result["applied"] and result["outcome"] == "applied"
    assert result["plan"] == body["plan"] and result["plan_name"] == "promax"
    assert result["entitlement_source"] == "external_billing"
    assert result["revision"] == 1 and len(result["services"]) == 1
    assert response.headers["cache-control"] == "no-store"
    async with s.factory() as db:
        assert (await service.inspect_paid_integration(db, s.wid, "catalog-sync")).allowed
        assert (await db.get(Context, s.cid)).display_name == "Original"
        assert (await db.get(WorkspaceIntegrationState, s.wid)).ordered_billing
    other = await call(s, workspace=s.other)
    assert other.json()["revision"] == 0 and not other.json()["ordered_billing"]


async def test_exact_duplicate_and_reordering_return_current_coherent_whole_state(ordered):
    s = ordered
    old = complete(addons={"memory": 9})
    assert (await call(s, old)).status_code == 200
    newer = complete(3, plan="free", services=[])
    newer["max_access"] = None
    assert (await call(s, newer)).status_code == 200
    for body, outcome in ((old, "duplicate"), (complete(2), "stale")):
        response = await call(s, body)
        assert response.status_code == 200
        result = response.json()
        assert not result["applied"] and result["outcome"] == outcome
        assert result["revision"] == 3 and result["plan"]["plan_name"] == "free"
        assert set(result["plan"]["addons"].values()) == {0}
        assert result["max_access"] is None and result["services"] == []
    async with s.factory() as db:
        assert not (await service.inspect_paid_integration(db, s.wid, "catalog-sync")).allowed


@pytest.mark.parametrize("changed", ["plan", "addon", "window", "event"])
async def test_event_or_revision_reuse_includes_plan_and_addon_content(ordered, changed):
    s = ordered
    body = complete()
    assert (await call(s, body)).status_code == 200
    if changed == "plan":
        body["plan"]["plan_name"] = "pro"
    elif changed == "addon":
        body["plan"]["addons"]["memory"] = 2
    elif changed == "window":
        body["max_access"]["valid_until"] = (
            (NOW + timedelta(days=8)).replace(tzinfo=UTC).isoformat()
        )
    else:
        body["event_id"] = "changed-event"
    assert (await call(s, body)).status_code == 409
    assert (await call(s)).json()["plan"] == complete()["plan"]


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("plan", {"plan_name": "free", "addons": {}}),
        ("integration-entitlements", snapshot(20, services=[])),
    ],
)
async def test_legacy_billing_writers_cannot_bypass_opted_in_revision(ordered, suffix, body):
    s = ordered
    assert (await call(s, complete(4, addons={"memory": 7}))).status_code == 200
    assert (await call(s, body, suffix=suffix)).status_code == 409
    current = (await call(s)).json()
    assert current["revision"] == 4 and current["plan"]["addons"]["memory"] == 7
    assert len(current["services"]) == 1


async def wait_for_database_waiter(store):
    for _ in range(100):
        async with store.factory() as db:
            if await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                    "AND cardinality(pg_blocking_pids(pid))>0)"
                )
            ):
                return
        await asyncio.sleep(0.01)
    pytest.fail("The legacy request never reached the real workspace lock")


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("plan", {"plan_name": "free"}),
        ("integration-entitlements", snapshot(30, services=[])),
    ],
)
async def test_old_writer_waiting_on_workspace_lock_is_fenced_after_opt_in(ordered, suffix, body):
    s = ordered
    async with s.factory() as db:
        await db.execute(select(Workspace).where(Workspace.id == s.wid).with_for_update())
        blocked = asyncio.create_task(call(s, body, suffix=suffix))
        try:
            await wait_for_database_waiter(s)
            assert not blocked.done()
            value = WorkspaceBillingPush.model_validate(complete(8))
            await service.apply_integration_entitlements(
                db,
                s.wid,
                service.IntegrationEntitlementPush.model_validate(
                    value.model_dump(exclude={"contract_version", "plan"})
                ),
                billing_plan=value.plan,
            )
            await db.commit()
            assert (await asyncio.wait_for(blocked, 2)).status_code == 409
        finally:
            if not blocked.done():
                blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)
    current = (await call(s)).json()
    assert current["revision"] == 8 and current["plan_name"] == "promax"


async def test_exception_after_plan_mutation_rolls_back_entire_snapshot_and_mode(
    ordered, monkeypatch
):
    s = ordered

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic delivery failure")

    monkeypatch.setattr(service, "_replace_licenses", broken)
    async with s.factory() as db:
        body = WorkspaceBillingPush.model_validate(complete(plan="pro", addons={"memory": 77}))
        with pytest.raises(RuntimeError):
            await service.apply_integration_entitlements(
                db,
                s.wid,
                service.IntegrationEntitlementPush.model_validate(
                    body.model_dump(exclude={"contract_version", "plan"})
                ),
                billing_plan=body.plan,
            )
        await db.rollback()
    current = (await call(s)).json()
    assert not current["ordered_billing"] and current["revision"] == 0
    assert current["plan"]["plan_name"] == "promax"
    assert set(current["plan"]["addons"].values()) == {0}
    async with s.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkspaceIntegrationEvent)
                .where(WorkspaceIntegrationEvent.workspace_id == s.wid)
            )
            == 0
        )


async def test_concurrent_full_snapshots_preserve_newer_whole_state(ordered):
    s = ordered
    old = complete(1, addons={"memory": 4})
    new = complete(2, plan="free", addons={"memory": 8}, services=[])
    new["max_access"] = None
    responses = await asyncio.gather(call(s, new), call(s, old))
    assert all(response.status_code == 200 for response in responses)
    view = (await call(s)).json()
    assert view["revision"] == 2 and view["plan"] == new["plan"]
    assert view["plan_name"] == "free" and view["max_access"] is None and view["services"] == []


async def test_complete_replacement_keeps_unrelated_services_and_clears_old_quotas(ordered):
    s = ordered
    async with s.factory() as db:
        db.add(
            PublicUsageBudget(
                workspace_id=s.wid,
                period_started_at=NOW,
                calls_used=24999,
                period_seq=8,
                updated_at=NOW,
            )
        )
        await db.commit()
    first = complete(addons={"connector": 9, "memory": 5})
    second_service = {**first["services"][0], "service_id": "other-service"}
    first["services"].append(second_service)
    assert (await call(s, first)).status_code == 200
    second = complete(2, services=[second_service])
    assert (await call(s, second)).status_code == 200
    view = (await call(s)).json()
    assert [item["service_id"] for item in view["services"]] == ["other-service"]
    assert set(view["plan"]["addons"].values()) == {0}
    async with s.factory() as db:
        assert (await db.get(Context, s.cid)).display_name == "Original"
        budget = await db.get(PublicUsageBudget, s.wid)
        assert (budget.period_started_at, budget.calls_used, budget.period_seq) == (NOW, 24999, 8)


async def test_read_waits_for_the_whole_committed_plan_and_rights_state(ordered):
    s = ordered
    async with s.factory() as db:
        await db.execute(select(Workspace).where(Workspace.id == s.wid).with_for_update())
        pending = asyncio.create_task(call(s))
        try:
            await wait_for_database_waiter(s)
            value = WorkspaceBillingPush.model_validate(complete(plan="free", services=[]))
            await service.apply_integration_entitlements(
                db,
                s.wid,
                service.IntegrationEntitlementPush.model_validate(
                    value.model_dump(exclude={"contract_version", "plan"})
                ),
                billing_plan=value.plan,
            )
            await db.commit()
            answer = await asyncio.wait_for(pending, 2)
            view = answer.json()
            assert view["plan_name"] == view["plan"]["plan_name"] == "free"
            assert view["ordered_billing"] and view["revision"] == 1 and view["services"] == []
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_stale_first_complete_request_does_not_disable_existing_publishers(ordered):
    s = ordered
    assert (await call(s, snapshot(6), suffix="integration-entitlements")).status_code == 200
    stale = await call(s, complete(5))
    assert stale.status_code == 200 and stale.json()["outcome"] == "stale"
    assert not stale.json()["ordered_billing"]
    assert (await call(s, {"plan_name": "promax"}, suffix="plan")).status_code == 200
    applied = await call(s, complete(7))
    assert applied.status_code == 200 and applied.json()["ordered_billing"]


async def test_administrator_override_is_visible_without_clearing_ordered_mode(ordered):
    s = ordered
    assert (await call(s, complete())).status_code == 200
    async with s.factory() as db:
        workspace = await db.get(Workspace, s.wid)
        workspace.plan_name = "pro"
        workspace.entitlement_source = "admin_grant"
        await db.commit()
    view = (await call(s)).json()
    assert view["plan_name"] == view["plan"]["plan_name"] == "pro"
    assert view["entitlement_source"] == "admin_grant" and view["ordered_billing"]
    assert view["revision"] == 1
    assert (await call(s, {"plan_name": "promax"}, suffix="plan")).status_code == 409


async def test_missing_workspace_cannot_receive_or_disclose_a_complete_snapshot(ordered):
    missing = uuid4()
    assert (await call(ordered, workspace=missing)).status_code == 404
    assert (await call(ordered, complete(), workspace=missing)).status_code == 404
