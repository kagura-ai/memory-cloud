"""The admin sleep report list filters by workspace and names it, on real Postgres (#2844).

Over HTTP against the real routes, as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds):

- ``?workspace_id=W`` answers only W's reports, ``total`` counts only those,
  and it is ANDed with ``status``, ``source``, ``context_id`` and ``user_id``;
- an id with no report answers an empty list with ``total`` 0, a report
  without a workspace id never matches, a value that is not a UUID is a 422;
- every list row and the detail's ``report`` carry ``workspace_name``: the
  name of a live workspace, null for a soft-deleted or missing one and for a
  report without a workspace id; one query resolves a response's names;
- without the parameter the list answers the same reports in the same order.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import sleep_reports as sleep_report_routes
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.auth import Context, User, Workspace
from models.sleep import SleepReport
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}
MODES = sorted(PRINCIPALS)


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
    """Six reports of two users: three in a live workspace, one in a
    soft-deleted one, one whose workspace row is gone, one without an id."""
    tag = uuid4().hex[:8]
    deleted_name = f"deleted-ws-{secrets.token_hex(8)}"
    owner, u1, u2 = f"ws-owner-{tag}", f"ws-sleeper-one-{tag}", f"ws-sleeper-two-{tag}"
    owner_email = f"owner-{secrets.token_hex(6)}@workspace-filter.example"
    db_session.add(User(email=owner_email, user_id=owner, name=owner, role="user"))
    for user in (u1, u2):
        db_session.add(User(email=f"{user}@test.example", user_id=user, name=user, role="user"))
    await db_session.flush()

    def workspace(name: str, **cols) -> Workspace:
        return Workspace(
            id=uuid4(),
            name=name,
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=5000,
            weekly_api_limit=25000,
            **cols,
        )

    live = workspace(f"Alpha {tag}")
    other_live = workspace(f"Gamma {tag}")
    deleted = workspace(deleted_name, deleted_at=utcnow())
    db_session.add_all([live, other_live, deleted])
    await db_session.flush()
    c1 = Context(id=uuid4(), workspace_id=live.id, name=f"c1-{tag}", created_by=owner)
    c1b = Context(id=uuid4(), workspace_id=live.id, name=f"c1b-{tag}", created_by=owner)
    c2 = Context(id=uuid4(), workspace_id=deleted.id, name=f"c2-{tag}", created_by=owner)
    db_session.add_all([c1, c1b, c2])
    await db_session.flush()
    missing_workspace = uuid4()
    now = utcnow()

    def report(minutes: int, **cols) -> SleepReport:
        return SleepReport(
            id=uuid4(),
            started_at=now - timedelta(minutes=minutes),
            **{"status": "completed"} | cols,
        )

    reports = {
        "r1": report(1, user_id=u1, workspace_id=live.id, context_id=c1.id),
        "r2": report(2, user_id=u2, workspace_id=live.id, context_id=c1b.id, status="failed"),
        "r3": report(3, user_id=u1, workspace_id=live.id, context_id=c1.id, source="analysis"),
        "r4": report(4, user_id=u1, workspace_id=deleted.id, context_id=c2.id),
        "r5": report(5, user_id=u1, workspace_id=missing_workspace),
        "r6": report(6, user_id=u1),
    }
    db_session.add_all(reports.values())
    await db_session.commit()
    ids = {key: str(value.id) for key, value in reports.items()}
    report_ids = [value.id for value in reports.values()]
    workspace_ids = [live.id, other_live.id, deleted.id]
    names = SimpleNamespace(
        ids=ids,
        live=live.id,
        live_name=live.name,
        other_live=other_live.id,
        deleted=deleted.id,
        deleted_name=deleted_name,
        missing=missing_workspace,
        c1=c1.id,
        c2=c2.id,
        u1=u1,
        u2=u2,
        owner=owner,
        owner_email=owner_email,
    )
    try:
        yield names
    finally:
        await db_session.rollback()
        await db_session.execute(delete(SleepReport).where(SleepReport.id.in_(report_ids)))
        await db_session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
        await db_session.execute(delete(User).where(User.user_id.in_([owner, u1, u2])))
        await db_session.commit()


async def _list(maker, mode: str, **params) -> httpx.Response:
    async with _client(maker, PRINCIPALS[mode]) as client:
        return await client.get("/api/v1/admin/sleep-reports", params=params)


def _ids(response: httpx.Response) -> list[str]:
    return [row["id"] for row in response.json()["reports"]]


@pytest.mark.parametrize("mode", MODES)
async def test_workspace_id_answers_only_that_workspaces_reports(maker, world, mode):
    response = await _list(maker, mode, workspace_id=str(world.live))

    assert response.status_code == 200, response.text
    body = response.json()
    assert _ids(response) == [world.ids["r1"], world.ids["r2"], world.ids["r3"]]
    assert body["total"] == 3
    assert {row["workspace_id"] for row in body["reports"]} == {str(world.live)}


async def test_total_counts_the_filtered_rows_while_paging(maker, world):
    first = await _list(maker, "service", workspace_id=str(world.live), limit=2)
    second = await _list(maker, "service", workspace_id=str(world.live), limit=2, offset=2)

    assert (first.json()["total"], second.json()["total"]) == (3, 3)
    assert _ids(first) == [world.ids["r1"], world.ids["r2"]]
    assert _ids(second) == [world.ids["r3"]]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("other", "expected"),
    [
        ({"status": "failed"}, ["r2"]),
        ({"status": "completed"}, ["r1", "r3"]),
        ({"source": "analysis"}, ["r3"]),
        ({"source": "sleep"}, ["r1", "r2"]),
        ({"context_id": "c1"}, ["r1", "r3"]),
        ({"context_id": "c2"}, []),
        ({"user_id": "u2"}, ["r2"]),
        ({"user_id": "u1", "status": "completed", "source": "sleep", "context_id": "c1"}, ["r1"]),
        ({"user_id": "u2", "status": "completed"}, []),
    ],
)
async def test_workspace_id_is_anded_with_the_other_filters(maker, world, mode, other, expected):
    params = {
        key: str(getattr(world, value)) if key in {"context_id", "user_id"} else value
        for key, value in other.items()
    }

    response = await _list(maker, mode, workspace_id=str(world.live), **params)

    assert response.status_code == 200, response.text
    assert _ids(response) == [world.ids[key] for key in expected]
    assert response.json()["total"] == len(expected)


@pytest.mark.parametrize("mode", MODES)
async def test_an_id_with_no_report_answers_an_empty_list(maker, world, mode):
    for workspace_id in (world.other_live, uuid4()):
        response = await _list(maker, mode, workspace_id=str(workspace_id), limit=7)

        assert response.status_code == 200, response.text
        assert response.json() == {"reports": [], "total": 0, "limit": 7, "offset": 0}


async def test_a_report_without_a_workspace_id_never_matches(maker, world):
    for workspace_id in (world.live, world.deleted, world.missing, world.other_live):
        response = await _list(maker, "service", workspace_id=str(workspace_id), user_id=world.u1)

        assert response.status_code == 200, response.text
        assert world.ids["r6"] not in _ids(response)
    # The stored id is what matches, also for a workspace that is gone.
    gone = await _list(maker, "service", workspace_id=str(world.missing))
    assert _ids(gone) == [world.ids["r5"]]
    assert gone.json()["total"] == 1


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("value", ["not-a-uuid", "123", ""])
async def test_a_workspace_id_that_is_not_a_uuid_is_422(maker, world, mode, value):
    response = await _list(maker, mode, workspace_id=value)

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("mode", MODES)
async def test_each_list_row_names_its_workspace(maker, world, mode):
    response = await _list(maker, mode, user_id=world.u1)

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["reports"]}
    assert rows[world.ids["r1"]]["workspace_name"] == world.live_name
    assert rows[world.ids["r3"]]["workspace_name"] == world.live_name
    # Soft-deleted: the id stays, the name is not answered.
    assert rows[world.ids["r4"]]["workspace_id"] == str(world.deleted)
    assert rows[world.ids["r4"]]["workspace_name"] is None
    # No workspace row at all.
    assert rows[world.ids["r5"]]["workspace_id"] == str(world.missing)
    assert rows[world.ids["r5"]]["workspace_name"] is None
    # No workspace id.
    assert rows[world.ids["r6"]]["workspace_id"] is None
    assert rows[world.ids["r6"]]["workspace_name"] is None
    assert world.deleted_name not in response.text


@pytest.mark.parametrize("mode", MODES)
async def test_the_detail_names_its_workspace(maker, world, mode):
    expected = {"r1": world.live_name, "r4": None, "r5": None, "r6": None}
    async with _client(maker, PRINCIPALS[mode]) as client:
        for key, name in expected.items():
            for query in ({}, {"include_actions": "false"}):
                response = await client.get(
                    f"/api/v1/admin/sleep-reports/{world.ids[key]}", params=query
                )

                assert response.status_code == 200, response.text
                assert response.json()["report"]["workspace_name"] == name
                assert world.deleted_name not in response.text


async def test_nothing_else_about_the_workspace_is_answered(maker, world):
    response = await _list(maker, "service", workspace_id=str(world.live))

    assert response.status_code == 200, response.text
    for row in response.json()["reports"]:
        assert {key for key in row if "workspace" in key} == {"workspace_id", "workspace_name"}
        assert not {"owner_user_id", "plan_name", "members", "plan"} & set(row)
    assert world.owner not in response.text
    assert world.owner_email not in response.text


async def test_the_names_of_one_response_are_resolved_in_one_query(maker, world, async_engine):
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        # Five reports over three workspace ids (two live rows missing).
        response = await _list(maker, "service", user_id=world.u1)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)

    assert response.status_code == 200, response.text
    assert len(response.json()["reports"]) == 5
    on_workspaces = [sql for sql in statements if "FROM workspaces" in sql]
    assert len(on_workspaces) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_without_the_parameter_the_list_is_the_one_before(maker, world, mode):
    response = await _list(maker, mode, user_id=world.u1)

    assert response.status_code == 200, response.text
    # Every workspace's reports and the ones without one, newest first.
    assert _ids(response) == [world.ids[key] for key in ("r1", "r3", "r4", "r5", "r6")]
    assert response.json()["total"] == 5
    for row in response.json()["reports"]:
        assert UUID(row["id"])


async def test_a_value_the_database_would_refuse_is_422_and_the_bounds_are_answered(maker, world):
    for params in (
        {"user_id": f"{world.u1}\x00"},
        {"user_id": "\x00"},
        {"offset": 2**63},
    ):
        refused = await _list(maker, "service", **params)
        assert refused.status_code == 422, params

    largest = await _list(maker, "service", user_id=world.u1, offset=2**63 - 1)
    assert largest.status_code == 200, largest.text
    assert largest.json()["reports"] == []
    assert largest.json()["total"] == 5
    # An id is matched exactly: a LIKE wildcard in it matches nothing.
    for value in ("%", "_", world.u1[:-1] + "_", world.u1[:-1] + "%"):
        answered = await _list(maker, "service", user_id=value)
        assert answered.status_code == 200, value
        assert answered.json()["total"] == 0, value
