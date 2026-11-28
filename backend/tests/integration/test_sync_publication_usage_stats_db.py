"""The installation-bound read of the public usage statistics, on real OIDC and PostgreSQL (#3035).

``GET /api/v1/workspaces/{wid}/sync-installations/{iid}/publication-usage-stats``
through the installation's registered browser client with ``workspaces:read``:

* the canonical owner gets the owner route's body for the installation's
  public endpoint (routes, callers and daily visitors included), counted in
  the same per-workspace bucket;
* a current member who reaches the installation (the member read of the
  installation admits them) and is not the canonical owner gets ``403
  owner_required``; any other member, a non-member, another workspace or
  installation, and another client the uniform 404; a token without
  ``workspaces:read`` ``403 insufficient_scope``;
* an installation whose public endpoint is not live is the 404;
* a slot-suspended workspace is ``403 SLOT-001``; the owner through a browser
  client without a service registration ``409 RES-002``, as on the domain
  observation; a held installation row ``409 RES-002`` (send the request
  again), as the other rows of the admission.

Every row a test adds (usage events, visitor rows) is deleted afterwards.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import Response
from sqlalchemy import delete, insert, text, update

from api.routes.public_mcp_usage import get_public_mcp_usage_stats
from config.settings import get_settings
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import (
    PMU_CALLER_BOUND_KEY,
    PMU_ROUTE_REST,
    PublicMcpDailyVisitor,
    PublicMcpUsageEvent,
)
from models.workspace_slots import WorkspaceSlotSuspension
from services import public_mcp_usage_stats as stats
from services.public_mcp_usage_events import UsageEventInput, build_usage_event_row
from tests.integration import sync_installation_fixtures as fixtures
from tests.integration.test_sync_publication_domain_commands_db import (
    NOT_FOUND,
    _request,
    installed,
    token_for,
)
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store

OWNER_REQUIRED = {"error": "owner_required"}


@pytest_asyncio.fixture(loop_scope="session")
async def env(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_publication_usage_stats_enabled", True)
    buckets: list[str] = []

    async def counter(key: str, ttl: int | None = None) -> int:
        buckets.append(key)
        return 1

    monkeypatch.setattr(stats, "increment_counter", counter)
    store.buckets = buckets
    try:
        yield store
    finally:
        async with store.factory() as db:
            for model in (PublicMcpUsageEvent, PublicMcpDailyVisitor):
                await db.execute(
                    delete(model).where(model.workspace_id.in_([store.wid, store.other]))
                )
            await db.commit()


async def read(
    store,
    target: dict,
    *,
    role: str = "owner",
    token: str | None = None,
    workspace: UUID | None = None,
    installation: str | None = None,
    query: str = "",
) -> Response:
    path = (
        f"/api/v1/workspaces/{workspace or store.wid}/sync-installations/"
        f"{installation or target['installation_id']}/publication-usage-stats{query}"
    )
    return await _request(store, "GET", path, token or store.tokens[role])


async def seed(store, target: dict) -> None:
    """Two public MCP searches, a bound-key REST search and one visitor of today."""
    now = utcnow()
    rows = []
    for route, caller in (
        ("mcp", "anonymous"),
        ("mcp", "anonymous"),
        (PMU_ROUTE_REST, PMU_CALLER_BOUND_KEY),
    ):
        row = build_usage_event_row(
            UsageEventInput(
                endpoint_id=UUID(target["endpoint_id"]),
                workspace_id=store.wid,
                context_id=UUID(target["context_id"]),
                resource_pk=UUID(target["resource_pk"]),
                resource_id=target["resource_id"],
                tool="recall",
                outcome="success",
                era="stateless",
                started_at=now,
                duration_ms=3,
                route=route,
                caller=caller,
            ),
            key=get_settings().audit_hmac_key,
            session_details_enabled=False,
        ).row
        row["occurred_at"] = now - timedelta(minutes=1)
        rows.append(row)
    async with store.factory() as db:
        await db.execute(insert(PublicMcpUsageEvent), rows)
        await db.execute(
            insert(PublicMcpDailyVisitor).values(
                endpoint_id=UUID(target["endpoint_id"]),
                day=now.date(),
                visitor_key="0" * 32,
                workspace_id=store.wid,
            )
        )
        await db.commit()


async def owner_route(store, target: dict, days: int) -> dict:
    async with store.factory() as db:
        response = await get_public_mcp_usage_stats(
            owner=(store.users["owner"], store.wid),
            resource_id=target["resource_id"],
            days=days,
            db=db,
        )
    return response.model_dump(mode="json")


def _without_time(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "generated_at"}


async def test_the_owner_reads_the_owner_routes_body_for_the_installation(env):
    store = env
    target = await installed(store)
    await seed(store, target)

    response = await read(store, target)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert _without_time(body) == _without_time(await owner_route(store, target, 7))
    assert body["endpoint_id"] == target["endpoint_id"]
    assert body["resource_id"] == target["resource_id"]
    assert body["window_days"] == 7
    assert body["totals"]["calls"] == 3
    assert body["totals"]["calls_by_route"] == {"mcp": 2, "rest": 1}
    assert body["totals"]["calls_by_caller"] == {"anonymous": 2, "bound_key": 1}
    assert body["visitors"]["status"] == "ok"
    assert body["days"][-1]["visitors"] == 1

    shorter = await read(store, target, query="?days=2")
    assert shorter.status_code == 200, shorter.text
    assert _without_time(shorter.json()) == _without_time(await owner_route(store, target, 2))
    # Both routes count in the workspace's one bucket.
    assert len(store.buckets) == 4
    assert {key.rsplit(":", 1)[0] for key in store.buckets} == {f"pmcp_usage_stats_rl:{store.wid}"}


async def test_a_member_who_reaches_the_installation_is_owner_required(env):
    """403 exactly for the members the member read of the installation admits;
    a member it refuses (here: no read of the installation's context) gets its 404."""
    store = env
    target = await installed(store)
    inspection = f"/api/v1/workspaces/{store.wid}/sync-installations/{target['installation_id']}"
    reached = set()
    for role in ("admin", "member", "viewer", "next"):
        inspected = await _request(store, "GET", inspection, store.tokens[role])
        response = await read(store, target, role=role)
        if inspected.status_code == 200:
            reached.add(role)
            assert (response.status_code, response.json()) == (403, OWNER_REQUIRED), role
        else:
            assert (response.status_code, response.content) == (404, NOT_FOUND), role
    assert reached == {"admin", "viewer"}
    assert store.buckets == []


async def test_everyone_else_gets_the_uniform_404(env):
    store = env
    target = await installed(store)
    response = await read(store, target, role="outsider")
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    for selection in (
        {"workspace": store.other},
        {"installation": str(uuid4())},
        {"workspace": store.other, "installation": str(uuid4())},
    ):
        for role in ("owner", "member"):
            response = await read(store, target, role=role, **selection)
            assert (response.status_code, response.content) == (404, NOT_FOUND), (role, selection)
    # A member through another browser client does not reach the installation.
    foreign = await token_for(store, "member", client_id=store.foreign_client)
    response = await read(store, target, token=foreign)
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    assert store.buckets == []


async def test_a_token_without_workspaces_read_is_insufficient_scope(env):
    store = env
    target = await installed(store)
    for role in ("owner", "member"):
        manager = await token_for(store, role, scope="openid integrations:manage")
        response = await read(store, target, token=manager)
        assert (response.status_code, response.json()) == (403, {"error": "insufficient_scope"})
    assert store.buckets == []


async def test_an_installation_without_a_live_public_endpoint_is_404(env):
    store = env
    target = await installed(store)
    async with store.factory() as db:
        await db.execute(
            update(PublicMcpEndpoint)
            .where(PublicMcpEndpoint.id == UUID(target["endpoint_id"]))
            .values(revoked_at=utcnow())
        )
        await db.commit()

    response = await read(store, target)
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    assert store.buckets == []


async def test_a_slot_suspended_workspace_is_slot_001(env):
    store = env
    target = await installed(store)
    with patch("services.workspace_slots.workspace_slot_mode", return_value="enforce"):
        async with store.factory() as db:
            db.add(
                WorkspaceSlotSuspension(
                    workspace_id=store.wid,
                    owner_user_id=store.users["owner"],
                    cause="billing_snapshot",
                    suspended_at=utcnow(),
                )
            )
            await db.commit()
        try:
            response = await read(store, target)
        finally:
            async with store.factory() as db:
                await db.execute(
                    delete(WorkspaceSlotSuspension).where(
                        WorkspaceSlotSuspension.workspace_id == store.wid
                    )
                )
                await db.commit()
    assert (response.status_code, response.json()) == (403, {"error": "SLOT-001"})
    assert store.buckets == []


async def test_the_owner_through_an_unregistered_client_is_refused_as_on_the_observation(env):
    store = env
    target = await installed(store)
    foreign = await token_for(
        store, "owner", client_id=store.foreign_client, scope="openid workspaces:read"
    )
    response = await read(store, target, token=foreign)
    assert (response.status_code, response.json()) == (409, {"error": "RES-002"})
    assert store.buckets == []


async def test_a_held_installation_row_is_res_002(env):
    """The installation row is read ``FOR SHARE NOWAIT``: a transaction that
    updates it (an unlink, a resume, a management transition) makes the read
    answer ``409 RES-002``, as a held row of the rest of the admission does."""
    store = env
    target = await installed(store)
    holder = store.factory()
    try:
        await holder.execute(
            text("SELECT id FROM sync_installations WHERE id = :id FOR UPDATE"),
            {"id": UUID(target["installation_id"])},
        )
        response = await read(store, target)
        assert (response.status_code, response.json()) == (409, {"error": "RES-002"})
    finally:
        await holder.rollback()
        await holder.close()
    assert store.buckets == []
    response = await read(store, target)
    assert response.status_code == 200, response.text
