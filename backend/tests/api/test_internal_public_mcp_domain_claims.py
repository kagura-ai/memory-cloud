"""The edge agent's claims list of public MCP custom domains (#2993).

``GET /internal/public-mcp-domains/claims`` lists the claims an edge must
serve or keep (``active`` and ``suspended``) for an edge agent that keeps one
record per host name. It sits behind the same gate as the certificate report,
in the same order, before any session is opened:

1. reporting not configured (the reporter credential empty, or either switch
   off): 404;
2. the credential missing, wrong, or sent twice: 401;
3. the one claims slot cannot be taken without waiting: 503;
4. one read in its own short session, bounded in time and in rows: more than
   ``CLAIMS_LIMIT`` rows, a timeout or any error is 503 with an empty body;
5. else 200 with the versioned list and ``Cache-Control: no-store``.

No log line carries a host name or the credential. The rows on PostgreSQL are
in ``tests/integration/test_public_mcp_domain_claims_db.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import namedtuple
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
import structlog
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from api.routes import internal_public_mcp_domains as routes
from config.settings import get_settings

PATH = "/internal/public-mcp-domains/claims"
INTERNAL_HOST = "api-blue:8080"
APP_HOST = "app.example.com"
TOKEN = "reporter-" + "q" * 40
CLAIMED = "claimed.example.org"

ENVELOPE_KEYS = {"schema_version", "generated_at", "claims"}
CLAIM_KEYS = {"hostname", "state", "domain_id", "endpoint_id", "check_seq", "created_at"}

Row = namedtuple("Row", ["hostname", "state", "id", "endpoint_id", "check_seq", "created_at"])


@pytest.fixture
def configured(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setenv("API_URL", "https://api.example.com")
    monkeypatch.setattr(s, "mcp_public_domains_enabled", True)
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(s, "mcp_public_domains_tls_reporter_token", TOKEN)
    monkeypatch.setattr(s, "frontend_url", f"https://{APP_HOST}")
    monkeypatch.setattr(s, "oidc_issuer", "")
    monkeypatch.setattr(s, "kmc_mcp_url", "")
    monkeypatch.setattr(s, "payment_public_base_url", "")
    monkeypatch.setattr(s, "cors_origins", f"https://{APP_HOST}")
    monkeypatch.setattr(s, "mcp_allowed_origins", "")
    monkeypatch.setattr(s, "mcp_public_domains_routing_target", "routing.example.net")
    monkeypatch.setattr(s, "mcp_public_domains_app_hosts", ".svc.example.net")
    monkeypatch.setattr(s, "mcp_public_domains_reserved_suffixes", "reserved.example.org")


def _row(hostname: str = CLAIMED, state: str = "active", seq: int = 3) -> Row:
    return Row(hostname, state, uuid4(), uuid4(), seq, datetime(2026, 11, 1, 12, 30, 5, 123456))


class _Session:
    """Answers the claims statement with ``rows``; ``execute`` may be replaced."""

    def __init__(self, rows: list[Row], statements: list[Any]) -> None:
        self._rows = rows
        self._statements = statements

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self._statements.append(statement)
        rows = self._rows

        class _Result:
            def all(self) -> list[Row]:
                return list(rows)

        return _Result()


class _Sessions:
    """Stands in for ``get_db``: records each session it opens and closes."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0
        self.rows: list[Row] = [_row()]
        self.statements: list[Any] = []
        self.execute: Any = None

    async def __call__(self) -> AsyncIterator[_Session]:
        self.opened += 1
        session = _Session(self.rows, self.statements)
        if self.execute is not None:
            session.execute = self.execute  # type: ignore[method-assign]
        try:
            yield session
        finally:
            self.closed += 1


@pytest.fixture
def sessions(monkeypatch) -> _Sessions:
    spy = _Sessions()
    monkeypatch.setattr(routes, "get_db", spy)
    return spy


class _Recorder:
    """Stands in for the module's logger: keeps every line, debug included.

    The configured logger drops debug lines before any capture, so the
    route's own lines are recorded here.
    """

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        def log(event: str, **fields: Any) -> None:
            self.lines.append({"event": event, "log_level": level, **fields})

        return log


_RECORDER: _Recorder | None = None


