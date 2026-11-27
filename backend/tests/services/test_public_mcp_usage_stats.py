"""Owner statistics of a public MCP endpoint, without a database (#3035).

The definitions of the response (zero-filled UTC days, totals, outcomes,
searches = ``recall`` calls), the naive UTC bound of the statements, the time
budget and the per-step statement timeout, the per-workspace minute bucket,
and the names the response schema may never carry.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from models.public_mcp_usage import PMU_OUTCOMES
from services import public_mcp_usage_stats as stats
from services.public_mcp_usage_stats import (
    USAGE_STATS_RATE_LIMIT_PER_MINUTE,
    LiveEndpoint,
    assemble_days,
    check_usage_stats_rate_limit,
    read_usage_stats,
    window,
)
from utils.exceptions import PublicMcpUsageStatsRateLimitError

ENDPOINT = LiveEndpoint(
    id=uuid4(), public_id="pmcp_" + "C" * 22, resource_pk=uuid4(), context_id=uuid4()
)
WORKSPACE = uuid4()

# ----------------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------------


def test_days_are_zero_filled_in_order_with_every_outcome() -> None:
    first = date(2026, 11, 21)
    rows = [
        (date(2026, 11, 22), "recall", "success", 3),
        (date(2026, 11, 22), "recall", "rate_limited", 1),
        (date(2026, 11, 22), "reference", "success", 4),
        (date(2026, 11, 27), "list_tags", "error", 2),
        (date(2026, 11, 27), "recall", "degraded", 5),
        (date(2026, 11, 20), "recall", "success", 99),  # before the window
    ]
    days, totals = assemble_days(rows, first, 7)
    assert [d for d, _ in days] == [first + timedelta(days=i) for i in range(7)]
    for _, counts in days:
        assert set(counts.search_outcomes) == set(PMU_OUTCOMES)
    by_day = dict(days)
    assert (by_day[date(2026, 11, 22)].calls, by_day[date(2026, 11, 22)].searches) == (8, 4)
    assert by_day[date(2026, 11, 22)].search_outcomes["rate_limited"] == 1
    assert (by_day[date(2026, 11, 27)].calls, by_day[date(2026, 11, 27)].searches) == (7, 5)
    assert by_day[date(2026, 11, 21)].calls == 0
    assert totals.calls == sum(c.calls for _, c in days) == 15
    assert totals.searches == sum(c.searches for _, c in days) == 9
    assert set(totals.search_outcomes) == set(PMU_OUTCOMES)
    for outcome in PMU_OUTCOMES:
        assert totals.search_outcomes[outcome] == sum(c.search_outcomes[outcome] for _, c in days)
    assert totals.search_outcomes == {
        **dict.fromkeys(PMU_OUTCOMES, 0),
        "success": 3,
        "rate_limited": 1,
        "degraded": 5,
    }


@pytest.mark.parametrize(("days", "retention", "expected"), [(7, 90, 7), (90, 30, 30), (1, 90, 1)])
def test_window_is_at_most_the_retention(days, retention, expected) -> None:
    today = date(2026, 11, 27)
    window_days, first_day, start = window(days, retention, today)
    assert window_days == expected
    assert first_day == today - timedelta(days=expected - 1)
    assert start == datetime(first_day.year, first_day.month, first_day.day, 0, 0, 0)
    assert start.tzinfo is None


# ----------------------------------------------------------------------------
# The read, on a recording fake session
# ----------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return list(self.rows)

    def first(self) -> Any:
        return self.rows[0] if self.rows else None


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Session:
    """Records every executed statement; advances the clock on chosen statements."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.executed: list[tuple[Any, dict[str, Any] | None]] = []
        self.cost: dict[Any, float] = {}
        self.results: dict[Any, list[Any]] = {}
        self.savepoints = 0

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        self.executed.append((stmt, params))
        self.clock.now += self.cost.get(stmt, 0.0)
        return _Result(self.results.get(stmt, []))

    @contextlib.asynccontextmanager
    async def _nested(self):
        self.savepoints += 1
        yield

    def begin_nested(self):
        return self._nested()

    def sql(self) -> list[str]:
        return [str(stmt) for stmt, _ in self.executed]

    def timeouts(self) -> list[str]:
        return [p["ms"] for s, p in self.executed if s is stats._STATEMENT_TIMEOUT and p]


@pytest.fixture
def serving(monkeypatch):
    """The endpoint serves; the resolver is recorded."""
    endpoint = SimpleNamespace(id=ENDPOINT.id, resource_pk=ENDPOINT.resource_pk)
    calls: list[list[str]] = []

    async def load(db, public_id):
        return endpoint

    async def paused(db, ep):
        return False

    async def resolve(db, ep, keys):
        calls.append(list(keys))
        return {k: {"title": f"T-{k[:4]}", "source_url": None} for k in keys}

    monkeypatch.setattr(stats, "load_serving_endpoint", load)
    monkeypatch.setattr(stats, "serving_paused", paused)
    monkeypatch.setattr(stats, "resolve_public_documents", resolve)
    return calls


