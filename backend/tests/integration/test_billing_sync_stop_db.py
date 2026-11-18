"""Billing deliveries commit when the sync stop that follows them has to wait (#2913).

A delivery that ends paid eligibility stops the workspace's synchronization in
the same transaction. When another transaction holds the shared target fence
(the resource indexer, any ordinary write to a manual target), that stop cannot
take its exclusive fence. For a managed target the delivery now commits and the
maintenance tick finishes the stop; a legacy installation without a managed
row is refused with a retryable 409 ``sync_transition_busy``, never with 404
``RES-001``. Every test holds the fence from a second real PostgreSQL session.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from api.routes import internal_integrations
from db.base import get_db
from models.auth import Context, Workspace
from models.integration_entitlement import (
    WorkspaceIntegrationEvent,
    WorkspaceIntegrationLicense,
    WorkspaceIntegrationState,
)
from models.sync_installation import SyncInstallation
from models.sync_management import SyncManagedTarget
from models.workspace_billing_schemas import BILLING_V1_ADDON_KEYS
from services import sync_management
from services.sync_managed_guard import target_fence
from tests.integration import test_sync_management_db as fixtures
from tests.integration.sync_feed_fixtures import committed, product
from utils.exceptions import MemoryCloudException

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, managed = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.managed
ROUTES = ["billing-snapshot", "integration-entitlements"]


def body(worker, revision, *, paid=False, memory=0, ordered=True):
    """A complete delivery; ``paid=False`` leaves the sync service right out."""
    now = worker.store.clock.now
    window = {
        "valid_from": (now - timedelta(hours=1)).replace(tzinfo=UTC).isoformat(),
        "valid_until": (now + timedelta(days=14)).replace(tzinfo=UTC).isoformat(),
        "evidence_ref": "confirmed-term",
    }
    result = {
        "revision": revision,
        "event_id": f"stop-{revision}",
        "source_ref": "workspace-snapshot",
        "max_access": window,
        "services": [{"service_id": worker.store.service_id, **window}] if paid else [],
    }
    if ordered:
        result["contract_version"] = "workspace-billing-snapshot-v1"
        result["plan"] = {
            "plan_name": "promax",
            "addons": {**dict.fromkeys(BILLING_V1_ADDON_KEYS, 0), "memory": memory},
        }
    return result


async def deliver(worker, revision, *, route="billing-snapshot", **kwargs):
    """PUT a delivery through the real internal route and its global error envelope."""
    app = FastAPI()
    app.include_router(internal_integrations.router)

    async def sessions():
        async with worker.factory() as db:
            yield db

    @app.exception_handler(MemoryCloudException)
    async def canonical(_request, exc):
        return JSONResponse(
            {"error": exc.error_code, "message": exc.message, "details": exc.details},
            status_code=exc.status_code,
        )

    app.dependency_overrides[get_db] = sessions
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        return await client.put(
            f"/internal/workspaces/{worker.store.wid}/{route}",
            json=body(worker, revision, ordered=route == "billing-snapshot", **kwargs),
            headers={"Authorization": "Bearer fixture-billing-2802"},
        )


def hold_shared_fence(worker):
    """What the resource indexer or an ordinary write to a manual target holds."""
    return FenceHolder(worker, exclusive=False)


class FenceHolder:
    """A second session holding the target fence until the block exits."""

    def __init__(self, worker, *, exclusive):
        self.worker, self.exclusive = worker, exclusive

    async def __aenter__(self):
        self.db = self.worker.factory()
        session = await self.db.__aenter__()
        await target_fence(
            session, UUID(self.worker.target["context_id"]), exclusive=self.exclusive
        )
        return session

    async def __aexit__(self, *exc):
        await self.db.__aexit__(*exc)


async def delivered(worker, revision):
    """The delivery's rows: state revision, license count, receipt, ordered flag."""
    async with worker.factory() as db:
        state = await db.get(WorkspaceIntegrationState, worker.store.wid)
        licenses = await db.scalar(
            select(func.count())
            .select_from(WorkspaceIntegrationLicense)
            .where(WorkspaceIntegrationLicense.workspace_id == worker.store.wid)
        )
        receipt = await db.get(WorkspaceIntegrationEvent, (worker.store.wid, f"stop-{revision}"))
        workspace = await db.get(Workspace, worker.store.wid)
        return state.revision, licenses, receipt is not None, state.ordered_billing, workspace


async def target(worker):
    async with worker.factory() as db:
        return await db.get(SyncManagedTarget, worker.iid)


async def maintain(worker):
    async with worker.factory() as db:
        await sync_management.maintain_sync_management(db)


