"""The public endpoint and the authenticated ``/mcp`` stay apart in the app (#2675).

Through ``api.main.app`` (not the ASGI callables), so route order is what is
tested: ``/mcp/public/...`` must reach the public app and never the
``/mcp{path:path}`` catch-all, and ``/mcp`` without credentials must still be
a 401 with a challenge.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
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
