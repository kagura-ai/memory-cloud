"""The Host guard of verified custom domains (#2809, R5).

``PublicMcpDomainHostGuard`` is driven directly with a spy inner app and a spy
public app, then through ``api.main.app`` to pin that it is the outermost
middleware: a request under a foreign Host reaches neither the session, the
rate-limit, the request-logging, the correlation nor the CORS middleware.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.middleware.cors import CORSMiddleware

from api.middleware.correlation import CorrelationMiddleware
from api.middleware.public_mcp_domain_host import PublicMcpDomainHostGuard
from api.middleware.rate_limit import RateLimitMiddleware
from api.middleware.request_logger import RequestLoggingMiddleware
from config.settings import get_settings
from mcp_server.public import transport as pt
from mcp_server.public.transport import NOT_PUBLISHED_BODY

APP_HOST = "app.example.com"
ALIAS = "alias.example.org"
NOT_PUBLISHED_BYTES = json.dumps(NOT_PUBLISHED_BODY).encode()


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


class _Spy:
    """An ASGI app that records its scopes and answers 200 with a cookie."""

    def __init__(self) -> None:
        self.scopes: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"set-cookie", b"kagura_session=x"]],
                }
            )
            await send({"type": "http.response.body", "body": b"inner"})


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return self.messages[0]["status"]

    @property
    def headers(self) -> dict[bytes, bytes]:
        return {bytes(k).lower(): bytes(v) for k, v in self.messages[0].get("headers", [])}

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages[1:])


class _WatchedScope(dict):
    """A scope that remembers which keys were read."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return super().get(key, default)


def _scope(
    path: str = "/mcp",
    host: bytes | None = ALIAS.encode(),
    *,
    kind: str = "http",
    method: str = "POST",
    extra: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host))
    headers.extend(extra or [])
    scope: dict[str, Any] = {
        "type": kind,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"tools=recall",
        "headers": headers,
        "root_path": "",
    }
    if kind == "http":
        scope["method"] = method
    return scope


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _run(scope: dict) -> tuple[_Spy, _Spy, _Recorder]:
    inner, public = _Spy(), _Spy()
    send = _Recorder()
    await PublicMcpDomainHostGuard(inner, public_app=public)(scope, _receive, send)
    return inner, public, send


def _assert_not_published(send: _Recorder) -> None:
    assert send.status == 404
    assert send.body == NOT_PUBLISHED_BYTES
    assert b"set-cookie" not in send.headers
    assert not any(name.startswith(b"access-control-") for name in send.headers)


# ----------------------------------------------------------------------------
# Switch off: no Host rule at all
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/mcp", "/api/v1/auth/login", "/"])
async def test_switch_off_calls_the_inner_app_without_reading_headers(monkeypatch, path) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_domains_enabled", False)
    scope = _WatchedScope(_scope(path, extra=[(b"host", b"other.example.org")]))
    inner, public, send = await _run(scope)
    assert inner.scopes == [scope]
    assert inner.scopes[0] is scope
    assert "headers" not in scope.read
    assert public.scopes == []
    assert send.status == 200


# ----------------------------------------------------------------------------
# Header shape
# ----------------------------------------------------------------------------


@pytest.mark.usefixtures("domains_on")
@pytest.mark.parametrize("path", ["/mcp", "/health"])
@pytest.mark.parametrize(
    "hosts",
    [(APP_HOST.encode(), ALIAS.encode()), (APP_HOST.encode(), APP_HOST.encode())],
)
async def test_two_host_headers_are_a_bad_request(path, hosts) -> None:
    inner, public, send = await _run(_scope(path, hosts[0], extra=[(b"host", hosts[1])]))
    assert send.status == 400
    assert json.loads(send.body) == {"error": "bad_request"}
    assert inner.scopes == [] and public.scopes == []


@pytest.mark.usefixtures("domains_on")
@pytest.mark.parametrize(
    "host",
    [
        APP_HOST.encode(),
        b"APP.Example.com.",
        b"app.example.com:443",
        b"api.example.com",
        b"api.svc.example.net",
        b"svc.example.net:8080",
        b"testserver",
        b"api-blue:8080",
        b"10.0.0.5",
        b"10.0.0.5:8080",
        b"[::1]:8080",
        b"x.internal",
        b"nas.home.arpa",
        b"",
        None,
    ],
)
@pytest.mark.parametrize("path", ["/mcp", "/api/v1/auth/login"])
async def test_application_and_internal_hosts_pass_untouched(host, path) -> None:
    scope = _scope(path, host)
    snapshot = dict(scope)
    inner, public, send = await _run(scope)
    assert len(inner.scopes) == 1
    assert inner.scopes[0] is scope
    assert scope == snapshot
    assert public.scopes == []
    assert send.status == 200


