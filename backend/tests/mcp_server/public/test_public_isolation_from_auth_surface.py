"""The public endpoint and the authenticated ``/mcp`` stay apart in the app (#2675).

Through ``api.main.app`` (not the ASGI callables), so route order is what is
tested: ``/mcp/public/...`` must reach the public app and never the
``/mcp{path:path}`` catch-all, and ``/mcp`` without credentials must still be
a 401 with a challenge.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx

from config.settings import get_settings
from mcp_server.public import transport as pt
from mcp_server.public.publication import ServingEndpoint

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
}


def _client() -> httpx.AsyncClient:
    from api.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_mcp_without_credentials_still_challenges() -> None:
    async with _client() as c:
        r = await c.post("/mcp", json=INIT)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer ")


async def test_public_path_with_the_switch_off_is_404_not_401(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", False)
    async with _client() as c:
        for path in (f"/mcp/public/pmcp_{'A' * 22}", f"/mcp/public/pmcp_{'A' * 22}/"):
            r = await c.post(path, json=INIT)
            assert r.status_code == 404, path
            assert r.json()["error"] == "not_found"
            assert "www-authenticate" not in r.headers


async def test_public_path_reaches_the_public_app(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    endpoint = ServingEndpoint(
        id=uuid4(),
        public_id=f"pmcp_{'B' * 22}",
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
    async with _client() as c:
        r = await c.post(
            f"/mcp/public/{endpoint.public_id}",
            json=INIT,
            headers={"Authorization": "Bearer not-a-key"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "kagura-memory-cloud-public"
    assert "www-authenticate" not in r.headers
    assert r.headers["mcp-session-id"].startswith("mcp-")
    load.assert_awaited_once_with(endpoint.public_id)


async def test_well_known_metadata_does_not_mention_the_public_path() -> None:
    async with _client() as c:
        r = await c.get("/.well-known/oauth-protected-resource")
    assert "/mcp/public" not in r.text


# ----------------------------------------------------------------------------
# #2809: a foreign Host on /mcp is the alias entry, never the authenticated one
# ----------------------------------------------------------------------------

ALIAS = "alias.example.org"


def _endpoint() -> ServingEndpoint:
    return ServingEndpoint(
        id=uuid4(),
        public_id=f"pmcp_{'C' * 22}",
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


def _domains_on(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(s, "mcp_public_domains_enabled", True)
    monkeypatch.setattr(s, "frontend_url", "https://app.example.com")
    monkeypatch.setattr(s, "mcp_public_domains_app_hosts", "")
    monkeypatch.setattr(s, "mcp_public_domains_reserved_suffixes", "")


def _host_client(host: str) -> httpx.AsyncClient:
    from api.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


async def test_foreign_host_on_mcp_never_reaches_the_authenticated_transport(
    monkeypatch,
) -> None:
    import api.main as main

    _domains_on(monkeypatch)
    authenticated = AsyncMock()
    monkeypatch.setattr(main, "mcp_asgi_app", authenticated)
    endpoint = _endpoint()
    load_alias = AsyncMock(return_value=(endpoint, False))
    monkeypatch.setattr(pt, "_load_alias", load_alias)
    monkeypatch.setattr(pt, "check_public_rate_limit", AsyncMock(return_value=None))

    async with _host_client(ALIAS) as c:
        r = await c.post("/mcp", json=INIT, headers={"Authorization": "Bearer not-a-key"})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["serverInfo"]["name"] == "kagura-memory-cloud-public"
        assert "www-authenticate" not in r.headers
        load_alias.return_value = (None, False)
        for path in ("/mcp", "/mcp/"):
            r = await c.post(path, json=INIT)
            assert r.status_code == 404
            assert r.json() == pt.NOT_PUBLISHED_BODY
            assert "www-authenticate" not in r.headers
        r = await c.post(f"/mcp/public/{endpoint.public_id}", json=INIT)
        assert r.status_code == 404
    authenticated.assert_not_awaited()
    assert [call.args for call in load_alias.await_args_list] == [(ALIAS,)] * 3


async def test_foreign_host_with_a_session_cookie_reads_no_session(monkeypatch) -> None:
    import api.main as main
    import api.middleware.request_logger as request_logger
    from auth.session import SESSION_COOKIE_NAME

    _domains_on(monkeypatch)
    sessions = MagicMock()
    sessions.get_session.return_value = {
        "sub": "user-1",
        "user_id": "user-1",
        "email": "user@example.com",
        "role": "user",
    }
    monkeypatch.setattr(main, "_session_middleware_manager", sessions)
    log_usage = AsyncMock()
    monkeypatch.setattr(request_logger, "log_usage", log_usage)
    monkeypatch.setattr(pt, "_load_alias", AsyncMock(return_value=(None, False)))
    cookies = {SESSION_COOKIE_NAME: "a-session-id"}

    async with _host_client(ALIAS) as c:
        c.cookies.update(cookies)
        r = await c.post("/mcp", json=INIT)
    assert r.status_code == 404
    sessions.get_session.assert_not_called()
    log_usage.assert_not_awaited()

    # The same cookie on the application's own Host is read: the spies work.
    async with _host_client("app.example.com") as c:
        c.cookies.update(cookies)
        r = await c.post("/mcp", json=INIT)
    assert r.status_code == 401
    sessions.get_session.assert_called_once_with("a-session-id")
    log_usage.assert_awaited_once()
