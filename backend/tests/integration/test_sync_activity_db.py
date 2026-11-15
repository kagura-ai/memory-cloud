"""Metadata visibility, trusted manual provenance and non-reflective audit boundaries."""

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from models.auth import AuditLog
from models.public_usage_budget import PublicUsageBudget
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product
from tests.integration.sync_feed_fixtures import request as authenticated_request

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


@pytest_asyncio.fixture(loop_scope="session")
async def activity(feeds, monkeypatch):
    from api.routes import sync_activity as routes
    from services import sync_activity, sync_audit, sync_requests

    monkeypatch.setattr(sync_activity.get_settings(), "sync_observability_enabled", True)
    for module in (sync_activity, sync_requests, sync_audit):
        monkeypatch.setattr(module, "utcnow", lambda: feeds.store.clock.now)
    feeds.store.app.include_router(routes.router, prefix="/api/v1")
    return feeds


async def test_metadata_read_is_passive_private_and_distinguishes_commit_from_search(activity):
    row = await committed(activity, [product(description="private-audit-marker")])
    async with activity.factory() as db:
        before = await db.scalar(select(func.count()).select_from(PublicUsageBudget))
    result = await human(activity, "GET", "activity")
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert "private-audit-marker" not in result.text
    assert result.json()["allowance"]["state"] in {"idle", "disabled"}
    result = await human(activity, "GET", f"activity/runs/{row['run_id']}")
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "committed"
    assert result.json()["search"]["state"] == "pending"
    async with activity.factory() as db:
        assert await db.scalar(select(func.count()).select_from(PublicUsageBudget)) == before


async def test_manual_request_is_server_bound_and_service_keeps_its_own_identity(activity):
    result = await human(
        activity, "POST", "activity/requests", role="member", body={"operation_id": str(uuid4())}
    )
    assert result.status_code == 201, result.text
    request_id = result.json()["request_id"]
    row = await committed(activity, [product()], manual_request_id=request_id)
    response = await human(activity, "GET", f"activity/runs/{row['run_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["origin"] == "manual"
    events = await human(activity, "GET", "activity/events")
    assert events.status_code == 200, events.text
    committed_events = [e for e in events.json()["items"] if e["action"] == "sync.run.committed"]
    assert len(committed_events) == 1
    assert committed_events[0]["actor"]["kind"] == "service"
    assert committed_events[0]["manual_request_id"] == request_id
    async with activity.factory() as db:
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "sync.request.created",
                AuditLog.user_metadata["manual_request_id"].as_string() == request_id,
            )
        )
        assert audit.user_id == activity.store.users["member"]
        assert "private-audit-marker" not in json.dumps(audit.user_metadata)


async def test_foreign_targets_and_service_tokens_do_not_become_member_inspection(activity):
    for suffix in ("activity", "activity/events", "activity/runs"):
        response = await human(activity, "GET", suffix, workspace=uuid4())
        assert response.status_code == 404, response.text
        response = await human(activity, "GET", suffix, installation=uuid4())
        assert response.status_code == 404, response.text
        response = await authenticated_request(
            activity,
            "GET",
            f"/api/v1/workspaces/{activity.store.wid}/sync-installations/{activity.iid}/{suffix}",
            token=activity.token,
        )
        assert response.status_code == 401


async def test_event_cursor_takes_the_last_audit_id_and_refuses_the_next(activity):
    # ``audit_logs.id`` is a 32-bit integer (#2857).
    last = await human(activity, "GET", f"activity/events?before_id={2**31 - 1}")
    assert last.status_code == 200, last.text
    past = await human(activity, "GET", f"activity/events?before_id={2**31}")
    assert past.status_code == 400
    assert past.json() == {"error": "invalid_request"}
