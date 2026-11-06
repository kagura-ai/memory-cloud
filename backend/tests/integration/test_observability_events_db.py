"""The usage event stream through the observability API (#2679, AC 4).

A thin HTTP layer over #2678's commit-safe cursor: a late commit is not
skipped, a page replays identically, a reader restarted with a stored cursor
continues, a cursor below the retention mark answers 410 with a usable
``oldest_cursor``, and an unsupported schema version is a 400.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import get_settings
from models.public_mcp_usage import PublicMcpUsageEvent, PublicMcpUsageRetention
from services.public_mcp_usage_events import (
    UsageCursor,
    UsageEventInput,
    build_gap_row,
    build_usage_event_row,
    encode_cursor,
)
from tests.observability_helpers import World, app_client, bearer, issue_credential, world_rows

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "/api/v1/observability/events"


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        async with w.factory() as s:
            await s.execute(delete(PublicMcpUsageEvent))
            await s.execute(
                pg_insert(PublicMcpUsageRetention).values(id=1).on_conflict_do_nothing()
            )
            await s.execute(
                update(PublicMcpUsageRetention)
                .where(PublicMcpUsageRetention.id == 1)
                .values(purged_through_txid=0, purged_through_id=0)
            )
            await s.commit()
        try:
            yield w
        finally:
            async with w.factory() as s:
                await s.execute(delete(PublicMcpUsageEvent))
                await s.commit()


@pytest.fixture(autouse=True)
def api_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "observability_api_enabled", True)


def _row(w: World, key: str = "a1", workspace: UUID | None = None) -> dict:
    return build_usage_event_row(
        UsageEventInput(
            endpoint_id=uuid4(),
            workspace_id=workspace or w.ws,
            context_id=w.ctx[key],
            resource_pk=w.resource_pk[key],
            resource_id=w.slugs[key],
            tool="recall",
            outcome="success",
            era="session",
            started_at=datetime.now(UTC),
            duration_ms=5,
        ),
        key="k" * 40,
        session_details_enabled=False,
    ).row


async def _horizon(w: World) -> int:
    async with w.factory() as s:
        return int(
            (
                await s.execute(
                    text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint")
                )
            ).scalar_one()
        )


async def _wait_sealed(w: World, txid: int) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while await _horizon(w) <= txid:
        if loop.time() > deadline:
            pytest.fail("a transaction on the server held the horizon")
        await asyncio.sleep(0.05)


async def _insert(w: World, *rows: dict) -> list[tuple[int, int]]:
    async with w.factory() as s:
        result = await s.execute(
            insert(PublicMcpUsageEvent)
            .values(list(rows))
            .returning(PublicMcpUsageEvent.txid, PublicMcpUsageEvent.id)
        )
        keys = [(r.txid, r.id) for r in result.all()]
        await s.commit()
    await _wait_sealed(w, max(t for t, _ in keys))
    return keys


async def test_late_commit_replay_restart_expiry(world: World) -> None:
    _, secret = await issue_credential(world)
    _, other_secret = await issue_credential(world, workspace="b")
    async with world.factory() as session_a:
        a = await session_a.execute(
            insert(PublicMcpUsageEvent)
            .values(_row(world))
            .returning(PublicMcpUsageEvent.event_id, PublicMcpUsageEvent.txid)
        )
        a_event_id, a_txid = a.one()
        async with world.factory() as s:
            b = await s.execute(
                insert(PublicMcpUsageEvent)
                .values(_row(world))
                .returning(PublicMcpUsageEvent.event_id)
            )
            b_event_id = b.scalar_one()
            await s.commit()
        # B is committed but A (smaller id) is not: nothing is published yet.
        async with app_client(world) as c:
            page = (await c.get(BASE, headers=bearer(secret))).json()
        assert page["events"] == []
        assert page["coverage"]["unsealed_events"] >= 1
        start_cursor = page["next_cursor"]
        await session_a.commit()
    await _wait_sealed(world, a_txid)

    async with app_client(world) as c:
        page = (await c.get(f"{BASE}?cursor={start_cursor}", headers=bearer(secret))).json()
        assert [e["event_id"] for e in page["events"]] == [str(a_event_id), str(b_event_id)]
        assert page["schema_version"] == 1 and page["client_family_verified"] is False
        assert page["as_of"].endswith("Z") and page["deployment_id"]
        # Replay: the same cursor answers the same rows.
        again = (await c.get(f"{BASE}?cursor={start_cursor}", headers=bearer(secret))).json()
        assert again["events"] == page["events"]
        stored = page["next_cursor"]
        # Workspace B's credential sees none of A's rows.
        other = (await c.get(BASE, headers=bearer(other_secret))).json()
        assert other["events"] == []

    # A new reader (new client, new sessions) continues from the stored cursor.
    later = await _insert(
        world,
        _row(world),
        build_gap_row(
            dropped_count=1,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            reason="queue_full",
        ),
    )
    async with app_client(world) as c:
        page = (await c.get(f"{BASE}?cursor={stored}&limit=1", headers=bearer(secret))).json()
        assert len(page["events"]) == 1 and page["has_more"] is True
        page2 = (await c.get(f"{BASE}?cursor={page['next_cursor']}", headers=bearer(secret))).json()
        assert [e["kind"] for e in page2["events"]] == ["gap"]
        assert page2["coverage"]["gaps_in_page"] == 1
        # Gap rows are in every scope.
        other = (await c.get(BASE, headers=bearer(other_secret))).json()
        assert [e["kind"] for e in other["events"]] == ["gap"]

        # Retention purged through the first later row: the stored cursor is
        # older than the mark, so it may have missed rows.
        mark_txid, mark_id = later[0]
        async with world.factory() as s:
            await s.execute(
                update(PublicMcpUsageRetention)
                .where(PublicMcpUsageRetention.id == 1)
                .values(purged_through_txid=mark_txid, purged_through_id=mark_id)
            )
            await s.commit()
        r = await c.get(f"{BASE}?cursor={stored}", headers=bearer(secret))
        assert r.status_code == 410
        body = r.json()
        assert body["error"] == "cursor_expired"
        oldest = body["details"]["oldest_cursor"]
        assert oldest == encode_cursor(UsageCursor(mark_txid, mark_id))
        r = await c.get(f"{BASE}?cursor={oldest}", headers=bearer(secret))
        assert r.status_code == 200
        assert [e["kind"] for e in r.json()["events"]] == ["gap"]

        r = await c.get(f"{BASE}?cursor=garbage", headers=bearer(secret))
        assert r.status_code == 400 and r.json()["error"] == "cursor_invalid"
        r = await c.get(f"{BASE}?schema_version=2", headers=bearer(secret))
        assert r.status_code == 400 and r.json()["details"]["supported"] == [1]
        assert (await c.get(f"{BASE}?limit=501", headers=bearer(secret))).status_code == 422


async def test_resource_filter_and_scope(world: World) -> None:
    await _insert(world, _row(world, "a1"), _row(world, "a2"))
    _, workspace_secret = await issue_credential(world)
    _, resource_secret = await issue_credential(world, resource="a2")
    async with app_client(world) as c:
        everything = (await c.get(BASE, headers=bearer(workspace_secret))).json()
        slugs = {e.get("resource_id") for e in everything["events"] if e["kind"] == "tool_call"}
        assert {world.slugs["a1"], world.slugs["a2"]} <= slugs
        only = (
            await c.get(f"{BASE}?resource_id={world.slugs['a1']}", headers=bearer(workspace_secret))
        ).json()
        assert {e["resource_id"] for e in only["events"] if e["kind"] == "tool_call"} == {
            world.slugs["a1"]
        }
        bound = (await c.get(BASE, headers=bearer(resource_secret))).json()
        assert {e["resource_id"] for e in bound["events"] if e["kind"] == "tool_call"} == {
            world.slugs["a2"]
        }
        r = await c.get(f"{BASE}?resource_id={world.slugs['a1']}", headers=bearer(resource_secret))
        assert r.status_code == 404 and r.json()["error"] == "resource_not_found"
