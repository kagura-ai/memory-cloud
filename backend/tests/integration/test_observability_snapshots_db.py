"""Sanitised indexer snapshots on real rows (#2679, AC 5).

Never-run is distinct from idle and from error; the age of the last run is
reported as such (not lag), the offset as an offset (not a backlog); no
metrics passthrough, no error text, no failed event ids.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete

from config.settings import get_settings
from models.public_mcp import PublicMcpEndpoint
from models.resource import IndexerState
from tests.observability_helpers import World, app_client, bearer, issue_credential, world_rows
from utils.datetime import utcnow
from utils.exceptions import CapacityLockedError, QuotaExceededError

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "/api/v1/observability/snapshots"
ERROR_TEXT = "zq-indexer-error-text-5521"


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        yield w


@pytest.fixture(autouse=True)
def api_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "observability_api_enabled", True)


async def _set_state(w: World, key: str, **fields) -> None:
    async with w.factory() as s:
        await s.execute(delete(IndexerState).where(IndexerState.context_id == w.ctx[key]))
        if fields:
            s.add(
                IndexerState(
                    resource_pk=w.resource_pk[key],
                    resource_id=w.slugs[key],
                    context_id=w.ctx[key],
                    **fields,
                )
            )
        await s.commit()


async def _snapshot(w: World, secret: str, key: str) -> tuple[dict, str]:
    async with app_client(w) as c:
        r = await c.get(f"{BASE}?resource_id={w.slugs[key]}", headers=bearer(secret))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["field_notes"] == {
        "seconds_since_last_run": "age of the last indexer run; not ingest lag",
        "last_offset": "the last processed ingestion event id; not a backlog count",
    }
    (item,) = body["snapshots"]
    return item, r.text


async def test_never_run_idle_and_error(world: World) -> None:
    _, secret = await issue_credential(world)

    await _set_state(world, "a1")
    item, _ = await _snapshot(world, secret, "a1")
    assert item["indexer"] == {
        "state": "never_run",
        "last_run_at": None,
        "seconds_since_last_run": None,
        "next_run_at": None,
        "last_offset": None,
        "last_run": None,
    }
    assert item["context_id"] == str(world.ctx["a1"])

    # A row that never ran is still never_run, not idle.
    await _set_state(world, "a1", last_offset=0, last_run_at=None, job_status="idle")
    item, _ = await _snapshot(world, secret, "a1")
    assert item["indexer"]["state"] == "never_run"

    ran = utcnow() - timedelta(seconds=300)
    await _set_state(
        world,
        "a1",
        last_offset=42,
        last_run_at=ran,
        job_status="idle",
        metrics={"applied_upserts": 3, "applied_deletes": 1, "errors": 0, "custom": ERROR_TEXT},
    )
    item, _ = await _snapshot(world, secret, "a1")
    indexer = item["indexer"]
    assert indexer["state"] == "idle"
    assert indexer["last_offset"] == 42
    assert 290 <= indexer["seconds_since_last_run"] <= 400
    assert indexer["last_run"] == {
        "applied_upserts": 3,
        "applied_deletes": 1,
        "errors": 0,
        "failed_events": 0,
        "skipped_reason": None,
    }

    await _set_state(
        world,
        "a1",
        last_offset=42,
        last_run_at=ran,
        job_status="failed",
        metrics={
            "errors": 2,
            "error": ERROR_TEXT,
            "failed_events": [
                {"event_id": 987654321, "error": ERROR_TEXT},
                {"event_id": 987654322, "error": ERROR_TEXT},
            ],
        },
    )
    item, text = await _snapshot(world, secret, "a1")
    assert item["indexer"]["state"] == "error"
    assert item["indexer"]["last_run"]["failed_events"] == 2
    assert ERROR_TEXT not in text
    assert "987654321" not in text
    assert '"metrics"' not in text


async def test_all_in_scope_and_publication(world: World, monkeypatch) -> None:
    _, secret = await issue_credential(world)
    async with world.factory() as s:
        s.add(
            PublicMcpEndpoint(
                workspace_id=world.ws,
                context_id=world.ctx["a2"],
                resource_pk=world.resource_pk["a2"],
                resource_id=world.slugs["a2"],
                public_name="Public catalog",
                created_by=world.owner,
            )
        )
        await s.commit()
    async with app_client(world) as c:
        monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", False)
        body = (await c.get(BASE, headers=bearer(secret))).json()
        by_slug = {x["resource_id"]: x for x in body["snapshots"]}
        assert set(by_slug) == {world.slugs["a1"], world.slugs["a2"]}
        assert by_slug[world.slugs["a1"]]["publication"] is None
        publication = by_slug[world.slugs["a2"]]["publication"]
        assert publication["public_name"] == "Public catalog"
        assert publication["public_id"].startswith("pmcp_")
        assert publication["serving"] is False  # the operator switch is off
        monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
        body = (await c.get(BASE, headers=bearer(secret))).json()
        by_slug = {x["resource_id"]: x for x in body["snapshots"]}
        assert by_slug[world.slugs["a2"]]["publication"]["serving"] is True
        resources = (await c.get("/api/v1/observability/resources", headers=bearer(secret))).json()
        by_slug = {x["resource_id"]: x for x in resources["resources"]}
        assert by_slug[world.slugs["a2"]]["publication"]["serving"] is True
        assert by_slug[world.slugs["a1"]]["context_id"] == str(world.ctx["a1"])

        # A plan pause or a capacity lock stops the anonymous endpoint (503),
        # so the publication is not serving either.
        async def locked(*args, **kwargs):
            raise CapacityLockedError.for_outsider()

        with patch("mcp_server.public.publication.ensure_not_capacity_locked", new=locked):
            body = (await c.get(BASE, headers=bearer(secret))).json()
            by_slug = {x["resource_id"]: x for x in body["snapshots"]}
            assert by_slug[world.slugs["a2"]]["publication"]["serving"] is False
            resources = (
                await c.get("/api/v1/observability/resources", headers=bearer(secret))
            ).json()
            by_slug = {x["resource_id"]: x for x in resources["resources"]}
            assert by_slug[world.slugs["a2"]]["publication"]["serving"] is False

        def paused(workspace):
            raise QuotaExceededError("paused", quota_type="api_public_daily")

        with patch("mcp_server.public.publication.ensure_public_serving_allowed", new=paused):
            body = (await c.get(BASE, headers=bearer(secret))).json()
            by_slug = {x["resource_id"]: x for x in body["snapshots"]}
            assert by_slug[world.slugs["a2"]]["publication"]["serving"] is False