@pytest.mark.usefixtures("domains_on")
@pytest.mark.parametrize("host", [b"x.1.0.0.2.ip6.arpa", b"4.3.2.1.in-addr.arpa", b"x.onion"])
async def test_arpa_and_onion_names_are_foreign_and_never_served(host) -> None:
    for path in ("/api/v1/auth/login", "/mcp"):
        inner, public, send = await _run(_scope(path, host))
        _assert_not_published(send)
        assert inner.scopes == [] and public.scopes == []


# ----------------------------------------------------------------------------
# Foreign Host: only /mcp, and only for a claimable name
# ----------------------------------------------------------------------------

_SYNC_ID = "00000000-0000-4000-8000-000000000001"

ROUTE_INVENTORY = [
    "/api/v1/auth/login",
    "/api/v1/oauth/authorize",
    "/api/v1/workspaces",
    # #2807's staged feed routes: a foreign Host never reaches them either.
    "/api/v1/sync/installations/" + _SYNC_ID + "/schema",
    "/api/v1/sync/installations/" + _SYNC_ID + "/runs",
    "/api/v1/workspaces/" + _SYNC_ID + "/sync-installations/" + _SYNC_ID + "/records",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/oidc/jwks",
    "/oidc/authorize",
    "/internal/public-mcp-domains/tls-permission",
    "/internal/billing/plan",
    "/docs",
    "/openapi.json",
    "/health",
    "/readiness",
    "/",
    "/static/api/openapi.json",
    "/mcp/w/x",
    "/mcp/public/pmcp_" + "A" * 22,
    "/mcp/public/pmcp_" + "A" * 22 + "/",
    "/mcp/messages",
    "/mcpx",
    "/MCP",
    "//mcp",
]


@pytest.mark.usefixtures("domains_on")
@pytest.mark.parametrize("path", ROUTE_INVENTORY)
@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS", "DELETE"])
async def test_foreign_host_on_any_other_path_is_404(path, method) -> None:
    extra = [
        (b"cookie", b"kagura_session=abc"),
        (b"origin", f"https://{APP_HOST}".encode()),
        (b"access-control-request-method", b"POST"),
    ]
    inner, public, send = await _run(_scope(path, method=method, extra=extra))
    _assert_not_published(send)
    assert inner.scopes == []
    assert public.scopes == []


@pytest.mark.usefixtures("domains_on")
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize(
    ("host", "name"),
    [
        (ALIAS.encode(), ALIAS),
        (b"Alias.Example.ORG.", ALIAS),
        (b"alias.example.org:443", ALIAS),
        (b"mcp.customer.example.com", "mcp.customer.example.com"),
    ],
)
async def test_foreign_host_on_mcp_goes_to_the_public_app(path, host, name) -> None:
    scope = _scope(path, host)
    scope["public_endpoint_public_id"] = "pmcp_" + "A" * 22
    snapshot = dict(scope)
    inner, public, _send = await _run(scope)
    assert inner.scopes == []
    assert len(public.scopes) == 1
    forwarded = public.scopes[0]
    assert forwarded is not scope
    assert scope == snapshot
    assert forwarded["path"] == "/mcp/public/"
    assert forwarded["public_endpoint_alias_host"] == name
    assert "public_endpoint_public_id" not in forwarded
    for key in ("type", "method", "headers", "query_string"):
        assert forwarded[key] == scope[key]


@pytest.mark.usefixtures("domains_on")
@pytest.mark.parametrize(
    "host",
    [
        b"sub.app.example.com",
        b"x.reserved.example.org",
        b"reserved.example.org",
        b"routing.example.net",
        b"edge.routing.example.net",
        b"x.onion",
        b"under_score.example.org",
        b"*.example.org",
        b"a:b:c.example.org",
        ("x" * 240 + ".example.org").encode(),
    ],
)
async def test_reserved_or_malformed_name_on_mcp_is_404_without_the_transport(host) -> None:
    inner, public, send = await _run(_scope("/mcp", host))
    _assert_not_published(send)
    assert inner.scopes == [] and public.scopes == []


