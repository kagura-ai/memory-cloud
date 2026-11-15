"""An interrupted manual Sleep run stops blocking, on real Postgres (#2856).

Over HTTP against both run routes (``POST /api/v1/admin/sleep/run`` for the
calling admin, ``POST /api/v1/admin/users/{user_id}/sleep/run`` for a target
user), and the background job and batch themselves with the phases replaced:

- a ``running`` report older than ``SLEEP_RUN_STALE_AFTER`` that no
  transaction holds and no batch of this process runs is closed as
  ``failed`` (``SLEEP_RUN_INTERRUPTED``, ``completed_at`` set) and the new run
  is admitted: exactly one ``running`` report afterwards;
- a younger one, an old one whose row another session holds, and an old one
  of a batch this process runs still answer 409 ``SLEEP-002`` with the same
  body, and nothing is written;
- two requests at once with a stale report: one 202, one 409;
- a real job holds its report's row from its first statement to its end, and
  the run guard answers 409 for it however old the report is;
- a report closed before its job starts is not run;
- a batch cancelled in its first job (directly, or by the shutdown step)
  closes both its reports as interrupted, stays cancelled, and the next run
  is admitted.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import memory_cloud_exception_handler
from api.routes import admin_sleep
from auth.dependencies import require_admin
from db.base import get_db
from models.auth import AuditLog, Context, User, Workspace
from models.memory import Memory
from models.operation_history import OperationHistory
from models.sleep import SleepReport
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import MemoryCloudException

pytestmark = pytest.mark.asyncio(loop_scope="session")

ROUTES = ("own", "target")
STALE = admin_sleep.SLEEP_RUN_STALE_AFTER + timedelta(minutes=1)
FRESH = admin_sleep.SLEEP_RUN_STALE_AFTER - timedelta(minutes=1)
# How long a test waits for a background job to reach a point or to end.
WAIT_SECONDS = 10.0


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """An admin A and a user U, one workspace, and one context that is
    eligible for both routes: created by A, holding a live memory of U."""
    tag = uuid4().hex[:8]
    admin, target = f"sleep-int-a-{tag}", f"sleep-int-u-{tag}"
    db_session.add(User(email=f"{admin}@test.example", user_id=admin, name=admin, role="admin"))
    db_session.add(User(email=f"{target}@test.example", user_id=target, name=target, role="user"))
    await db_session.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"sleep-int-{tag}",
        plan_name="pro",
        owner_user_id=target,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.flush()
    ctx = Context(
        id=uuid4(), workspace_id=ws.id, created_by=admin, name=f"ctx-{tag}", sleep_mode="full"
    )
    db_session.add(ctx)
    await db_session.flush()
    db_session.add(
        Memory(
            id=uuid4(),
            context_id=ctx.id,
            workspace_id=ws.id,
            user_id=target,
            type="note",
            scope="working",
            client="test",
            summary="a memory",
            content="a memory",
            embedding_status="success",
        )
    )
    await db_session.commit()
    users = [admin, target]
    try:
        yield SimpleNamespace(
            admin=admin,
            target=target,
            principal={"user_id": admin, "email": f"{admin}@test.example", "role": "admin"},
            ws=ws.id,
            ctx=ctx.id,
            runner={"own": admin, "target": target},
        )
    finally:
        await db_session.rollback()
        await db_session.execute(delete(AuditLog).where(AuditLog.user_id == admin))
        await db_session.execute(
            delete(OperationHistory).where(OperationHistory.workspace_id == ws.id)
        )
        await db_session.execute(delete(SleepReport).where(SleepReport.user_id.in_(users)))
        await db_session.execute(delete(Memory).where(Memory.workspace_id == ws.id))
        await db_session.execute(delete(Context).where(Context.workspace_id == ws.id))
        await db_session.execute(delete(Workspace).where(Workspace.id == ws.id))
        await db_session.execute(delete(User).where(User.user_id.in_(users)))
        await db_session.commit()


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.include_router(admin_sleep.router, prefix="/api/v1")
    app.include_router(admin_sleep.target_router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: dict(principal)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _post(client: httpx.AsyncClient, world, route: str) -> httpx.Response:
    body = {"context_id": str(world.ctx)}
    if route == "own":
        return await client.post("/api/v1/admin/sleep/run", json=body)
    return await client.post(f"/api/v1/admin/users/{world.target}/sleep/run", json=body)


@pytest.fixture
def jobs(monkeypatch) -> list[tuple[list, str]]:
    """The batches a run route started; the phases never run here."""
    started: list[tuple[list, str]] = []

    async def fake_batch(batch: list, user_id: str) -> None:
        started.append((batch, user_id))

    monkeypatch.setattr(admin_sleep, "_run_sleep_batch", fake_batch)
    return started


@pytest_asyncio.fixture(loop_scope="session")
async def job_sessions(monkeypatch, maker) -> AsyncIterator[None]:
    """The background job's ``get_db`` opens sessions on the test database.

    Each generator is kept until the test ends: a job that leaves ``get_db``
    by an exception does not close its session then, and only the job's own
    rollback ends the transaction before the batch closes its reports. (A
    generator nothing references is finalized soon after, which would hide a
    job that does not release its session.)
    """
    opened = []

    async def _sessions() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def _get_db() -> AsyncIterator[AsyncSession]:
        generator = _sessions()
        opened.append(generator)
        return generator

    monkeypatch.setattr("db.base.get_db", _get_db)
    try:
        yield
    finally:
        for generator in opened:
            await generator.aclose()


@pytest.fixture
def phases(monkeypatch) -> SimpleNamespace:
    """The orchestrator, replaced: a run waits for ``release`` and completes."""
    state = SimpleNamespace(started=asyncio.Event(), release=asyncio.Event(), runs=[])

    class FakeOrchestrator:
        def __init__(self, db: AsyncSession) -> None:
            self.db = db

        async def run(self, user_id, workspace_id, context_id, *, report) -> None:
            state.runs.append(report.id)
            state.started.set()
            await state.release.wait()
            report.status = "completed"
            report.completed_at = utcnow()

        async def delete_committed_points(self) -> None:
            return None

    monkeypatch.setattr(admin_sleep, "SleepOrchestrator", FakeOrchestrator)
    return state


async def _add_report(maker, world, user_id: str, *, age: timedelta, status="running") -> UUID:
    async with maker() as s:
        report = SleepReport(
            id=uuid4(),
            user_id=user_id,
            workspace_id=world.ws,
            context_id=world.ctx,
            status=status,
            started_at=utcnow() - age,
        )
        s.add(report)
        await s.commit()
        return report.id


async def _reports(maker, user_id: str) -> dict[UUID, SleepReport]:
    async with maker() as s:
        rows = (
            (await s.execute(select(SleepReport).where(SleepReport.user_id == user_id)))
            .scalars()
            .all()
        )
    return {row.id: row for row in rows}


def _conflict(report: SleepReport) -> dict:
    return {
        "error": "SLEEP-002",
        "message": "A sleep run is already in progress for this user.",
        "details": {
            "running_report_id": str(report.id),
            "started_at": to_utc_iso(report.started_at),
        },
    }


@pytest.mark.parametrize("route", ROUTES)
async def test_a_stale_unheld_report_is_closed_as_interrupted_and_the_run_admitted(
    maker, world, jobs, route
):
    user_id = world.runner[route]
    stale = await _add_report(maker, world, user_id, age=STALE)

    async with _client(maker, world.principal) as client:
        response = await _post(client, world, route)

    assert response.status_code == 202, response.text
    [new_id] = [UUID(value) for value in response.json()["report_ids"]]
    rows = await _reports(maker, user_id)
    closed = rows[stale]
    assert closed.status == "failed"
    assert closed.error_message == admin_sleep.SLEEP_RUN_INTERRUPTED
    assert closed.error_message.startswith("interrupted")
    assert closed.completed_at is not None
    assert [r.id for r in rows.values() if r.status == "running"] == [new_id]
    assert [batch for batch, _ in jobs] == [[(new_id, str(world.ws), str(world.ctx))]]


@pytest.mark.parametrize("route", ROUTES)
async def test_a_younger_report_still_answers_409_and_nothing_is_written(maker, world, jobs, route):
    user_id = world.runner[route]
    young = await _add_report(maker, world, user_id, age=FRESH)
    before = await _reports(maker, user_id)

    async with _client(maker, world.principal) as client:
        response = await _post(client, world, route)

    assert response.status_code == 409, response.text
    assert response.json() == _conflict(before[young])
    after = await _reports(maker, user_id)
    assert list(after) == [young]
    assert after[young].status == "running" and after[young].error_message is None
    assert jobs == []


@pytest.mark.parametrize("route", ROUTES)
async def test_an_old_report_another_session_holds_answers_409(maker, world, jobs, route):
    user_id = world.runner[route]
    old = await _add_report(maker, world, user_id, age=STALE)

    async with maker() as holder:
        held = (
            await holder.execute(
                select(SleepReport).where(SleepReport.id == old).with_for_update(key_share=True)
            )
        ).scalar_one()
        expected = _conflict(held)
        async with _client(maker, world.principal) as client:
            response = await _post(client, world, route)
        await holder.rollback()

    assert response.status_code == 409, response.text
    assert response.json() == expected
    rows = await _reports(maker, user_id)
    assert list(rows) == [old] and rows[old].status == "running"
    assert jobs == []


@pytest.mark.parametrize("route", ROUTES)
async def test_an_old_report_of_a_batch_this_process_runs_answers_409(maker, world, jobs, route):
    user_id = world.runner[route]
    old = await _add_report(maker, world, user_id, age=STALE)
    report = (await _reports(maker, user_id))[old]

    admin_sleep._LIVE_REPORT_IDS.add(old)
    try:
        async with _client(maker, world.principal) as client:
            response = await _post(client, world, route)
    finally:
        admin_sleep._LIVE_REPORT_IDS.discard(old)

    assert response.status_code == 409, response.text
    assert response.json() == _conflict(report)
    rows = await _reports(maker, user_id)
    assert list(rows) == [old] and rows[old].status == "running"
    assert jobs == []


@pytest.mark.parametrize("route", ROUTES)
async def test_a_stale_report_a_concurrent_guard_is_closing_answers_409(maker, world, jobs, route):
    """The guard's close holds the row until its transaction ends, so a second
    request at the same time cannot close it again or start a second run."""
    user_id = world.runner[route]
    stale = await _add_report(maker, world, user_id, age=STALE)
    report = (await _reports(maker, user_id))[stale]

    async with maker() as first:
        await admin_sleep._raise_if_sleep_running(first, user_id)  # closes, uncommitted
        async with _client(maker, world.principal) as client:
            response = await _post(client, world, route)
        await first.rollback()

    assert response.status_code == 409, response.text
    assert response.json() == _conflict(report)
    assert jobs == []


@pytest.mark.parametrize("route", ROUTES)
async def test_two_requests_at_once_with_a_stale_report_admit_one(maker, world, jobs, route):
    user_id = world.runner[route]
    stale = await _add_report(maker, world, user_id, age=STALE)

    async with _client(maker, world.principal) as client:
        responses = await asyncio.gather(_post(client, world, route), _post(client, world, route))

    assert sorted(r.status_code for r in responses) == [202, 409], [r.text for r in responses]
    rows = await _reports(maker, user_id)
    assert rows[stale].status == "failed"
    assert len([r for r in rows.values() if r.status == "running"]) == 1
    assert len(jobs) == 1


@pytest.mark.parametrize("route", ROUTES)
async def test_a_running_job_holds_its_report_row_and_still_answers_409(
    maker, world, job_sessions, phases, route
):
    user_id = world.runner[route]
    # Old enough to be closed if nothing held it, and not in this process's
    # live set: only the job's lock keeps it live.
    report_id = await _add_report(maker, world, user_id, age=STALE)
    job = asyncio.create_task(
        admin_sleep._run_sleep_job(report_id, user_id, str(world.ws), str(world.ctx))
    )
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            await phases.started.wait()

        async with maker() as probe:
            with pytest.raises(DBAPIError) as refused:
                await probe.execute(
                    select(SleepReport)
                    .where(SleepReport.id == report_id)
                    .with_for_update(key_share=True, nowait=True)
                )
            await probe.rollback()
        assert "55P03" in str(refused.value.orig.sqlstate)  # type: ignore[union-attr]

        async with _client(maker, world.principal) as client:
            response = await _post(client, world, route)
        assert response.status_code == 409, response.text
        assert response.json()["details"]["running_report_id"] == str(report_id)
    finally:
        phases.release.set()
        async with asyncio.timeout(WAIT_SECONDS):
            await job

    rows = await _reports(maker, user_id)
    assert list(rows) == [report_id]
    assert rows[report_id].status == "completed"


async def test_a_report_closed_before_its_job_starts_is_not_run(maker, world, job_sessions, phases):
    report_id = await _add_report(maker, world, world.target, age=FRESH, status="failed")
    before = (await _reports(maker, world.target))[report_id]

    async with asyncio.timeout(WAIT_SECONDS):
        await admin_sleep._run_sleep_job(report_id, world.target, str(world.ws), str(world.ctx))

    assert phases.runs == []
    after = (await _reports(maker, world.target))[report_id]
    assert (after.status, after.started_at) == ("failed", before.started_at)


async def _wait_for_a_lock_wait(maker) -> None:
    """Return once some session of this database waits on a lock."""
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            async with maker() as s:
                waiting = (
                    await s.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        )
                    )
                ).scalar_one()
            if waiting:
                return
            await asyncio.sleep(0.05)


async def test_a_report_closed_while_its_job_waits_for_the_claim_is_not_run(
    maker, world, job_sessions, phases
):
    """The claim locks the row before it reads the status (#2856).

    A guard that holds the report while it closes it makes the job wait; once
    the close commits, the job reads ``failed`` and does not run. A claim
    that read the status without the lock would see ``running`` and run.
    """
    report_id = await _add_report(maker, world, world.target, age=STALE)
    async with maker() as holder:
        report = (
            await holder.execute(
                select(SleepReport)
                .where(SleepReport.id == report_id)
                .with_for_update(key_share=True)
            )
        ).scalar_one()
        job = asyncio.create_task(
            admin_sleep._run_sleep_job(report_id, world.target, str(world.ws), str(world.ctx))
        )
        try:
            await _wait_for_a_lock_wait(maker)
            report.status = "failed"
            report.error_message = admin_sleep.SLEEP_RUN_INTERRUPTED
            await holder.commit()
        finally:
            phases.release.set()
            async with asyncio.timeout(WAIT_SECONDS):
                await job

    assert phases.runs == []
    assert (await _reports(maker, world.target))[report_id].status == "failed"


@pytest.mark.parametrize("route", ROUTES)
async def test_the_409_names_the_oldest_live_report(maker, world, jobs, route):
    user_id = world.runner[route]
    newer = await _add_report(maker, world, user_id, age=timedelta(minutes=1))
    oldest = await _add_report(maker, world, user_id, age=timedelta(minutes=5))
    rows = await _reports(maker, user_id)

    async with _client(maker, world.principal) as client:
        response = await _post(client, world, route)

    assert response.status_code == 409, response.text
    assert response.json() == _conflict(rows[oldest])
    assert newer in rows
    assert jobs == []


async def test_the_409_names_the_lowest_id_among_reports_started_at_once(maker, world, jobs):
    started_at = utcnow() - timedelta(minutes=2)
    low, high = sorted((uuid4(), uuid4()), key=str)
    async with maker() as s:
        # The higher id first, so the table's own order is not the answer.
        for report_id in (high, low):
            s.add(
                SleepReport(
                    id=report_id,
                    user_id=world.admin,
                    workspace_id=world.ws,
                    context_id=world.ctx,
                    status="running",
                    started_at=started_at,
                )
            )
            await s.flush()
        await s.commit()
    rows = await _reports(maker, world.admin)

    async with _client(maker, world.principal) as client:
        response = await _post(client, world, "own")

    assert response.status_code == 409, response.text
    assert response.json() == _conflict(rows[low])


@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
@pytest.mark.parametrize("route", ROUTES)
async def test_a_batch_cancelled_in_its_first_job_closes_both_reports(
    maker, world, job_sessions, phases, route, stop
):
    user_id = world.runner[route]
    first = await _add_report(maker, world, user_id, age=timedelta(0))
    second = await _add_report(maker, world, user_id, age=timedelta(0))
    batch = [(first, str(world.ws), str(world.ctx)), (second, str(world.ws), str(world.ctx))]

    started = set(asyncio.all_tasks())
    admin_sleep._start_sleep_batch(batch, user_id)
    [task] = set(asyncio.all_tasks()) - started
    try:
        assert task in admin_sleep._IN_FLIGHT_BATCHES
        async with asyncio.timeout(WAIT_SECONDS):
            await phases.started.wait()
        assert {first, second} <= admin_sleep._LIVE_REPORT_IDS

        if stop == "cancel":
            task.cancel()
        else:
            await admin_sleep.stop_manual_sleep_runs(timeout=WAIT_SECONDS)
        async with asyncio.timeout(WAIT_SECONDS):
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        # A job that was not cut off must not outlive the test.
        phases.release.set()
        task.cancel()

    assert task.cancelled()
    assert phases.runs == [first]
    rows = await _reports(maker, user_id)
    for report_id in (first, second):
        assert rows[report_id].status == "failed"
        assert rows[report_id].error_message == admin_sleep.SLEEP_RUN_INTERRUPTED
        assert rows[report_id].completed_at is not None
    assert task not in admin_sleep._IN_FLIGHT_BATCHES
    assert not {first, second} & admin_sleep._LIVE_REPORT_IDS

    # Nothing is left running: the next run is admitted.
    async def no_batch(_batch: list, _user_id: str) -> None:
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(admin_sleep, "_run_sleep_batch", no_batch)
        async with _client(maker, world.principal) as client:
            response = await _post(client, world, route)
    assert response.status_code == 202, response.text
