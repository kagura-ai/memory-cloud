"""The per-day counts of public usage events, on Postgres (#3053).

The rollup ``public_mcp_usage_day_counts`` must hold, for every UTC day in
``public_mcp_usage_closed_days``, exactly the ``tool_call`` events of that
day grouped by endpoint, workspace, tool, outcome, route and caller:

1. across a UTC day boundary: one batch with calls on both sides of
   midnight, and a late call of the earlier day written after that day was
   closed;
2. for a batch the writer retries after a commit that landed: counted once;
3. for events the writer did not count (written before the rollup existed):
   closing the day counts them; a writer increment that waits on a closing
   day lands on top of the recount;
4. after the retention purge: the days the purge reached lose their rows and
   their closed mark, every other closed day still agrees with its events.

The statistics read gives the same body whether a day is closed or not.

Every row a test adds is deleted afterwards: its events and rollup rows by
endpoint, its closed marks by day (days 50 to 70 back, which no other test
of the suite closes).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.public_mcp_usage import PMU_TXID_DEFAULT
from services import public_mcp_usage_rollup as rollup
from services.public_mcp_usage_events import UsageEventInput, build_usage_event_row
from services.public_mcp_usage_stats import LiveEndpoint, read_usage_stats
from services.public_mcp_usage_writer import PublicMcpUsageWriter
from tasks.public_mcp_usage_tasks import purge_public_mcp_usage_events
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = "k" * 40
RETENTION_DAYS = 90
# Days this file closes: far enough back that no other test closes them.
FIRST_BACK, LAST_BACK = 50, 70

_EVENT_GROUPS = text(
    "SELECT endpoint_id, (occurred_at)::date AS day, workspace_id, tool, outcome, "
    "COALESCE(route, 'mcp') AS route, COALESCE(caller, 'anonymous') AS caller, count(*) "
    "FROM public_mcp_usage_events WHERE kind = 'tool_call' AND endpoint_id = ANY(:endpoints) "
    "GROUP BY 1, 2, 3, 4, 5, 6, 7"
)
_ROLLUP_GROUPS = text(
    "SELECT endpoint_id, day, workspace_id, tool, outcome, route, caller, calls "
    "FROM public_mcp_usage_day_counts WHERE endpoint_id = ANY(:endpoints)"
)
_CLOSED = text("SELECT day FROM public_mcp_usage_closed_days WHERE day = ANY(:days)")


@pytest.fixture(autouse=True)
def _retention(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_retention_days", RETENTION_DAYS)


class Env:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.today = utcnow().date()
        self.endpoints: list[UUID] = []
        self.workspace = uuid4()

    def back(self, days: int) -> date:
        assert FIRST_BACK <= days <= LAST_BACK
        return self.today - timedelta(days=days)

    def endpoint(self) -> UUID:
        endpoint = uuid4()
        self.endpoints.append(endpoint)
        return endpoint

    async def groups(self, statement) -> dict[tuple, int]:
        async with self.factory() as s:
            rows = (await s.execute(statement, {"endpoints": self.endpoints})).all()
        return {tuple(r[:7]): int(r[7]) for r in rows}

    async def events(self) -> dict[tuple, int]:
        return await self.groups(_EVENT_GROUPS)

    async def counts(self) -> dict[tuple, int]:
        return await self.groups(_ROLLUP_GROUPS)

    async def closed(self, days: list[date]) -> set[date]:
        async with self.factory() as s:
            return set((await s.execute(_CLOSED, {"days": days})).scalars())

    async def close(self, day: date, now: datetime | None = None) -> bool:
        async with self.factory() as s:
            return await rollup.close_usage_day(s, day, now=now or utcnow())


@pytest_asyncio.fixture(loop_scope="session")
async def env(async_engine: AsyncEngine) -> AsyncIterator[Env]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    state = Env(factory)
    days = [state.today - timedelta(days=n) for n in range(FIRST_BACK, LAST_BACK + 1)]
    try:
        yield state
    finally:
        async with factory() as s:
            ids = {"endpoints": state.endpoints, "days": days}
            await s.execute(
                text("DELETE FROM public_mcp_usage_events WHERE endpoint_id = ANY(:endpoints)"),
                ids,
            )
            await s.execute(
                text("DELETE FROM public_mcp_usage_day_counts WHERE endpoint_id = ANY(:endpoints)"),
                ids,
            )
            await s.execute(
                text("DELETE FROM public_mcp_usage_closed_days WHERE day = ANY(:days)"), ids
            )
            await s.commit()


def _call(env: Env, endpoint: UUID, at: datetime, **overrides: Any) -> UsageEventInput:
    values: dict[str, Any] = {
        "endpoint_id": endpoint,
        "workspace_id": env.workspace,
        "context_id": uuid4(),
        "resource_pk": uuid4(),
        "resource_id": "catalog",
        "tool": "recall",
        "outcome": "success",
        "era": "session",
        "started_at": at,
        "duration_ms": 2,
    }
    values.update(overrides)
    return UsageEventInput(**values)


def _mix(env: Env, endpoint: UUID, at: datetime) -> list[UsageEventInput]:
    """Calls of every kind the counts tell apart, at one time."""
    return [
        _call(env, endpoint, at),
        _call(env, endpoint, at),
        _call(env, endpoint, at, outcome="rate_limited", error_code="rate_limit_exceeded"),
        _call(env, endpoint, at, tool="explore"),
        _call(
            env, endpoint, at, era="stateless", route="rest", caller="bound_key", outcome="degraded"
        ),
    ]


def _writer(session_factory, **overrides: Any) -> PublicMcpUsageWriter:
    values: dict[str, Any] = {
        "session_factory": session_factory,
        "queue_size": 100,
        "flush_interval": 0.05,
        "write_timeout": 5.0,
        "key": KEY,
        "session_details_enabled": False,
        "retry_backoff": 0.0,
    }
    values.update(overrides)
    return PublicMcpUsageWriter(**values)


async def _write(session_factory, calls: list[UsageEventInput], **overrides: Any) -> None:
    writer = _writer(session_factory, **overrides)
    for call in calls:
        writer.offer(call)
    writer.start()
    await writer.stop(timeout=10)
    assert writer.health().dropped_since_start == 0


_EVENT_COLUMNS = (
    "event_id",
    "occurred_at",
    "kind",
    "endpoint_id",
    "workspace_id",
    "context_id",
    "resource_pk",
    "resource_id",
    "tool",
    "outcome",
    "era",
    "duration_ms",
)
_INSERT_EVENT = text(
    f"INSERT INTO public_mcp_usage_events ({', '.join(_EVENT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _EVENT_COLUMNS)})"
)
# A row as a restored database holds it: its ``txid`` stored, not this transaction's.
_INSERT_WITH_TXID = text(
    f"INSERT INTO public_mcp_usage_events (txid, {', '.join(_EVENT_COLUMNS)}) "
    f"VALUES (:txid, {', '.join(':' + c for c in _EVENT_COLUMNS)})"
)
# Transaction ids the test stores ahead of the counter: more than the
# transactions the database starts before the writer's batch.
_TXIDS_AHEAD = 20


def _event_values(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in _EVENT_COLUMNS}


async def _insert_without_the_writer(env: Env, calls: list[UsageEventInput]) -> None:
    """The previous release's INSERT: events only, no day counts."""
    rows = [build_usage_event_row(c, key=KEY, session_details_enabled=False).row for c in calls]
    async with env.factory() as s:
        for row in rows:
            await s.execute(_INSERT_EVENT, _event_values(row))
        await s.commit()


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min)