async def test_delivery_that_ends_eligibility_commits_while_the_shared_fence_is_held(managed):
    # A committed record keeps the stop draining, so the tick leaves it at stopping.
    await committed(managed, [product()])
    before = await target(managed)
    assert before.mode == "feed"
    async with managed.factory() as db:
        generation = (await db.get(SyncInstallation, managed.iid)).generation
    async with hold_shared_fence(managed):
        response = await deliver(managed, 10, memory=3)
        assert response.status_code == 200, response.text
        assert response.json()["applied"] and response.json()["services"] == []
        # Read after the stop's savepoint was rolled back: the delivery's own state.
        assert response.json()["plan"]["addons"]["memory"] == 3
        revision, licenses, receipt, ordered, workspace = await delivered(managed, 10)
        assert (revision, licenses, receipt, ordered) == (10, 0, True, True)
        assert workspace.addon_memory_bonus == 3
        # The stop waits for the fence; the delivery did not, and nothing of
        # the rolled-back stop (generation bump, mode, epoch) was kept.
        assert ((await target(managed)).mode, (await target(managed)).epoch) == (
            "feed",
            before.epoch,
        )
        async with managed.factory() as db:
            assert (await db.get(SyncInstallation, managed.iid)).generation == generation
    await maintain(managed)
    after = await target(managed)
    assert after.mode == "stopping", after.mode
    assert after.stop_reason == "entitlement_ended"
    assert after.epoch == before.epoch + 1


async def test_maintenance_while_the_fence_is_still_held_keeps_the_stop_retryable(managed):
    async with hold_shared_fence(managed):
        assert (await deliver(managed, 10)).status_code == 200
        await maintain(managed)
        assert (await target(managed)).mode == "feed"
    await maintain(managed)
    assert (await target(managed)).stop_reason == "entitlement_ended"
    assert (await target(managed)).mode in {"stopping", "manual"}


@pytest.mark.parametrize("mode", ["stopping", "manual"])
@pytest.mark.parametrize("route", ROUTES)
async def test_plan_only_delivery_to_a_stopped_target_ignores_a_shared_fence_holder(
    managed, mode, route
):
    # The first delivery ends eligibility and stops the target in its transaction.
    assert (await deliver(managed, 10, route=route)).status_code == 200
    if mode == "manual":
        await fixtures.finalize(managed)
    stopped = await target(managed)
    assert stopped.mode == mode
    async with hold_shared_fence(managed):
        response = await deliver(managed, 11, route=route, memory=5)
        assert response.status_code == 200, response.text
    revision, _licenses, receipt, _ordered, workspace = await delivered(managed, 11)
    assert (revision, receipt) == (11, True)
    if route == "billing-snapshot":
        assert workspace.addon_memory_bonus == 5
    after = await target(managed)
    assert (after.mode, after.epoch, after.generation) == (
        stopped.mode,
        stopped.epoch,
        stopped.generation,
    )


@pytest.mark.parametrize("route", ROUTES)
async def test_delivery_for_a_busy_managed_target_never_answers_409_or_404(managed, route):
    async with FenceHolder(managed, exclusive=True):
        response = await deliver(managed, 10, route=route)
        assert response.status_code == 200, response.text
    assert (await delivered(managed, 10))[:3] == (10, 0, True)
    await maintain(managed)
    assert (await target(managed)).stop_reason == "entitlement_ended"


@pytest.fixture
def legacy(feeds, monkeypatch):
    """An installation from before the barrier: management on, no managed row."""
    monkeypatch.setattr(sync_management, "_maintenance_scan", sync_management.MaintenanceScan())
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", True)
    return feeds


def assert_busy(response):
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "RES-002"
    assert response.json()["details"] == {"reason": "sync_transition_busy"}


@pytest.mark.parametrize("route", ROUTES)
async def test_legacy_delivery_behind_a_fence_holder_is_a_retryable_busy_409(legacy, route):
    async with hold_shared_fence(legacy):
        assert_busy(await deliver(legacy, 10, route=route))
    async with legacy.factory() as db:
        assert (await db.get(WorkspaceIntegrationState, legacy.store.wid)).revision < 10
        assert await db.get(WorkspaceIntegrationEvent, (legacy.store.wid, "stop-10")) is None
    # The same delivery retried once the holder is gone applies and stops.
    response = await deliver(legacy, 10, route=route)
    assert response.status_code == 200, response.text
    row = await target(legacy)
    assert row.mode == "stopping" and row.stop_reason == "entitlement_ended"


@pytest.mark.parametrize("route", ROUTES)
async def test_legacy_delivery_with_a_deleted_context_is_never_404(legacy, route):
    async with legacy.factory() as db:
        context = await db.get(Context, UUID(legacy.target["context_id"]))
        context.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
    response = await deliver(legacy, 10, route=route)
    assert_busy(response)
    assert response.json()["error"] != "RES-001"
    async with legacy.factory() as db:
        assert await db.get(SyncManagedTarget, legacy.iid) is None
        assert await db.get(SyncInstallation, legacy.iid) is not None


async def test_eligible_delivery_ignores_a_busy_fence(legacy):
    async with FenceHolder(legacy, exclusive=True):
        response = await deliver(legacy, 10, paid=True, memory=2)
        assert response.status_code == 200, response.text
    assert (await delivered(legacy, 10))[:3] == (10, 1, True)
    assert await target(legacy) is None
