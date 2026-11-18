"""Every ordinary ingest route shares the feed ownership fence (#2906).

Real PostgreSQL sessions: the fence guards are no-ops for mocked sessions (see
``tests/conftest.py``), so only a database proves which routes are fenced. The
routes walked are the single-event and batch resource-token routes, the
management-grant ``POST /events`` route and the MCP ``ingest_events`` tool.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update

from auth.resource_tokens import ResourceTokenManager
from config.settings import get_settings
from mcp_server.tools.resource import handle_ingest_events
from models.auth import Workspace
from models.public_mcp import PublicMcpEndpoint
from models.resource import ResourceEvent, ResourceSchema
from models.sync_installation import SyncInstallation, SyncService
from models.sync_management import SyncManagedTarget
from services.sync_managed_guard import target_fence
from tests.observability_helpers import app_client, owner_client, world_rows

pytestmark = pytest.mark.asyncio(loop_scope="session")

MANAGEMENT = "/api/v1/resource-management/events"
GRANTS = "/api/v1/resource-management-grants"
OPS = ["schema:read", "events:upsert", "events:delete"]
FENCED_MODES = ["feed", "stopping", "retained"]


@pytest_asyncio.fixture(loop_scope="session")
async def scenario(async_engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(settings, "sync_managed_enabled", True)
    # Quota (Redis), indexer scheduling and usage logging are not under test.
    for target in (
        "api.routes.resource_ingest.check_event_quota",
        "api.routes.resource_management.check_event_quota",
        "mcp_server.tools.resource.check_event_quota",
        "api.routes.resource_ingest._schedule_indexer_for_resource",
        "api.routes.resource_management._schedule_indexer_for_resource",
        "mcp_server.tools.resource._log_tool_usage",
        "utils.usage_logger.log_usage",
    ):
        monkeypatch.setattr(target, AsyncMock())
    async with world_rows(async_engine) as w:
        async with w.factory() as s:
            await s.execute(
                update(Workspace).where(Workspace.id == w.ws).values(plan_name="promax")
            )
            endpoint = PublicMcpEndpoint(
                id=uuid4(),
                workspace_id=w.ws,
                resource_pk=w.resource_pk["a1"],
                context_id=w.ctx["a1"],
                resource_id=w.slugs["a1"],
                public_name="Synthetic public resource",
                created_by=w.owner,
            )
            s.add(endpoint)
            s.add(
                ResourceSchema(
                    resource_pk=w.resource_pk["a1"],
                    resource_id=w.slugs["a1"],
                    schema_version=1,
                    field_definitions=[
                        {"name": "name", "type": "text", "classification": "public"}
                    ],
                )
            )
            token, _ = await ResourceTokenManager(s).create_token(
                w.slugs["a1"],
                resource_pk=w.resource_pk["a1"],
                workspace_id=w.ws,
                description="Fence walk",
                created_by=w.owner,
                actor_user_id=w.owner,
            )
            await s.commit()
        async with owner_client(w, w.owner) as c:
            result = await c.post(
                GRANTS,
                json={
                    "resource_pk": str(w.resource_pk["a1"]),
                    "context_id": str(w.ctx["a1"]),
                    "endpoint_id": str(endpoint.id),
                    "operations": OPS,
                    "approved_fields": ["name"],
                    "ttl_days": 1,
                    "quota_events_per_hour": 100,
                    "max_active_tokens": 2,
                    "acknowledge_full_resource_token_authority": True,
                },
            )
            assert result.status_code == 201, result.text
        w.endpoint_id = endpoint.id  # type: ignore[attr-defined]
        w.token = token  # type: ignore[attr-defined]
        w.grant = result.json()  # type: ignore[attr-defined]
        yield w


@contextlib.asynccontextmanager
async def managed_as(w, mode: str) -> AsyncIterator[None]:
    """Make target a1 feed-owned in ``mode``; ``retained`` = installation not enrolled yet."""
    service_id = "fence-" + uuid4().hex[:12]
    async with w.factory() as s:
        s.add(
            SyncService(
                service_id=service_id,
                browser_client_id=service_id + "-browser",
                service_client_id=service_id + "-service",
            )
        )
        await s.flush()
        installation = SyncInstallation(
            workspace_id=w.ws,
            service_id=service_id,
            service_client_id=service_id + "-service",
            authorization_id=uuid4(),
            application_id="fence-walk",
            operations=[],
            resource_pk=w.resource_pk["a1"],
            context_id=w.ctx["a1"],
            endpoint_id=w.endpoint_id,
            resource_id=w.slugs["a1"],
        )
        s.add(installation)
        await s.flush()
        if mode != "retained":
            s.add(
                SyncManagedTarget(
                    installation_id=installation.id,
                    resource_pk=w.resource_pk["a1"],
                    context_id=w.ctx["a1"],
                    mode=mode,
                    generation=2 if mode == "stopping" else 1,
                    drain_generation=1 if mode == "stopping" else None,
                )
            )
        await s.commit()
    try:
        yield
    finally:
        async with w.factory() as s:
            await s.execute(delete(SyncInstallation).where(SyncInstallation.workspace_id == w.ws))
            await s.execute(delete(SyncService).where(SyncService.service_id == service_id))
            await s.commit()


async def event_count(w) -> int:
    async with w.factory() as s:
        return await s.scalar(
            select(func.count())
            .select_from(ResourceEvent)
            .where(ResourceEvent.resource_pk == w.resource_pk["a1"])
        )


def event(doc_id: str, **extra) -> dict:
    return {
        "op": "upsert",
        "doc_id": doc_id,
        "version": 1,
        "payload": {"name": "Synthetic"},
        **extra,
    }


def token_headers(w) -> dict:
    return {"X-Resource-API-Key": w.token}


def grant_headers(w) -> dict:
    return {"authorization": "Bearer " + w.grant["secret"], "x-management-actor": "a" * 64}


def management_body(w, *events: dict) -> dict:
    return {
        "operation_id": str(uuid4()),
        "schema_hash": w.grant["schema_hash"],
        "events": [{**e, "idempotency_key": str(uuid4())} for e in events],
    }


async def mcp_ingest(w, *events: dict) -> dict:
    async def _get_db():
        async with w.factory() as s:
            yield s

    with patch("db.base.get_db", new=_get_db):
        result = await handle_ingest_events(
            {"resource_id": w.slugs["a1"], "events": list(events)}, w.owner, w.ws
        )
    return json.loads(result[0].text)


@pytest.mark.parametrize("mode", FENCED_MODES)
async def test_single_event_route_is_refused_on_a_feed_managed_target(scenario, mode):
    w = scenario
    before = await event_count(w)
    async with managed_as(w, mode), app_client(w) as c:
        response = await c.post(
            f"/api/v1/resources/{w.slugs['a1']}/events",
            headers=token_headers(w),
            json=event("single-" + mode),
        )
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "SYNC-LOCKED"
    assert response.json()["details"]["management_mode"] == ("feed" if mode == "retained" else mode)
    assert await event_count(w) == before


@pytest.mark.parametrize("mode", FENCED_MODES)
async def test_every_ordinary_ingest_route_is_fenced_on_a_feed_managed_target(scenario, mode):
    w = scenario
    before = await event_count(w)
    shown = "feed" if mode == "retained" else mode
    async with managed_as(w, mode):
        async with app_client(w) as c:
            single = await c.post(
                f"/api/v1/resources/{w.slugs['a1']}/events",
                headers=token_headers(w),
                json=event("walk-single"),
            )
            batch = await c.post(
                f"/api/v1/resources/{w.slugs['a1']}/events/batch",
                headers=token_headers(w),
                json={"events": [event("walk-batch")]},
            )
            managed = await c.post(
                MANAGEMENT,
                headers=grant_headers(w),
                json=management_body(w, event("walk-management")),
            )
        for response in (single, batch, managed):
            assert response.status_code == 409, response.text
            assert response.json()["error"] == "SYNC-LOCKED"
        mcp = await mcp_ingest(w, event("walk-mcp"))
        assert mcp["status"] == "error"
        assert mcp["management_mode"] == shown
        assert "feed-managed" in mcp["message"]
    assert await event_count(w) == before


@pytest.mark.parametrize("mode", ["unmanaged", "manual"])
async def test_single_event_route_keeps_its_201_idempotency_and_conflict_contract(scenario, mode):
    w = scenario
    doc_id = f"open-{mode}-{uuid4().hex[:6]}"
    body = event(doc_id, idempotency_key="key-" + doc_id)
    url = f"/api/v1/resources/{w.slugs['a1']}/events"
    async with contextlib.AsyncExitStack() as stack:
        if mode == "manual":
            await stack.enter_async_context(managed_as(w, "manual"))
        c = await stack.enter_async_context(app_client(w))
        created = await c.post(url, headers=token_headers(w), json=body)
        assert created.status_code == 201, created.text
        event_id = created.json()["event_id"]
        assert created.json() == {
            "status": "success",
            "event_id": event_id,
            "queued": True,
            "estimated_indexing_time_seconds": None,
        }
        replay = await c.post(url, headers=token_headers(w), json=body)
        assert replay.status_code == 201
        assert replay.json() == {
            "status": "success",
            "event_id": event_id,
            "queued": False,
            "estimated_indexing_time_seconds": 0,
        }
        duplicate = await c.post(
            url, headers=token_headers(w), json={**body, "idempotency_key": "other-" + doc_id}
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"] == "RES-002"
    async with w.factory() as s:
        row = await s.get(ResourceEvent, event_id)
        assert row is not None and row.event_metadata == {}


@pytest.mark.parametrize("key", ["sync_run_id", "installation_generation"])
async def test_sync_reserved_metadata_is_422_on_every_ordinary_ingest_route(scenario, key):
    w = scenario
    before = await event_count(w)
    metadata = {"event_metadata": {key: 1}}
    async with app_client(w) as c:
        single = await c.post(
            f"/api/v1/resources/{w.slugs['a1']}/events",
            headers=token_headers(w),
            json=event("reserved-single", **metadata),
        )
        batch = await c.post(
            f"/api/v1/resources/{w.slugs['a1']}/events/batch",
            headers=token_headers(w),
            json={"events": [event("reserved-ok"), event("reserved-batch", **metadata)]},
        )
        # The management event schema has no event_metadata field at all.
        managed = await c.post(
            MANAGEMENT,
            headers=grant_headers(w),
            json=management_body(w, event("reserved-management", **metadata)),
        )
    for response in (single, batch):
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "VAL-001"
        assert response.json()["details"]["field"] == "event_metadata"
    assert batch.json()["details"]["index"] == 1
    assert managed.status_code == 422, managed.text
    mcp = await mcp_ingest(w, event("reserved-ok"), event("reserved-mcp", **metadata))
    assert mcp["error"] == "validation_error"
    assert mcp["index"] == 1
    assert await event_count(w) == before


async def test_single_event_route_holds_the_shared_fence_through_the_insert(scenario):
    """A transition holding the exclusive fence makes the single route retry, not write."""
    w = scenario
    before = await event_count(w)
    url = f"/api/v1/resources/{w.slugs['a1']}/events"
    async with w.factory() as transition:
        await target_fence(transition, UUID(str(w.ctx["a1"])), exclusive=True)
        async with app_client(w) as c:
            refused = await c.post(url, headers=token_headers(w), json=event("during-transition"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["message"] == "Feed management is changing; retry"
        await transition.rollback()
    assert await event_count(w) == before
    async with app_client(w) as c:
        accepted = await c.post(url, headers=token_headers(w), json=event("after-transition"))
    assert accepted.status_code == 201, accepted.text