@pytest.mark.usefixtures("domains_on")
async def test_forwarded_host_headers_change_nothing() -> None:
    forwarded = [
        (b"x-forwarded-host", ALIAS.encode()),
        (b"forwarded", f"host={ALIAS}".encode()),
    ]
    # Application Host, a forwarded header naming an alias: the application.
    scope = _scope("/mcp", APP_HOST.encode(), extra=forwarded)
    inner, public, _send = await _run(scope)
    assert inner.scopes == [scope] and public.scopes == []

    # Foreign Host, a forwarded header naming the application: still foreign.
    back = [
        (b"x-forwarded-host", APP_HOST.encode()),
        (b"forwarded", f"host={APP_HOST}".encode()),
    ]
    inner, public, send = await _run(_scope("/api/v1/auth/login", ALIAS.encode(), extra=back))
    _assert_not_published(send)
    assert inner.scopes == []

    # Foreign Host on /mcp: the Host's name, never the forwarded one.
    inner, public, _send = await _run(_scope("/mcp", ALIAS.encode(), extra=back))
    assert inner.scopes == []
    assert public.scopes[0]["public_endpoint_alias_host"] == ALIAS


@pytest.mark.usefixtures("domains_on")
async def test_foreign_websocket_is_closed_before_accept() -> None:
    inner, public, send = await _run(_scope("/mcp", kind="websocket"))
    assert send.messages == [{"type": "websocket.close", "code": 1008}]
    assert inner.scopes == [] and public.scopes == []


@pytest.mark.usefixtures("domains_on")
async def test_application_websocket_and_lifespan_pass() -> None:
    scope = _scope("/ws", APP_HOST.encode(), kind="websocket")
    inner, _public, _send = await _run(scope)
    assert inner.scopes == [scope]
    lifespan = {"type": "lifespan"}
    inner, _public, _send = await _run(lifespan)
    assert inner.scopes == [lifespan]


@pytest.mark.usefixtures("domains_on")
async def test_the_404_has_the_bytes_of_the_transport() -> None:
    _inner, _public, guard_send = await _run(_scope("/health"))
    transport_send = _Recorder()
    await pt._send_not_published(transport_send)
    assert guard_send.messages == transport_send.messages


# ----------------------------------------------------------------------------
# Through api.main.app: the guard is the outermost middleware
# ----------------------------------------------------------------------------


def test_the_guard_is_the_outermost_user_middleware() -> None:
    from api.main import app

    assert app.user_middleware[0].cls is PublicMcpDomainHostGuard
    stack = app.build_middleware_stack()
    chain: list[type] = []
    node: Any = stack
    while node is not None and len(chain) < 50:
        chain.append(type(node))
        node = getattr(node, "app", None)
    guard_at = chain.index(PublicMcpDomainHostGuard)
    # Only the framework's own wrappers (Starlette's ServerErrorMiddleware,
    # FastAPI's exception telemetry) sit outside the guard.
    for outer in chain[:guard_at]:
        assert outer.__module__.split(".")[0] in ("starlette", "fastapi"), outer
    from api.main import SessionMiddlewareWrapper

    for later in (
        CorrelationMiddleware,
        RequestLoggingMiddleware,
        RateLimitMiddleware,
        SessionMiddlewareWrapper,
        CORSMiddleware,
    ):
        assert chain.index(later) > guard_at, later


def _wrapped_middleware() -> list[type]:
    from api.main import SessionMiddlewareWrapper

    return [
        CORSMiddleware,
        SessionMiddlewareWrapper,
        RateLimitMiddleware,
        RequestLoggingMiddleware,
        CorrelationMiddleware,
    ]


@pytest.fixture
def reached(monkeypatch) -> list[str]:
    """Record which of the application's middleware a request passes through."""
    names: list[str] = []
    for cls in _wrapped_middleware():
        original = cls.__call__

        async def spy(self, scope, receive, send, _original=original, _name=cls.__name__):
            if scope.get("type") == "http":
                names.append(_name)
            await _original(self, scope, receive, send)

        monkeypatch.setattr(cls, "__call__", spy)
    return names


@pytest.mark.usefixtures("domains_on")
async def test_a_foreign_request_reaches_no_application_middleware(monkeypatch, reached) -> None:
    from api.main import app

    load_alias = AsyncMock(return_value=(None, False))
    monkeypatch.setattr(pt, "_load_alias", load_alias)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{ALIAS}") as c:
        for path in (
            "/api/v1/auth/login",
            "/health",
            "/.well-known/openid-configuration",
            "/api/v1/sync/installations/" + _SYNC_ID + "/schema",
        ):
            r = await c.get(path, headers={"Origin": f"https://{APP_HOST}"})
            assert r.status_code == 404, path
            assert r.json() == NOT_PUBLISHED_BODY
            assert "set-cookie" not in r.headers
            assert "access-control-allow-origin" not in r.headers
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 404
        assert "www-authenticate" not in r.headers
    assert reached == []
    load_alias.assert_awaited_once_with(ALIAS)

    # The same spies see an application request: the proof above is not vacuous.
    async with httpx.AsyncClient(transport=transport, base_url=f"https://{APP_HOST}") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert set(reached) == {cls.__name__ for cls in _wrapped_middleware()}
