"""Admin sleep report reads on real Postgres (#2842).

Over HTTP against the real routes, as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds):

- walking ``GET /api/v1/admin/sleep-reports/{id}/actions`` with ``offset``
  and ``limit`` returns every action of the run exactly once, in ``id``
  order, and never an action of another run; ``total`` equals the detail
  route's ``action_count``;
- ``?include_actions=false`` answers ``actions: []`` with the same count and
  none of the text an action's ``details`` hold;
- an unknown report answers 404 on both routes, and a read changes no row.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import sleep_reports as sleep_report_routes
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.sleep import SleepAction, SleepReport

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}
ACTIONS_OF_THE_RUN = 7


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(sleep_report_routes.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: principal
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """A run with seven actions, interleaved with another run's actions."""
    tag = uuid4().hex[:8]
    sentinel = f"merged-summary-{secrets.token_hex(8)}"
    report = SleepReport(id=uuid4(), user_id=f"sleeper-{tag}", status="completed")
    other = SleepReport(id=uuid4(), user_id=f"other-{tag}", status="completed")
    empty = SleepReport(id=uuid4(), user_id=f"empty-{tag}", status="completed")
    db_session.add_all([report, other, empty])
    await db_session.flush()
    for index in range(ACTIONS_OF_THE_RUN):
        db_session.add(
            SleepAction(
                report_id=report.id,
                phase="dedup_merge",
                action_type="merge",
                memory_id=uuid4(),
                target_id=uuid4(),
                details={"loser_summary": f"{sentinel}-{index}", "reason": sentinel},
            )
        )
        # Flush one by one: the ids of the two runs interleave.
        await db_session.flush()
        db_session.add(
            SleepAction(
                report_id=other.id,
                phase="edge_discovery",
                action_type="create_edge",
                details={"edge_type": f"other-{sentinel}"},
            )
        )
        await db_session.flush()
    await db_session.commit()
    expected = list(
        (
            await db_session.execute(
                select(SleepAction.id)
                .where(SleepAction.report_id == report.id)
                .order_by(SleepAction.id)
            )
        ).scalars()
    )
    assert len(expected) == ACTIONS_OF_THE_RUN
    report_id, other_id, empty_id = report.id, other.id, empty.id
    try:
        yield SimpleNamespace(
            report_id=report_id,
            other_id=other_id,
            empty_id=empty_id,
            action_ids=expected,
            sentinel=sentinel,
        )
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(SleepReport).where(SleepReport.id.in_([report_id, other_id, empty_id]))
        )
        await db_session.commit()