async def _read(db: _Session, clock: _Clock, days: int = 7):
    return await read_usage_stats(
        db,  # type: ignore[arg-type]
        endpoint=ENDPOINT,
        workspace_id=WORKSPACE,
        days=days,
        retention_days=90,
        recording_enabled=True,
        clock=clock,
    )


async def test_statements_get_a_naive_utc_bound_and_both_ids(serving) -> None:
    clock = _Clock()
    db = _Session(clock)
    db.results[stats.TOP_DOCUMENTS] = [
        SimpleNamespace(scanned=2, doc_key="a" * 32, searches=2, best_rank=1)
    ]
    result = await _read(db, clock, days=7)
    bound = [p for s, p in db.executed if s in (stats.DAY_COUNTS, stats.TOP_DOCUMENTS)]
    assert len(bound) == 2
    today = stats.utcnow().date()
    for params in bound:
        assert params is not None
        start = params["first_day_start"]
        assert type(start) is datetime and start.tzinfo is None
        assert start == datetime.combine(today - timedelta(days=6), datetime.min.time())
        assert params["endpoint_id"] == ENDPOINT.id
        assert params["workspace_id"] == WORKSPACE
    assert result.documents.status == "ok"
    assert result.documents.items[0].title == "T-aaaa"
    assert serving == [["a" * 32]]
    # Each section is read-only inside its own savepoint.
    assert db.savepoints == 2
    assert db.sql().count(str(stats._READ_ONLY)) == 2


async def test_both_sections_take_search_from_one_definition(serving) -> None:
    """The document scan and the day counts agree on which tool is a search.

    The document statement binds the tool from ``_SEARCH_TOOL`` (no literal
    tool name in its text), and the day counts classify searches with the
    same constant.
    """
    assert "'recall'" not in str(stats.TOP_DOCUMENTS)
    assert ":search_tool" in str(stats.TOP_DOCUMENTS)
    clock = _Clock()
    db = _Session(clock)
    await _read(db, clock)
    [params] = [p for s, p in db.executed if s is stats.TOP_DOCUMENTS]
    assert params is not None
    assert params["search_tool"] == stats._SEARCH_TOOL
    _, totals = assemble_days(
        [(date(2026, 11, 27), stats._SEARCH_TOOL, "success", 2)], date(2026, 11, 27), 1
    )
    assert totals.searches == 2


async def test_no_document_statement_once_the_budget_is_spent(serving) -> None:
    clock = _Clock()
    db = _Session(clock)
    db.cost[stats.DAY_COUNTS] = stats.USAGE_STATS_BUDGET_SECONDS + 1
    db.results[stats.DAY_COUNTS] = [(stats.utcnow().date(), "recall", "success", 4)]
    result = await _read(db, clock)
    assert result.totals.calls == 4
    assert result.documents.status == "unavailable"
    assert result.documents.publication_serving is False
    assert result.documents.items == []
    # Nothing of section B ran: the last statement is the day count.
    assert db.executed[-1][0] is stats.DAY_COUNTS
    assert db.savepoints == 1
    assert serving == []


async def test_budget_spent_inside_the_document_section(serving) -> None:
    clock = _Clock()
    db = _Session(clock)
    db.cost[stats.TOP_DOCUMENTS] = stats.USAGE_STATS_BUDGET_SECONDS
    db.results[stats.TOP_DOCUMENTS] = [
        SimpleNamespace(scanned=1, doc_key="b" * 32, searches=1, best_rank=2)
    ]
    result = await _read(db, clock)
    assert result.documents.status == "unavailable"
    assert serving == []  # the serving check and the resolver did not start


async def test_statement_timeout_is_the_smaller_of_five_seconds_and_the_time_left(
    serving,
) -> None:
    clock = _Clock()
    db = _Session(clock)
    db.cost[stats.DAY_COUNTS] = 3.5  # 2.5 s left for section B
    db.results[stats.TOP_DOCUMENTS] = [
        SimpleNamespace(scanned=1, doc_key="c" * 32, searches=1, best_rank=1)
    ]
    db.cost[stats.TOP_DOCUMENTS] = 2.4996  # 0.4 ms left for the next steps
    await _read(db, clock)
    assert db.timeouts() == ["5000", "2500", "1", "1"]


async def test_day_count_failure_is_unavailable(monkeypatch) -> None:
    clock = _Clock()
    db = _Session(clock)

    async def broken(stmt, params=None):
        if stmt is stats.DAY_COUNTS:
            raise RuntimeError("boom")
        return _Result([])

    db.execute = broken  # type: ignore[method-assign]
    from utils.exceptions import PublicMcpUsageStatsUnavailableError

    with pytest.raises(PublicMcpUsageStatsUnavailableError) as exc:
        await _read(db, clock)
    assert exc.value.status_code == 503
    assert exc.value.error_code == "usage_stats_unavailable"
    assert exc.value.response_headers == {"Retry-After": "60"}


