"""The proxy's certificate permission call (#2809, R10).

``GET /internal/public-mcp-domains/tls-permission?domain=<name>`` answers 200
only for a name the alias statement serves now, 404 for anything else and 503
when it cannot answer in time. It carries no credential and is reachable only
under an application or internal Host (the guard answers 404 for ``/internal``
under any other Host). The steps run in a fixed order:

1. either switch off: 404;
2. ``domain`` missing or not claimable: 404;
3. the module semaphore cannot be taken without waiting: 503;
4. the lookup, in its own short session, bounded in time;
5. an endpoint: 200; none: 404; timeout or any exception: 503.

Steps 1 to 3 open no session. Every answer is an empty body; no exception
leaves the handler; no log line carries the name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from api.routes import internal_public_mcp_domains as permission
from config.settings import get_settings
from db.base import get_db
from mcp_server.public.publication import ServingEndpoint
from models.public_mcp_domain import PublicMcpDomain
from utils.datetime import utcnow

PATH = "/internal/public-mcp-domains/tls-permission"
ALIAS = "alias.example.org"
INTERNAL_HOST = "api-blue:8080"
APP_HOST = "app.example.com"


@pytest.fixture
def domains_on(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setenv("API_URL", "https://api.example.com")
    monkeypatch.setattr(s, "mcp_public_domains_enabled", True)
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(s, "frontend_url", f"https://{APP_HOST}")
    monkeypatch.setattr(s, "oidc_issuer", "")
    monkeypatch.setattr(s, "kmc_mcp_url", "")
    monkeypatch.setattr(s, "payment_public_base_url", "")
    monkeypatch.setattr(s, "cors_origins", f"https://{APP_HOST}")
    monkeypatch.setattr(s, "mcp_allowed_origins", "")
    monkeypatch.setattr(s, "mcp_public_domains_routing_target", "routing.example.net")
    monkeypatch.setattr(s, "mcp_public_domains_app_hosts", ".svc.example.net")
    monkeypatch.setattr(s, "mcp_public_domains_reserved_suffixes", "reserved.example.org")


def _endpoint() -> ServingEndpoint:
    return ServingEndpoint(
        id=uuid4(),
        public_id=f"pmcp_{'P' * 22}",
        workspace_id=uuid4(),
        context_id=uuid4(),
        resource_pk=uuid4(),
        resource_id="catalog",
        public_name="Catalog",
        public_description=None,
        public_usage_guide=None,
        context_name="catalog",
        owner_user_id="owner-1",
    )


class _Sessions:
    """Stands in for ``get_db``: records each session it opens and closes."""

    def __init__(self, *, fail_on_open: BaseException | None = None) -> None:
        self.opened = 0
        self.closed = 0
        self.sessions: list[object] = []
        self._fail_on_open = fail_on_open

    async def __call__(self) -> AsyncIterator[object]:
        self.opened += 1
        if self._fail_on_open is not None:
            raise self._fail_on_open
        session = object()
        self.sessions.append(session)
        try:
            yield session
        finally:
            self.closed += 1


@pytest.fixture
def sessions(monkeypatch) -> _Sessions:
    spy = _Sessions()
    monkeypatch.setattr(permission, "get_db", spy)
    return spy


@pytest.fixture
def loader(monkeypatch) -> AsyncMock:
    load = AsyncMock(return_value=_endpoint())
    monkeypatch.setattr(permission, "load_serving_endpoint_for_alias", load)
    return load


@contextlib.contextmanager
def _all_logs(caplog) -> Iterator[list[str]]:
    """Every structlog event and server-side stdlib record emitted inside the block, as text.

    The records are taken raw, before any formatter (so before the scrubber).
    The test client's own ``httpx`` request lines are left out: they are not
    the server's.
    """
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as events:
        lines: list[str] = []
        yield lines
    lines.extend(repr(event) for event in events)
    lines.extend(
        f"{record.name} {record.getMessage()} {record.__dict__!r}"
        for record in caplog.records
        if not record.name.startswith(("httpx", "httpcore"))
    )


def _client(host: str = INTERNAL_HOST) -> httpx.AsyncClient:
    from api.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


async def _ask(domain: str | None = ALIAS, *, host: str = INTERNAL_HOST, **params: str):
    query: dict[str, str] = dict(params)
    if domain is not None:
        query["domain"] = domain
    async with _client(host) as c:
        return await c.get(PATH, params=query)


def _assert_empty(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.content == b""


# ----------------------------------------------------------------------------
# Steps 1 and 2: no session
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(("domains", "endpoints"), [(False, True), (True, False), (False, False)])
async def test_either_switch_off_is_404_without_a_session(
    domains_on, monkeypatch, sessions, loader, domains, endpoints
) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "mcp_public_domains_enabled", domains)
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", endpoints)
    _assert_empty(await _ask(), 404)
    assert sessions.opened == 0
    loader.assert_not_awaited()


@pytest.mark.parametrize(
    "domain",
    [
        "",
        "localhost",
        "under_score.example.org",
        "*.example.org",
        "alias.example.org:443",
        "203.0.113.7",
        "x" * 64 + ".example.org",
        "tést.example.org",
        "x.onion",
        "x.1.0.0.2.ip6.arpa",
        "nas.home.arpa",
        "reserved.example.org",
        "deep.reserved.example.org",
        "routing.example.net",
        APP_HOST,
        "api.example.com",
        "x.svc.example.net",
    ],
)
async def test_a_name_that_cannot_be_claimed_is_404_without_a_session(
    domains_on, sessions, loader, domain
) -> None:
    _assert_empty(await _ask(domain), 404)
    assert sessions.opened == 0
    loader.assert_not_awaited()


@pytest.mark.parametrize(
    "domain", ["proof.example.net", f"{'a1' * 16}.proof.example.net", "deep.x.proof.example.net"]
)
async def test_a_name_in_the_cname_proof_zone_is_404_without_a_session(
    domains_on, monkeypatch, sessions, loader, domain
) -> None:
    """#2979: an unclaimed name pointing into the zone, and the zone's own
    names, never reach the alias statement: no certificate is issued."""
    monkeypatch.setattr(get_settings(), "mcp_public_domains_cname_proof_zone", "proof.example.net")
    _assert_empty(await _ask(domain), 404)
    assert sessions.opened == 0
    loader.assert_not_awaited()


async def test_a_missing_domain_is_404_without_a_session(domains_on, sessions, loader) -> None:
    _assert_empty(await _ask(None), 404)
    _assert_empty(await _ask(None, name=ALIAS), 404)
    assert sessions.opened == 0
    loader.assert_not_awaited()


async def test_two_domain_values_are_404_without_a_session(domains_on, sessions, loader) -> None:
    async with _client() as c:
        r = await c.get(f"{PATH}?domain={ALIAS}&domain={ALIAS}")
    _assert_empty(r, 404)
    assert sessions.opened == 0
    loader.assert_not_awaited()


# ----------------------------------------------------------------------------
# Step 3: bounded concurrency
# ----------------------------------------------------------------------------


async def test_a_busy_semaphore_is_503_without_a_session(domains_on, sessions, loader) -> None:
    taken = 0
    try:
        while not permission._SLOTS.locked():
            await permission._SLOTS.acquire()
            taken += 1
        assert taken == permission.PERMISSION_CONCURRENCY == 4
        _assert_empty(await _ask(), 503)
    finally:
        for _ in range(taken):
            permission._SLOTS.release()
    assert sessions.opened == 0
    loader.assert_not_awaited()
    _assert_empty(await _ask(), 200)


async def test_concurrent_calls_beyond_the_bound_are_503(domains_on, monkeypatch, sessions) -> None:
    release = asyncio.Event()
    started = 0

    async def slow(db, hostname):
        nonlocal started
        started += 1
        await release.wait()
        return _endpoint()

    monkeypatch.setattr(permission, "load_serving_endpoint_for_alias", slow)
    async with _client() as c:
        first = [asyncio.create_task(c.get(PATH, params={"domain": ALIAS})) for _ in range(4)]
        for _ in range(100):
            if started == 4:
                break
            await asyncio.sleep(0.01)
        assert started == 4
        extra = await c.get(PATH, params={"domain": ALIAS})
        release.set()
        answers = await asyncio.gather(*first)
    _assert_empty(extra, 503)
    assert [r.status_code for r in answers] == [200] * 4
    assert sessions.opened == sessions.closed == 4
    assert not permission._SLOTS.locked()


# ----------------------------------------------------------------------------
# Steps 4 and 5: the lookup
# ----------------------------------------------------------------------------


async def test_a_served_name_is_200(domains_on, sessions, loader) -> None:
    _assert_empty(await _ask(), 200)
    assert sessions.opened == sessions.closed == 1
    loader.assert_awaited_once_with(sessions.sessions[0], ALIAS)


async def test_the_name_is_looked_up_in_its_normalised_form(domains_on, sessions, loader) -> None:
    _assert_empty(await _ask(" Alias.Example.ORG. "), 200)
    loader.assert_awaited_once_with(sessions.sessions[0], ALIAS)


async def test_an_unserved_name_is_404(domains_on, sessions, loader) -> None:
    loader.return_value = None
    _assert_empty(await _ask(), 404)
    assert sessions.opened == sessions.closed == 1


async def test_a_lookup_slower_than_the_bound_is_503(
    domains_on, monkeypatch, sessions, caplog
) -> None:
    cancelled = asyncio.Event()

    async def slow(db, hostname):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(permission, "load_serving_endpoint_for_alias", slow)
    monkeypatch.setattr(permission, "PERMISSION_TIMEOUT_SECONDS", 0.05)
    with _all_logs(caplog) as lines:
        _assert_empty(await _ask(), 503)
    assert cancelled.is_set()
    assert sessions.opened == sessions.closed == 1
    assert not permission._SLOTS.locked()
    failures = [line for line in lines if "public_mcp_domain_permission_failed" in line]
    assert len(failures) == 1 and "TimeoutError" in failures[0]


def test_the_time_bound_is_three_seconds() -> None:
    assert permission.PERMISSION_TIMEOUT_SECONDS == 3


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT 1", {}, Exception("connection refused")),
        RuntimeError("boom"),
        ValueError("boom"),
    ],
    ids=["database", "runtime", "value"],
)
async def test_a_failing_lookup_is_503(domains_on, sessions, loader, caplog, error) -> None:
    loader.side_effect = error
    with _all_logs(caplog) as lines:
        _assert_empty(await _ask(), 503)
    assert sessions.opened == sessions.closed == 1
    failures = [line for line in lines if "public_mcp_domain_permission_failed" in line]
    assert len(failures) == 1 and type(error).__name__ in failures[0]
    assert not permission._SLOTS.locked()


async def test_a_session_that_cannot_open_is_503(domains_on, monkeypatch, loader) -> None:
    spy = _Sessions(fail_on_open=OperationalError("SELECT 1", {}, Exception("no pool")))
    monkeypatch.setattr(permission, "get_db", spy)
    _assert_empty(await _ask(), 503)
    assert spy.opened == 1
    loader.assert_not_awaited()


async def test_the_real_statement_without_a_database_is_503(domains_on, monkeypatch) -> None:
    """Nothing patched but the session factory: the alias statement fails, the answer is 503."""
    import db.base as db_base

    class _NoDatabase:
        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise OperationalError("SELECT 1", {}, Exception("no database in this test"))

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def __aenter__(self) -> _NoDatabase:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(db_base, "_get_session_factory", lambda: _NoDatabase)
    _assert_empty(await _ask(), 503)


async def test_pause_and_capacity_lock_do_not_withdraw_the_permission(
    domains_on, monkeypatch, sessions, loader
) -> None:
    """Only the alias statement decides; ``serving_paused`` is a request-time answer."""
    import mcp_server.public.publication as publication

    monkeypatch.setattr(
        publication, "serving_paused", AsyncMock(side_effect=AssertionError("not here"))
    )
    _assert_empty(await _ask(), 200)


# ----------------------------------------------------------------------------
# Logs, Hosts, the route's shape
# ----------------------------------------------------------------------------


async def test_no_log_line_carries_the_name(
    domains_on, monkeypatch, sessions, loader, caplog
) -> None:
    name = "secret-name.example.org"
    with _all_logs(caplog) as lines:
        loader.return_value = _endpoint()
        await _ask(name)
        loader.return_value = None
        await _ask(name)
        loader.side_effect = RuntimeError(f"failed for {name}")
        await _ask(name)
        loader.side_effect = None
        monkeypatch.setattr(permission, "PERMISSION_TIMEOUT_SECONDS", 0.01)

        async def slow(db, hostname):
            await asyncio.sleep(1)

        monkeypatch.setattr(permission, "load_serving_endpoint_for_alias", slow)
        await _ask(name)
        await _ask(f"{name}:443")
    assert lines, "expected at least the failure lines"
    assert not [line for line in lines if "secret-name" in line]


@pytest.mark.parametrize("host", [INTERNAL_HOST, "testserver", "127.0.0.1:8080", APP_HOST])
async def test_application_and_internal_hosts_reach_the_handler(
    domains_on, sessions, loader, host
) -> None:
    _assert_empty(await _ask(host=host), 200)
    loader.assert_awaited_once()


async def test_a_foreign_host_never_reaches_the_handler(domains_on, sessions, loader) -> None:
    r = await _ask(host=ALIAS)
    assert r.status_code == 404
    assert sessions.opened == 0
    loader.assert_not_awaited()


def _route():
    from fastapi.routing import APIRoute, iter_route_contexts

    from api.main import app

    found = [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and context.path == PATH
    ]
    assert len(found) == 1
    return found[0]


def test_the_route_takes_no_dependency() -> None:
    context = _route()
    assert context.methods == {"GET"}
    assert context.dependant.dependencies == []
    assert [p.name for p in context.dependant.query_params] == []


def test_the_route_is_not_in_the_openapi_document() -> None:
    from api.main import app

    assert not [path for path in app.openapi()["paths"] if "public-mcp-domains" in path]


# ----------------------------------------------------------------------------
# The edge certificate report (#2941): one log event per outcome
# ----------------------------------------------------------------------------

REPORT_PATH = "/internal/public-mcp-domains/tls-observation"
REPORTER = "reporter-" + "c" * 40
REPORTED = "cert.example.org"


class _ReportSession:
    """Stands in for the route's session: the claim row it reads, whether the write stores."""

    def __init__(self, row: PublicMcpDomain | None, stored: bool) -> None:
        self._row = row
        self._stored = stored

    async def scalar(self, *_args: Any, **_kwargs: Any) -> PublicMcpDomain | None:
        return self._row

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        stored = self._stored

        class _Result:
            def scalar_one_or_none(self) -> object | None:
                return uuid4() if stored else None

        return _Result()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _sessions(session: _ReportSession) -> Any:
    async def db() -> AsyncIterator[_ReportSession]:
        yield session

    return db


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _report(row: PublicMcpDomain, observed: datetime, until: datetime | None = None) -> dict:
    return {
        "domain_id": str(row.id),
        "hostname": row.hostname,
        "endpoint_id": str(row.endpoint_id),
        "tls": {
            "status": "valid",
            "observed_at": _iso(observed),
            "valid_until": _iso(until or observed + timedelta(minutes=3)),
            "certificate_not_after": _iso(observed + timedelta(days=30)),
        },
    }