@pytest.mark.parametrize("mode", sorted(PRINCIPALS))
@pytest.mark.parametrize("limit", [1, 3, 7, 500])
async def test_walking_the_pages_returns_every_action_once_in_id_order(maker, world, mode, limit):
    seen: list[int] = []
    async with _client(maker, PRINCIPALS[mode]) as client:
        offset = 0
        while True:
            response = await client.get(
                f"/api/v1/admin/sleep-reports/{world.report_id}/actions",
                params={"limit": limit, "offset": offset},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == ACTIONS_OF_THE_RUN
            assert (body["limit"], body["offset"]) == (limit, offset)
            assert len(body["actions"]) <= limit
            if not body["actions"]:
                break
            seen.extend(action["id"] for action in body["actions"])
            # A walk that never reaches the end fails here instead of looping.
            assert len(seen) <= ACTIONS_OF_THE_RUN
            offset += limit

    assert seen == world.action_ids
    assert len(set(seen)) == ACTIONS_OF_THE_RUN


@pytest.mark.parametrize("mode", sorted(PRINCIPALS))
async def test_total_equals_the_detail_routes_action_count(maker, world, mode):
    async with _client(maker, PRINCIPALS[mode]) as client:
        page = await client.get(f"/api/v1/admin/sleep-reports/{world.report_id}/actions")
        full = await client.get(f"/api/v1/admin/sleep-reports/{world.report_id}")
        light = await client.get(
            f"/api/v1/admin/sleep-reports/{world.report_id}", params={"include_actions": "false"}
        )

    assert page.status_code == full.status_code == light.status_code == 200
    assert page.json()["total"] == full.json()["action_count"] == ACTIONS_OF_THE_RUN
    assert light.json()["action_count"] == ACTIONS_OF_THE_RUN
    # The page's actions are the detail route's, field for field.
    assert page.json()["actions"] == full.json()["actions"]
    assert [a["id"] for a in full.json()["actions"]] == world.action_ids


@pytest.mark.parametrize("mode", sorted(PRINCIPALS))
async def test_include_actions_false_holds_no_action_details(maker, world, mode):
    async with _client(maker, PRINCIPALS[mode]) as client:
        light = await client.get(
            f"/api/v1/admin/sleep-reports/{world.report_id}", params={"include_actions": "false"}
        )
        full = await client.get(f"/api/v1/admin/sleep-reports/{world.report_id}")

    assert light.status_code == 200, light.text
    body = light.json()
    assert body["actions"] == []
    assert world.sentinel not in light.text
    assert "loser_summary" not in light.text
    # The report's own row is the one the full answer carries.
    assert body["report"] == full.json()["report"]
    # The seeded text is there to find when the actions are asked for.
    assert world.sentinel in full.text


async def test_a_page_never_holds_another_runs_action(maker, world):
    async with _client(maker, PRINCIPALS["service"]) as client:
        response = await client.get(
            f"/api/v1/admin/sleep-reports/{world.report_id}/actions", params={"limit": 500}
        )

    assert response.status_code == 200, response.text
    assert f"other-{world.sentinel}" not in response.text
    assert {a["phase"] for a in response.json()["actions"]} == {"dedup_merge"}


async def test_a_run_without_actions_answers_an_empty_page(maker, world):
    async with _client(maker, PRINCIPALS["service"]) as client:
        page = await client.get(f"/api/v1/admin/sleep-reports/{world.empty_id}/actions")
        light = await client.get(
            f"/api/v1/admin/sleep-reports/{world.empty_id}", params={"include_actions": "false"}
        )

    assert page.status_code == 200, page.text
    assert page.json() == {"actions": [], "total": 0, "limit": 50, "offset": 0}
    assert light.json()["action_count"] == 0


async def test_an_offset_past_the_end_answers_an_empty_page_with_the_total(maker, world):
    async with _client(maker, PRINCIPALS["session"]) as client:
        response = await client.get(
            f"/api/v1/admin/sleep-reports/{world.report_id}/actions", params={"offset": 100}
        )

    assert response.status_code == 200, response.text
    assert response.json()["actions"] == []
    assert response.json()["total"] == ACTIONS_OF_THE_RUN


@pytest.mark.parametrize("mode", sorted(PRINCIPALS))
async def test_an_unknown_report_is_404_on_both_routes(maker, world, mode):
    missing = uuid4()
    async with _client(maker, PRINCIPALS[mode]) as client:
        page = await client.get(f"/api/v1/admin/sleep-reports/{missing}/actions")
        light = await client.get(
            f"/api/v1/admin/sleep-reports/{missing}", params={"include_actions": "false"}
        )
        full = await client.get(f"/api/v1/admin/sleep-reports/{missing}")

    assert (page.status_code, light.status_code, full.status_code) == (404, 404, 404)


async def test_the_reads_change_no_row(maker, world, db_session: AsyncSession):
    async def snapshot() -> tuple[int, int]:
        actions = await db_session.scalar(select(func.count()).select_from(SleepAction))
        reports = await db_session.scalar(select(func.count()).select_from(SleepReport))
        await db_session.rollback()
        return actions or 0, reports or 0

    before = await snapshot()
    async with _client(maker, PRINCIPALS["service"]) as client:
        await client.get(f"/api/v1/admin/sleep-reports/{world.report_id}/actions")
        await client.get(
            f"/api/v1/admin/sleep-reports/{world.report_id}", params={"include_actions": "false"}
        )

    assert await snapshot() == before


async def test_an_offset_the_database_would_refuse_is_422_and_the_largest_is_answered(maker, world):
    url = f"/api/v1/admin/sleep-reports/{world.report_id}/actions"
    async with _client(maker, PRINCIPALS["service"]) as client:
        refused = await client.get(url, params={"offset": 2**63})
        largest = await client.get(url, params={"offset": 2**63 - 1})

    assert refused.status_code == 422, refused.text
    assert largest.status_code == 200, largest.text
    assert largest.json()["actions"] == []
    assert largest.json()["total"] == ACTIONS_OF_THE_RUN