async def test_counts_equal_events_across_a_utc_day_boundary(env: Env) -> None:
    endpoint = env.endpoint()
    before, after = env.back(61), env.back(60)
    last_moment = _midnight(after) - timedelta(microseconds=1)
    # One batch on both sides of midnight (two day locks, one transaction).
    await _write(
        env.factory, _mix(env, endpoint, last_moment) + _mix(env, endpoint, _midnight(after))
    )
    counts = await env.counts()
    assert counts == await env.events()
    assert {key[1] for key in counts} == {before, after}
    assert counts[(endpoint, before, env.workspace, "recall", "success", "mcp", "anonymous")] == 2
    assert counts[(endpoint, after, env.workspace, "recall", "degraded", "rest", "bound_key")] == 1

    # Closing recounts from the events: the same numbers.
    assert await env.close(before) is True
    assert await env.close(before) is False, "closed once"
    assert await env.closed([before, after]) == {before}
    assert await env.counts() == await env.events()

    # A late call of the closed day, written now: added to that day.
    await _write(env.factory, [_call(env, endpoint, last_moment - timedelta(seconds=30))])
    counts = await env.counts()
    assert counts == await env.events()
    assert counts[(endpoint, before, env.workspace, "recall", "success", "mcp", "anonymous")] == 3