async def _put(token: str, headers: dict, body: Any, session: _ReportSession) -> Any:
    get_settings().mcp_public_domains_tls_reporter_token = token
    app = FastAPI()
    app.include_router(permission.router)
    app.dependency_overrides[get_db] = _sessions(session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{INTERNAL_HOST}"
    ) as c:
        if isinstance(body, bytes):
            return await c.put(
                REPORT_PATH, content=body, headers={**headers, "Content-Type": "application/json"}
            )
        return await c.put(REPORT_PATH, json=body, headers=headers)


async def test_each_report_outcome_logs_one_event_without_secrets(
    domains_on, monkeypatch, caplog
) -> None:
    """One line per outcome, named for it; ``disabled`` only at debug level; never a secret."""
    monkeypatch.setattr(get_settings(), "mcp_public_domains_tls_reporter_token", REPORTER)
    row = PublicMcpDomain(
        id=uuid4(),
        endpoint_id=uuid4(),
        hostname=REPORTED,
        state="active",
        created_at=utcnow() - timedelta(hours=1),
    )
    now = datetime.now(UTC)
    fresh = _report(row, now - timedelta(seconds=5))
    future = _report(row, now + timedelta(seconds=60))
    expired = _report(row, now - timedelta(minutes=4), now - timedelta(minutes=1))
    bearer = {"Authorization": f"Bearer {REPORTER}"}
    wrong = {"Authorization": "Bearer wrong"}
    oversized = b"{" + b" " * 5000 + b"}"
    # (token, headers, body, claim row, stored, status, event, reason, level)
    cases: list[tuple[Any, ...]] = [
        ("", bearer, fresh, row, True, 404, "refused", "disabled", "debug"),
        (REPORTER, wrong, fresh, row, True, 401, "refused", "credential", "warning"),
        (REPORTER, bearer, oversized, row, True, 413, "refused", "size", "warning"),
        (REPORTER, bearer, {}, row, True, 422, "refused", "invalid", "warning"),
        (REPORTER, bearer, fresh, None, True, 404, "refused", "binding", "warning"),
        (REPORTER, bearer, future, row, True, 409, "refused", "future", "warning"),
        (REPORTER, bearer, expired, row, True, 409, "refused", "expired", "warning"),
        (REPORTER, bearer, fresh, row, False, 409, "refused", "older", "warning"),
        (REPORTER, bearer, fresh, row, True, 204, "stored", None, "info"),
    ]
    caplog.set_level(logging.DEBUG)
    lines: list[str] = []
    for token, headers, body, claim, stored, status, event, reason, level in cases:
        with structlog.testing.capture_logs() as events:
            r = await _put(token, headers, body, _ReportSession(claim, stored))
        assert (r.status_code, r.content) == (status, b""), (reason, r.status_code)
        lines.extend(repr(e) for e in events)
        reports = [e for e in events if e["event"].startswith("public_mcp_domain_tls_report_")]
        if level == "debug":
            # Below the default log level: no warning or info line at all; when
            # debug lines are kept, the one line says why.
            assert all((e["log_level"], e.get("reason")) == ("debug", reason) for e in reports)
            continue
        assert len(reports) == 1, (reason, events)
        assert reports[0]["event"] == f"public_mcp_domain_tls_report_{event}"
        assert reports[0]["log_level"] == level, reason
        if event == "refused":
            assert reports[0]["reason"] == reason
        else:
            assert (reports[0]["status"], reports[0]["domain_id"]) == ("valid", str(row.id))
    lines.extend(
        f"{record.name} {record.getMessage()} {record.__dict__!r}"
        for record in caplog.records
        if not record.name.startswith("httpx")
    )
    assert len([line for line in lines if "public_mcp_domain_tls_report_" in line]) >= 8
    assert not [line for line in lines if REPORTER in line or REPORTED in line]