@pytest.fixture(autouse=True)
def _recorded(monkeypatch) -> Iterator[None]:
    global _RECORDER
    _RECORDER = _Recorder()
    monkeypatch.setattr(routes, "logger", _RECORDER)
    yield
    _RECORDER = None


@contextlib.contextmanager
def _all_logs(caplog) -> Iterator[list[Any]]:
    """The route's own lines (as dicts), every structlog event and server-side stdlib record."""
    caplog.set_level(logging.DEBUG)
    assert _RECORDER is not None
    start = len(_RECORDER.lines)
    with structlog.testing.capture_logs() as events:
        captured: list[Any] = []
        yield captured
    captured.extend(_RECORDER.lines[start:])
    captured.extend(events)
    captured.extend(
        f"{record.name} {record.getMessage()} {record.__dict__!r}"
        for record in caplog.records
        if not record.name.startswith(("httpx", "httpcore"))
    )


def _events(captured: list[Any], name: str) -> list[dict]:
    return [e for e in captured if isinstance(e, dict) and e.get("event") == name]


def _client(host: str = INTERNAL_HOST) -> httpx.AsyncClient:
    from api.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


async def _list(
    token: str | None = TOKEN, *, host: str = INTERNAL_HOST, params: dict | None = None
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with _client(host) as c:
        return await c.get(PATH, headers=headers, params=params)


def _assert_empty(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.content == b""


# ----------------------------------------------------------------------------
# Steps 1 and 2: the reporter gate, no session
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "switch",
    [
        ("mcp_public_domains_tls_reporter_token", ""),
        ("mcp_public_domains_enabled", False),
        ("mcp_public_endpoints_enabled", False),
    ],
)
async def test_not_configured_is_404_without_a_session(
    configured, monkeypatch, sessions, caplog, switch
) -> None:
    monkeypatch.setattr(get_settings(), *switch)
    with _all_logs(caplog) as captured:
        _assert_empty(await _list(), 404)
        _assert_empty(await _list(None), 404)
    assert sessions.opened == 0
    refused = _events(captured, "public_mcp_domain_claims_refused")
    assert [(e["reason"], e["log_level"]) for e in refused] == [("disabled", "debug")] * 2


@pytest.mark.parametrize("token", [None, "", "wrong-" + "w" * 40, TOKEN + "x", TOKEN.upper()])
async def test_a_missing_or_wrong_credential_is_401_without_a_session(
    configured, sessions, caplog, token
) -> None:
    with _all_logs(caplog) as captured:
        _assert_empty(await _list(token), 401)
    assert sessions.opened == 0
    refused = _events(captured, "public_mcp_domain_claims_refused")
    assert [(e["reason"], e["log_level"]) for e in refused] == [("credential", "warning")]
    assert not [line for line in captured if TOKEN in repr(line)]


async def test_a_credential_without_the_bearer_scheme_is_401(configured, sessions) -> None:
    async with _client() as c:
        r = await c.get(PATH, headers={"Authorization": TOKEN})
    _assert_empty(r, 401)
    assert sessions.opened == 0


async def test_two_authorization_headers_are_401_without_a_session(configured, sessions) -> None:
    async with _client() as c:
        r = await c.get(
            PATH,
            headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
        )
    _assert_empty(r, 401)
    assert sessions.opened == 0


# ----------------------------------------------------------------------------
# Step 5: the list
# ----------------------------------------------------------------------------


async def test_the_list_has_the_versioned_shape(configured, sessions) -> None:
    active, suspended = _row("a.example.org", "active", 7), _row("b.example.org", "suspended", 0)
    sessions.rows = [active, suspended]
    before = datetime.now(UTC)
    r = await _list()
    after = datetime.now(UTC)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["schema_version"] == 1
    assert body["generated_at"].endswith("Z")
    generated = datetime.fromisoformat(body["generated_at"].replace("Z", "+00:00"))
    assert before - timedelta(seconds=1) <= generated <= after + timedelta(seconds=1)
    assert [set(claim) for claim in body["claims"]] == [CLAIM_KEYS, CLAIM_KEYS]
    assert body["claims"][0] == {
        "hostname": "a.example.org",
        "state": "active",
        "domain_id": str(active.id),
        "endpoint_id": str(active.endpoint_id),
        "check_seq": 7,
        "created_at": "2026-11-01T12:30:05.123456Z",
    }
    assert body["claims"][1]["state"] == "suspended"
    assert body["claims"][1]["check_seq"] == 0
    assert sessions.opened == sessions.closed == 1


async def test_an_empty_list_is_200(configured, sessions) -> None:
    sessions.rows = []
    r = await _list()
    assert r.status_code == 200
    assert r.json()["claims"] == []


async def test_query_parameters_are_ignored(configured, sessions) -> None:
    plain = (await _list()).json()["claims"]
    filtered = (await _list(params={"state": "pending", "limit": "0", "hostname": "x"})).json()
    assert filtered["claims"] == plain
    assert len(sessions.statements) == 2
    compiled = {_sql(s) for s in sessions.statements}
    assert len(compiled) == 1


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


async def test_the_statement_reads_the_held_rows_without_an_order_or_a_lock(
    configured, sessions
) -> None:
    await _list()
    (statement,) = sessions.statements
    sql = " ".join(_sql(statement).split())
    assert sql.startswith(
        "SELECT public_mcp_domains.hostname, public_mcp_domains.state, public_mcp_domains.id, "
        "public_mcp_domains.endpoint_id, public_mcp_domains.check_seq, "
        "public_mcp_domains.created_at FROM public_mcp_domains"
    ), sql
    assert "WHERE public_mcp_domains.state IN ('active', 'suspended')" in sql
    assert "ORDER BY" not in sql  # sorted after the read, in byte order
    assert sql.endswith(f"LIMIT {routes.CLAIMS_LIMIT + 1}")
    assert "FOR UPDATE" not in sql and "FOR SHARE" not in sql


async def test_the_list_is_in_byte_order_of_host_name_then_domain_id(configured, sessions):
    """Not the database collation: ``a-c`` (``-`` is 0x2d) sorts before ``ab``."""
    same = [_row("same.example.org"), _row("same.example.org")]
    sessions.rows = [_row("ab.example.org"), *same, _row("a-c.example.org"), _row("a.example.org")]
    body = (await _list()).json()
    keys = [(claim["hostname"], claim["domain_id"]) for claim in body["claims"]]
    assert [name for name, _ in keys] == [
        "a-c.example.org",
        "a.example.org",
        "ab.example.org",
        "same.example.org",
        "same.example.org",
    ]
    assert keys[3:] == sorted((row.hostname, str(row.id)) for row in same)
    assert keys == sorted(keys)


async def test_the_success_line_carries_the_count_only(configured, sessions, caplog) -> None:
    sessions.rows = [_row("secret-name.example.org"), _row("other.example.org")]
    with _all_logs(caplog) as captured:
        assert (await _list()).status_code == 200
    listed = _events(captured, "public_mcp_domain_claims_listed")
    assert len(listed) == 1
    assert listed[0]["log_level"] == "debug"
    assert {k: v for k, v in listed[0].items() if k not in {"event", "log_level"}} == {"count": 2}
    assert not [line for line in captured if "secret-name" in repr(line) or TOKEN in repr(line)]


# ----------------------------------------------------------------------------
# Step 4: the bounds
# ----------------------------------------------------------------------------


def test_the_bounds() -> None:
    assert routes.CLAIMS_LIMIT == 10_000
    assert routes.CLAIMS_TIMEOUT_SECONDS == 5
    assert routes.CLAIMS_SCHEMA_VERSION == 1


async def test_more_rows_than_the_limit_is_503_never_a_partial_list(
    configured, monkeypatch, sessions, caplog
) -> None:
    monkeypatch.setattr(routes, "CLAIMS_LIMIT", 2)
    sessions.rows = [_row("a.example.org"), _row("b.example.org")]
    r = await _list()
    assert r.status_code == 200
    assert len(r.json()["claims"]) == 2
    sessions.rows = [_row("a.example.org"), _row("b.example.org"), _row("c.example.org")]
    with _all_logs(caplog) as captured:
        _assert_empty(await _list(), 503)
    assert _sql(sessions.statements[-1]).endswith("LIMIT 3")
    over = _events(captured, "public_mcp_domain_claims_over_limit")
    assert len(over) == 1 and over[0]["log_level"] == "warning"
    assert sessions.opened == sessions.closed == 2
    assert not routes._CLAIMS_SLOT.locked()


async def test_a_busy_slot_is_503_without_a_session(configured, sessions, caplog) -> None:
    assert not routes._CLAIMS_SLOT.locked()
    await routes._CLAIMS_SLOT.acquire()
    try:
        assert routes._CLAIMS_SLOT.locked()
        with _all_logs(caplog) as captured:
            _assert_empty(await _list(), 503)
    finally:
        routes._CLAIMS_SLOT.release()
    assert sessions.opened == 0
    assert len(_events(captured, "public_mcp_domain_claims_busy")) == 1
    assert (await _list()).status_code == 200


async def test_a_second_concurrent_read_is_503(configured, sessions) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(statement: Any, *args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()

        class _Result:
            def all(self) -> list[Row]:
                return [_row()]

        return _Result()

    sessions.execute = slow
    async with _client() as c:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        first = asyncio.create_task(c.get(PATH, headers=auth))
        await asyncio.wait_for(started.wait(), 5)
        second = await c.get(PATH, headers=auth)
        release.set()
        answered = await first
    _assert_empty(second, 503)
    assert answered.status_code == 200
    assert sessions.opened == sessions.closed == 1
    assert not routes._CLAIMS_SLOT.locked()


async def test_a_read_slower_than_the_bound_is_503(
    configured, monkeypatch, sessions, caplog
) -> None:
    cancelled = asyncio.Event()

    async def slow(statement: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    sessions.execute = slow
    monkeypatch.setattr(routes, "CLAIMS_TIMEOUT_SECONDS", 0.05)
    with _all_logs(caplog) as captured:
        _assert_empty(await _list(), 503)
    assert cancelled.is_set()
    assert sessions.opened == sessions.closed == 1
    assert not routes._CLAIMS_SLOT.locked()
    failed = _events(captured, "public_mcp_domain_claims_failed")
    assert [e["error_type"] for e in failed] == ["TimeoutError"]


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT 1", {}, Exception(f"connection refused for {CLAIMED}")),
        RuntimeError(f"boom {CLAIMED}"),
        ValueError(f"boom {CLAIMED}"),
    ],
    ids=["database", "runtime", "value"],
)
async def test_a_failing_read_is_503_without_its_message(configured, sessions, caplog, error):
    async def failing(statement: Any, *args: Any, **kwargs: Any) -> Any:
        raise error

    sessions.execute = failing
    with _all_logs(caplog) as captured:
        _assert_empty(await _list(), 503)
    assert sessions.opened == sessions.closed == 1
    failed = _events(captured, "public_mcp_domain_claims_failed")
    assert len(failed) == 1
    assert {k: v for k, v in failed[0].items() if k not in {"event", "log_level"}} == {
        "error_type": type(error).__name__
    }
    assert not [line for line in captured if CLAIMED in repr(line)]
    assert not routes._CLAIMS_SLOT.locked()


async def test_a_session_that_cannot_open_is_503(configured, monkeypatch) -> None:
    async def no_session() -> AsyncIterator[Any]:
        raise OperationalError("SELECT 1", {}, Exception("no pool"))
        yield  # pragma: no cover

    monkeypatch.setattr(routes, "get_db", no_session)
    _assert_empty(await _list(), 503)
    assert not routes._CLAIMS_SLOT.locked()


# ----------------------------------------------------------------------------
# Hosts and the route's shape
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("host", [INTERNAL_HOST, "testserver", "127.0.0.1:8080", APP_HOST])
async def test_application_and_internal_hosts_reach_the_handler(configured, sessions, host):
    assert (await _list(host=host)).status_code == 200


async def test_a_foreign_host_never_reaches_the_handler(configured, sessions) -> None:
    r = await _list(host=CLAIMED)
    assert r.status_code == 404
    assert sessions.opened == 0


def test_the_route_is_get_only_without_a_dependency_and_not_in_the_schema() -> None:
    from fastapi.routing import APIRoute, iter_route_contexts

    from api.main import app

    found = [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and context.path == PATH
    ]
    assert len(found) == 1
    assert found[0].methods == {"GET"}
    assert found[0].dependant.dependencies == []
    assert [p.name for p in found[0].dependant.query_params] == []
    assert PATH not in app.openapi()["paths"]
