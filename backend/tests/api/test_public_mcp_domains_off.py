"""Verified custom domains switched off: everything behaves as before #2809.

With ``MCP_PUBLIC_DOMAINS_ENABLED=false`` the server runs no statement on
``public_mcp_domains`` and no answer changes. Every database session in these
tests comes from a factory that fails the test on any statement naming that
table (``no_domain_statements``).

Rows (later work packages add theirs):

- the Host guard applies no Host rule and reads no header;
- the public transport refuses the alias entry with its one 404 and no lookup;
- a request under an alias-shaped Host on ``/mcp`` gets the authenticated
  transport's 401 with ``WWW-Authenticate``, exactly as any other Host;
- the fixed address ``/mcp/public/{id}`` is served under any Host;
- the certificate permission endpoint answers 404 and opens no session;
- the certificate report endpoint answers 404 before its body is read or a
  statement runs (#2941);
- the claims list answers 404 before any statement, with the reporter
  credential (#2993);
- every owner domain route answers 404 before any statement, signed in or not;
- ``/api/v1/system/info`` reports ``features.public_mcp_domains = false``;
- unpublishing an endpoint releases no domain and runs no statement on them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

import db.base as db_base
from api.middleware.public_mcp_domain_host import PublicMcpDomainHostGuard
from config.settings import get_settings
from mcp_server.public import publication
from mcp_server.public import transport as pt
from mcp_server.public.publication import ServingEndpoint

ALIAS = "alias.example.org"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
}


class _NoDomainSession:
    """A session that fails on any statement naming ``public_mcp_domains``.

    ``fallback`` answers every other statement (by default: no database here).
    """

    def __init__(self, statements: list[str], fallback: Callable[[Any], Any] | None) -> None:
        self._statements = statements
        self._fallback = fallback

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        text = str(statement)
        self._statements.append(text)
        if "public_mcp_domains" in text:
            raise AssertionError("a statement on public_mcp_domains while the switch is off")
        if self._fallback is None:
            raise RuntimeError("no database in this test")
        return self._fallback(statement)

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.execute(statement, *args, **kwargs)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _NoDomainSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def no_domain_statements(monkeypatch) -> list[str]:
    """Route every ``db.base`` session through ``_NoDomainSession``; return its statements."""
    statements: list[str] = []
    monkeypatch.setattr(
        db_base, "_get_session_factory", lambda: lambda: _NoDomainSession(statements, None)
    )
    return statements


@pytest.fixture
def switch_off(monkeypatch, no_domain_statements) -> list[str]:
    s = get_settings()
    monkeypatch.setattr(s, "mcp_public_domains_enabled", False)
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(s, "frontend_url", "https://app.example.com")
    return no_domain_statements


def _client(host: str) -> httpx.AsyncClient:
    from api.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


# ----------------------------------------------------------------------------
# Guard
# ----------------------------------------------------------------------------


class _WatchedScope(dict):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return super().get(key, default)


@pytest.mark.parametrize("kind", ["http", "websocket"])
async def test_guard_applies_no_host_rule(switch_off, kind) -> None:
    inner = AsyncMock()
    public = AsyncMock()
    scope = _WatchedScope(
        type=kind,
        method="POST",
        path="/api/v1/auth/login",
        headers=[(b"host", ALIAS.encode()), (b"host", b"other.example.org")],
    )
    await PublicMcpDomainHostGuard(inner, public_app=public)(scope, AsyncMock(), AsyncMock())
    inner.assert_awaited_once()
    assert inner.await_args.args[0] is scope
    assert "headers" not in scope.read
    public.assert_not_awaited()
    assert switch_off == []


# ----------------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------------


async def test_transport_refuses_the_alias_entry_without_a_lookup(switch_off, monkeypatch) -> None:
    load_alias = AsyncMock()
    monkeypatch.setattr(pt, "_load_alias", load_alias)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/public/",
        "query_string": b"",
        "headers": [],
        "public_endpoint_alias_host": ALIAS,
    }
    await pt.public_mcp_asgi_app(scope, receive, send)
    assert messages[0]["status"] == 404
    sent = b"".join(m.get("body", b"") for m in messages[1:])
    reference: list[dict] = []

    async def record(message: dict) -> None:
        reference.append(message)

    await pt._send_not_published(record)
    assert messages == reference
    load_alias.assert_not_awaited()
    assert sent
    assert switch_off == []


async def test_alias_lookup_runs_no_statement_while_off(switch_off) -> None:
    session: Any = db_base._get_session_factory()()
    assert await publication.load_serving_endpoint_for_alias(session, ALIAS) is None
    assert switch_off == []


# ----------------------------------------------------------------------------
# The application: an alias-shaped Host gets what any Host gets
# ----------------------------------------------------------------------------


async def test_alias_host_on_mcp_gets_the_authenticated_401(switch_off, monkeypatch) -> None:
    load_alias = AsyncMock()
    monkeypatch.setattr(pt, "_load_alias", load_alias)
    answers = []
    for host in (ALIAS, "app.example.com", "testserver"):
        async with _client(host) as c:
            r = await c.post("/mcp", json=INIT)
        answers.append((r.status_code, r.headers.get("www-authenticate"), r.content))
    assert answers[0][0] == 401
    assert answers[0][1] is not None and answers[0][1].startswith("Bearer ")
    assert answers[0] == answers[1] == answers[2]
    load_alias.assert_not_awaited()
    assert all("public_mcp_domains" not in text for text in switch_off)


@pytest.mark.parametrize("host", ["api-blue:8080", "app.example.com", "testserver", ALIAS])
async def test_permission_endpoint_is_404_without_a_session(switch_off, monkeypatch, host) -> None:
    from api.routes import internal_public_mcp_domains as permission

    opened: list[object] = []

    async def spy_get_db():
        opened.append(object())
        yield None

    lookup = AsyncMock()
    monkeypatch.setattr(permission, "get_db", spy_get_db)
    monkeypatch.setattr(permission, "load_serving_endpoint_for_alias", lookup)
    async with _client(host) as c:
        r = await c.get("/internal/public-mcp-domains/tls-permission", params={"domain": ALIAS})
    assert r.status_code == 404
    assert r.content == b""
    assert opened == []
    lookup.assert_not_awaited()
    assert switch_off == []


async def test_certificate_report_endpoint_is_404_before_anything(switch_off, monkeypatch) -> None:
    """#2941: with the switch off a report is refused before its body is read or a statement runs."""
    token = "reporter-" + "t" * 40
    monkeypatch.setattr(get_settings(), "mcp_public_domains_tls_reporter_token", token)
    async with _client("api-blue:8080") as c:
        r = await c.put(
            "/internal/public-mcp-domains/tls-observation",
            content=b"{not json",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    assert r.status_code == 404
    assert r.content == b""
    assert switch_off == []


async def test_claims_list_is_404_before_any_statement(switch_off, monkeypatch) -> None:
    """#2993: with the switch off the claims list is refused before a session opens."""
    from api.routes import internal_public_mcp_domains as routes

    opened: list[object] = []

    async def spy_get_db():
        opened.append(object())
        yield None

    token = "reporter-" + "t" * 40
    monkeypatch.setattr(get_settings(), "mcp_public_domains_tls_reporter_token", token)
    monkeypatch.setattr(routes, "get_db", spy_get_db)
    async with _client("api-blue:8080") as c:
        r = await c.get(
            "/internal/public-mcp-domains/claims", headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 404
    assert r.content == b""
    assert opened == []
    assert switch_off == []


async def test_fixed_address_is_served_under_any_host(switch_off, monkeypatch) -> None:
    endpoint = ServingEndpoint(
        id=uuid4(),
        public_id=f"pmcp_{'D' * 22}",
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
    load = AsyncMock(return_value=(endpoint, False))
    monkeypatch.setattr(pt, "_load", load)
    monkeypatch.setattr(pt, "check_public_rate_limit", AsyncMock(return_value=None))
    # The instructions' dataset counts (#2995) are their own read, on memories.
    monkeypatch.setattr(pt, "dataset_facts", AsyncMock(return_value=None))
    async with _client(ALIAS) as c:
        r = await c.post(f"/mcp/public/{endpoint.public_id}", json=INIT)
    assert r.status_code == 200, r.text
    load.assert_awaited_once_with(endpoint.public_id)
    assert switch_off == []


# ----------------------------------------------------------------------------
# Owner routes and the capability
# ----------------------------------------------------------------------------

_DOMAIN_ID = "00000000-0000-4000-8000-000000000001"
OWNER_ROUTES = [
    ("GET", "/api/v1/resources/faq/public-mcp/domains", None),
    ("POST", "/api/v1/resources/faq/public-mcp/domains", {"hostname": ALIAS}),
    ("POST", f"/api/v1/resources/faq/public-mcp/domains/{_DOMAIN_ID}/verify", None),
    ("DELETE", f"/api/v1/resources/faq/public-mcp/domains/{_DOMAIN_ID}", None),
]


@pytest.mark.parametrize(("method", "path", "body"), OWNER_ROUTES)
@pytest.mark.parametrize("signed_in", [False, True])
async def test_owner_routes_are_404_before_any_statement(
    switch_off, monkeypatch, method, path, body, signed_in
) -> None:
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from services import public_mcp_domains as service

    called: list[str] = []
    for name in ("attach_domain", "verify_domain", "detach_domain", "list_domains"):
        monkeypatch.setattr(
            service, name, AsyncMock(side_effect=lambda *a, n=name: called.append(n))
        )

    async def user() -> dict[str, Any]:
        called.append("authenticated")
        return {"user_id": "owner-1", "role": "user", "current_workspace_id": uuid4()}

    if signed_in:
        app.dependency_overrides[get_user_from_api_key_or_session] = user
    try:
        async with _client("testserver") as c:
            r = await c.request(method, path, json=body)
    finally:
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)
    assert r.status_code == 404
    assert r.json()["error"] == "RES-001"
    assert r.headers["cache-control"] == "no-store"
    assert called == []
    assert switch_off == []


async def test_system_info_reports_the_switch_off(switch_off) -> None:
    async with _client("testserver") as c:
        r = await c.get("/api/v1/system/info")
    assert r.status_code == 200
    assert r.json()["features"]["public_mcp_domains"] is False
    assert switch_off == []


# ----------------------------------------------------------------------------
# Unpublish
# ----------------------------------------------------------------------------


class _UnpublishSession(_NoDomainSession):
    async def flush(self) -> None:
        return None


@pytest.mark.parametrize("domains_on", [False, True])
async def test_unpublish_releases_domains_only_while_the_switch_is_on(
    switch_off, monkeypatch, domains_on
) -> None:
    from types import SimpleNamespace

    from api.routes import public_mcp_endpoints as routes

    monkeypatch.setattr(get_settings(), "mcp_public_domains_enabled", domains_on)
    if domains_on:
        monkeypatch.setattr(get_settings(), "mcp_public_domains_routing_target", "edge.example.net")
    row = SimpleNamespace(
        id=uuid4(),
        public_id=f"pmcp_{'U' * 22}",
        context_id=uuid4(),
        created_by="owner-1",
        revoked_at=None,
        revoked_by=None,
    )
    monkeypatch.setattr(routes, "_live_endpoint", AsyncMock(return_value=row))
    monkeypatch.setattr(routes, "record_operation", lambda *a, **k: None)
    release = AsyncMock(return_value=0)
    actor = AsyncMock(return_value="actor")
    monkeypatch.setattr(routes, "release_domains_of_endpoint", release)
    monkeypatch.setattr(routes, "owner_actor", actor)
    session: Any = _UnpublishSession(switch_off, None)
    r = await routes.unpublish_public_mcp_endpoint(
        owner=("owner-1", uuid4()), resource_id="faq", db=session
    )
    assert r.status_code == 204
    assert row.revoked_at is not None
    if domains_on:
        release.assert_awaited_once_with(session, row.id, "actor")
    else:
        release.assert_not_awaited()
        actor.assert_not_awaited()
    assert switch_off == []