async def test_not_serving_gives_no_title(monkeypatch, serving) -> None:
    async def paused(db, ep):
        return True

    monkeypatch.setattr(stats, "serving_paused", paused)
    clock = _Clock()
    db = _Session(clock)
    db.results[stats.TOP_DOCUMENTS] = [
        SimpleNamespace(scanned=3, doc_key="d" * 32, searches=3, best_rank=1)
    ]
    result = await _read(db, clock)
    assert result.documents.status == "ok"
    assert result.documents.publication_serving is False
    assert [(i.doc_key, i.title, i.source_url) for i in result.documents.items] == [
        ("d" * 32, None, None)
    ]
    assert serving == []


async def test_another_endpoint_serving_at_the_address_gives_no_title(monkeypatch, serving):
    async def load(db, public_id):
        return SimpleNamespace(id=uuid4(), resource_pk=ENDPOINT.resource_pk)

    monkeypatch.setattr(stats, "load_serving_endpoint", load)
    clock = _Clock()
    db = _Session(clock)
    db.results[stats.TOP_DOCUMENTS] = [
        SimpleNamespace(scanned=1, doc_key="e" * 32, searches=1, best_rank=1)
    ]
    result = await _read(db, clock)
    assert result.documents.publication_serving is False
    assert result.documents.items[0].title is None
    assert serving == []


async def test_scanned_is_reported_when_no_document_was_returned(serving) -> None:
    clock = _Clock()
    db = _Session(clock)
    db.results[stats.TOP_DOCUMENTS] = [
        SimpleNamespace(scanned=5, doc_key=None, searches=None, best_rank=None)
    ]
    result = await _read(db, clock)
    assert result.documents.searches_scanned == 5
    assert result.documents.items == []
    assert result.documents.scan_limit == stats.USAGE_STATS_DOCUMENT_SCAN_LIMIT
    assert serving == []


# ----------------------------------------------------------------------------
# Rate bucket
# ----------------------------------------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: list[int | None] = []

    async def __call__(self, key: str, ttl: int | None = None) -> int:
        self.ttls.append(ttl)
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


async def test_rate_bucket_per_workspace_and_minute(monkeypatch) -> None:
    counter = _Counter()
    monkeypatch.setattr(stats, "increment_counter", counter)
    fixed = datetime(2026, 11, 27, 10, 15, 30)
    monkeypatch.setattr(stats, "utcnow", lambda: fixed)
    for _ in range(USAGE_STATS_RATE_LIMIT_PER_MINUTE):
        await check_usage_stats_rate_limit(WORKSPACE)
    with pytest.raises(PublicMcpUsageStatsRateLimitError) as exc:
        await check_usage_stats_rate_limit(WORKSPACE)
    assert exc.value.status_code == 429
    assert exc.value.error_code == "RATE-001"
    assert exc.value.response_headers == {"Retry-After": "60"}
    assert list(counter.counts) == [f"pmcp_usage_stats_rl:{WORKSPACE}:202611271015"]
    assert set(counter.ttls) == {60}
    # Another workspace has its own bucket.
    await check_usage_stats_rate_limit(uuid4())


async def test_rate_store_failure_serves_and_warns(monkeypatch) -> None:
    from db.redis import RedisError

    warnings: list[tuple[str, dict[str, Any]]] = []

    async def broken(key: str, ttl: int | None = None) -> int:
        raise RedisError("store down")

    monkeypatch.setattr(stats, "increment_counter", broken)
    monkeypatch.setattr(stats.logger, "warning", lambda event, **kw: warnings.append((event, kw)))
    await check_usage_stats_rate_limit(WORKSPACE)
    assert warnings == [
        ("public_mcp_usage_stats_rate_limit_unavailable", {"error_type": "RedisError"})
    ]


# ----------------------------------------------------------------------------
# Response schema: names that never appear, at any depth
# ----------------------------------------------------------------------------

FORBIDDEN = {
    "query",
    "query_text",
    "query_hash",
    "session_id",
    "session_key",
    "client_family",
    "ip",
    "user_agent",
    "memory_id",
    "doc_id",
    "resource_doc_id",
    "user_id",
}


def _property_names(node: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                names.update(value)
            names |= _property_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _property_names(value)
    return names


def test_response_schema_carries_no_forbidden_name() -> None:
    from api.routes.public_mcp_usage import PublicMcpUsageStatsResponse

    schema = PublicMcpUsageStatsResponse.model_json_schema()
    names = _property_names(schema)
    assert {"doc_key", "title", "source_url", "search_outcomes", "days"} <= names
    assert not names & FORBIDDEN, names & FORBIDDEN