class _CommitThenTimeOut:
    """Session factory whose first session commits and then reports a timeout."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.calls = 0

    def __call__(self) -> AsyncSession:
        self.calls += 1
        session = self._factory()
        if self.calls == 1:
            real_commit = session.commit

            async def commit() -> None:
                await real_commit()
                raise TimeoutError

            session.commit = commit  # type: ignore[method-assign]
        return session


async def test_a_retried_batch_is_counted_once(env: Env) -> None:
    endpoint = env.endpoint()
    at = _midnight(env.back(59)) + timedelta(hours=10)
    factory = _CommitThenTimeOut(env.factory)
    await _write(factory, _mix(env, endpoint, at))
    assert factory.calls >= 2, "the batch was sent again"
    counts = await env.counts()
    assert counts == await env.events()
    assert sum(counts.values()) == 5


async def test_a_batch_counts_only_its_own_rows_when_a_stored_txid_repeats(env: Env) -> None:
    """A database restored from a logical dump keeps the stored ``txid`` values
    while its transaction counter starts again, so a later batch can run under
    a transaction id that rows already carry. The batch counts its own rows."""
    stored, endpoint = env.endpoint(), env.endpoint()
    at = _midnight(env.back(62)) + timedelta(hours=9)
    rows = [
        build_usage_event_row(_call(env, stored, at), key=KEY, session_details_enabled=False).row
        for _ in range(_TXIDS_AHEAD)
    ]
    async with env.factory() as s:
        current = int((await s.execute(text("SELECT " + PMU_TXID_DEFAULT))).scalar_one())
        ahead = list(range(current + 1, current + 1 + _TXIDS_AHEAD))
        for row, txid in zip(rows, ahead, strict=True):
            await s.execute(_INSERT_WITH_TXID, {**_event_values(row), "txid": txid})
        await s.commit()

    await _write(env.factory, _mix(env, endpoint, at + timedelta(hours=1)))

    async with env.factory() as s:
        written = set(
            (
                await s.execute(
                    text("SELECT txid FROM public_mcp_usage_events WHERE endpoint_id = :e"),
                    {"e": endpoint},
                )
            ).scalars()
        )
    assert len(written) == 1 and written <= set(ahead), "the batch ran under a stored txid"
    counts = await env.counts()
    assert {key[0] for key in counts} == {endpoint}
    assert counts == {k: v for k, v in (await env.events()).items() if k[0] == endpoint}


async def test_closing_counts_events_the_writer_did_not(env: Env) -> None:
    endpoint = env.endpoint()
    day = env.back(58)
    noon = _midnight(day) + timedelta(hours=12)
    await _insert_without_the_writer(env, _mix(env, endpoint, noon))
    await _write(env.factory, [_call(env, endpoint, noon)])
    assert sum((await env.counts()).values()) == 1, "only the writer's call so far"
    assert await env.close(day) is True
    counts = await env.counts()
    assert counts == await env.events()
    assert sum(counts.values()) == 6


async def test_a_writer_waiting_on_a_closing_day_lands_on_the_recount(env: Env) -> None:
    """The close holds the day's lock from before its recount to its commit; a
    writer that inserted a call of that day meanwhile adds it afterwards."""
    endpoint = env.endpoint()
    day = env.back(57)
    noon = _midnight(day) + timedelta(hours=12)
    await _insert_without_the_writer(env, _mix(env, endpoint, noon))

    async with env.factory() as closing:
        await closing.execute(rollup._LOCK_EXCLUSIVE, {"key": rollup.day_lock_key(day)})
        writing = asyncio.create_task(_write(env.factory, [_call(env, endpoint, noon)]))
        await asyncio.sleep(0.5)
        assert not writing.done(), "the writer waits for the day's lock"
        assert await rollup.count_day(closing, day, now=utcnow()) is True
        await closing.commit()
    await asyncio.wait_for(writing, timeout=10)
    counts = await env.counts()
    assert counts == await env.events()
    assert sum(counts.values()) == 6
    assert await env.closed([day]) == {day}


async def test_the_read_is_the_same_before_and_after_closing(env: Env) -> None:
    endpoint = env.endpoint()
    first = env.back(56)
    calls = []
    for n in range(4):  # four past days and today
        at = _midnight(first + timedelta(days=n)) + timedelta(hours=n + 1)
        calls += _mix(env, endpoint, at)[: n + 2]
    await _insert_without_the_writer(env, calls[:3])
    await _write(env.factory, calls[3:] + _mix(env, endpoint, utcnow()))
    live = LiveEndpoint(
        id=endpoint, public_id="pmcp_" + "R" * 22, resource_pk=uuid4(), context_id=uuid4()
    )

    async def read() -> dict:
        async with env.factory() as s:
            stats = await read_usage_stats(
                s,
                endpoint=live,
                workspace_id=env.workspace,
                days=60,
                retention_days=RETENTION_DAYS,
                recording_enabled=True,
                rest_recording_enabled=True,
                visitor_counting_enabled=True,
            )
        return {"totals": asdict(stats.totals), "days": [(d, asdict(c)) for d, c in stats.days]}

    open_read = await read()
    assert open_read["totals"]["calls"] == len(calls) + 5
    for n in (0, 2, 3):  # one past day stays open
        assert await env.close(first + timedelta(days=n)) is True
    assert await read() == open_read


async def test_the_purge_takes_the_days_it_reaches_and_leaves_the_rest_equal(env: Env) -> None:
    endpoint = env.endpoint()
    days = [env.back(n) for n in (65, 64, 63)]
    for day in days:
        await _write(env.factory, _mix(env, endpoint, _midnight(day) + timedelta(hours=6)))
        await _write(env.factory, _mix(env, endpoint, _midnight(day) + timedelta(hours=18)))
        assert await env.close(day) is True
    # The cutoff falls at noon of the middle day: that day loses its morning.
    now = _midnight(days[1]) + timedelta(days=RETENTION_DAYS, hours=12)
    async with env.factory() as s:
        await purge_public_mcp_usage_events(s, now)
    assert await env.closed(days) == {days[2]}
    counts = await env.counts()
    assert {key[1] for key in counts} == {days[2]}
    events = await env.events()
    assert {key[1] for key in events} == {days[1], days[2]}
    assert {k: v for k, v in events.items() if k[1] == days[2]} == counts
    # A day reached by the purge is not closed again.
    assert await env.close(days[1], now=now) is False
    assert await env.closed(days) == {days[2]}


async def test_the_job_closes_finished_retained_days_newest_first(env: Env, monkeypatch) -> None:
    from config.settings import get_settings

    endpoint = env.endpoint()
    # Retention that reaches back to the days of this file only.
    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_retention_days", 3)
    now = _midnight(env.back(LAST_BACK - 3)) + rollup.DAY_CLOSE_GRACE
    await _write(env.factory, _mix(env, endpoint, now - timedelta(hours=30)))
    async with env.factory() as s:
        closed = await rollup.close_usage_days(s, now)
    # The window of three days ends with the day before ``now``'s day (the
    # grace has passed); ``now``'s own day is not finished.
    expected = [env.back(LAST_BACK - 2 + n) for n in (0, 1)]
    assert closed == 2
    assert await env.closed([env.back(n) for n in range(FIRST_BACK, LAST_BACK + 1)]) == set(
        expected
    )
    assert await env.counts() == await env.events()
    async with env.factory() as s:
        assert await rollup.close_usage_days(s, now) == 0
