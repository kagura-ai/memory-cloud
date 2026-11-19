"""A connector key on every REST route of the app (#2929), on real rows.

A connector key authenticates the MCP tools a connector worker uses and no REST
route. Over HTTP through the whole application, with a key minted by connector
provisioning (``api_keys.connector_id`` set):

* every route that authenticates an API key answers 403 ``AUTH-204`` and never
  reaches its handler, and the decision names the route by the template the
  route table registers (the list is matched on that, never on the URL);
* every other route gives the key nothing an anonymous caller would not get:
  no handler runs for it that would not run anonymously, and no 2xx answer
  that an anonymous caller would not get;
* ``CONNECTOR_KEY_SCOPE=log`` answers as if the rule did not exist and logs one
  ``connector_key_scope_would_refuse`` event, without key material;
* an ordinary key and an agent-bound key are not affected; a paused connector's
  key and a key linked to a connector of another workspace are still 401;
* resource ingest, which authenticates by ``X-Resource-API-Key`` only, serves a
  request that also carries a connector key as a bearer exactly as without it;
* on ``POST /mcp`` a connector key (linked, or with only the reserved name)
  calling a tool outside its list gets the ``AUTH-204`` tool result, with no
  usage row and no count against the daily call limit; a listed tool runs.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import APIKey, UsageStats, Workspace
from tests.api._route_inventory import _iter_routes, route_dependency_calls

pytestmark = pytest.mark.asyncio(loop_scope="session")

REFUSAL = {
    "error": "AUTH-204",
    "message": "This credential is not accepted on this route",
    "details": {},
}

# The MCP transport's catch-all is served by the MCP sweep
# (``tests/mcp_server/test_connector_key_scope_mcp.py``): its handler is the
# transport itself, which authenticates per request and gates every tool call.
_MCP_TRANSPORT = frozenset({"/mcp{path:path}"})

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-z]+)?\}")


def _url(path: str) -> str:
    """Fill a route template with values its path parameters accept."""

    def fill(match: re.Match[str]) -> str:
        name, converter = match.group(1), match.group(2)
        if converter == ":path":
            return "/x"
        if name in ("key_id",) and "/share-keys/" in path:
            return "skey_" + "0" * 22
        if name == "key_id":
            return "akey_" + "0" * 22
        if name == "invitation_id":
            return "winv_" + "0" * 22
        if name == "token_id":
            return "rtok_" + "0" * 22
        if name.endswith("id"):
            return "00000000-0000-0000-0000-000000000001"
        return "x"

    return _PLACEHOLDER.sub(fill, path)


def _api_key_routes() -> set[tuple[str, str]]:
    from auth.dependencies import get_user_from_api_key_or_session, verify_api_key_user

    gated = (get_user_from_api_key_or_session, verify_api_key_user)
    return {
        (method, path)
        for method, path, route, extra in _iter_routes()
        if any(call in gated for call in route_dependency_calls(route, extra))
    }


@contextmanager
def _observed():
    """Count handler runs and capture the connector key scope events."""
    import fastapi.routing

    real = fastapi.routing.run_endpoint_function
    seen = SimpleNamespace(handlers=0, events=[])

    async def counting(*args, **kwargs):
        seen.handlers += 1
        return await real(*args, **kwargs)

    def record(event, **fields):
        seen.events.append((event, fields))

    with (
        patch.object(fastapi.routing, "run_endpoint_function", counting),
        patch("auth.connector_key_scope.logger") as logger,
    ):
        logger.warning.side_effect = record
        yield seen


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession, connector_world, monkeypatch):
    """A workspace on a plan with connector seats, its connector and keys."""
    from api.main import app

    owner = await connector_world.user("cks-owner")
    member = await connector_world.user("cks-member")
    ws = await connector_world.workspace(owner, **{member: WorkspaceRole.MEMBER})
    provisioned = await connector_world.connector(ws, by=owner)
    from auth.api_keys import APIKeyManager

    manager = APIKeyManager(db_session)
    ordinary, _ = await manager.create_key(name="cks-ordinary", user_id=owner, workspace_id=ws.id)
    agent = await connector_world.agent(ws, by=owner)
    agent_key, _ = await connector_world.agent_key(member, ws, agent)
    # A key minted for a connector before the link existed: the reserved name, no link.
    legacy, _ = await manager.create_key(
        name=f"connector:{uuid4()}", user_id=owner, workspace_id=ws.id
    )
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "connector_key_scope", "enforce")
    # The custom-domain routes answer 404 before authentication while the
    # feature is off; switch the routes' own gate on so they reach it.
    monkeypatch.setattr("api.routes.public_mcp_domains.public_domains_enabled", lambda: True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield SimpleNamespace(
            db=db_session,
            client=client,
            ws=ws,
            owner=owner,
            connector_key=provisioned.plaintext_kmc_api_key,
            connector_id=provisioned.connector.id,
            context_id=provisioned.context_id,
            resource_id=provisioned.resource_id,
            resource_token=provisioned.plaintext_token,
            ordinary=ordinary,
            agent_key=agent_key,
            legacy=legacy,
        )


def _bearer(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


async def test_every_api_key_route_refuses_a_connector_key(world) -> None:
    """The whole route table, one request per (method, route)."""
    api_key_routes = _api_key_routes()
    assert len(api_key_routes) > 100  # the inventory reaches the included routers
    wrong = []
    for method, path, _route, _extra in _iter_routes():
        if path in _MCP_TRANSPORT or (method, path) not in api_key_routes:
            continue
        with _observed() as seen:
            response = await world.client.request(
                method, _url(path), headers=_bearer(world.connector_key)
            )
        body = (
            response.json() if response.headers.get("content-type") == "application/json" else None
        )
        targets = [fields.get("target") for event, fields in seen.events]
        if (
            response.status_code != 403
            or body != REFUSAL
            or seen.handlers
            or [event for event, _ in seen.events] != ["connector_key_scope_refused"]
            or targets != [f"{method} {path}"]
        ):
            wrong.append((method, path, response.status_code, body, seen.handlers, targets))
    assert wrong == []


async def test_every_other_route_gives_a_connector_key_nothing_more_than_anonymous(world) -> None:
    api_key_routes = _api_key_routes()
    wrong = []
    for method, path, _route, _extra in _iter_routes():
        if path in _MCP_TRANSPORT or (method, path) in api_key_routes:
            continue
        url = _url(path)
        with _observed() as with_key:
            keyed = await world.client.request(method, url, headers=_bearer(world.connector_key))
        with _observed() as anonymous:
            plain = await world.client.request(method, url)
        if (
            with_key.events
            or (with_key.handlers and not anonymous.handlers)
            or (keyed.is_success and not plain.is_success)
        ):
            wrong.append((method, path, keyed.status_code, plain.status_code))
    assert wrong == []


async def test_a_key_with_the_reserved_name_is_refused_like_a_linked_one(world) -> None:
    with _observed() as seen:
        response = await world.client.get("/api/v1/resource-tokens", headers=_bearer(world.legacy))
    assert (response.status_code, response.json()) == (403, REFUSAL)
    assert seen.handlers == 0
    assert seen.events[0][1]["connector_id"] is None


async def test_log_mode_answers_as_the_route_does_and_logs_once(world, monkeypatch) -> None:
    """The same request with the connector key and with the owner's ordinary key."""
    monkeypatch.setattr(get_settings(), "connector_key_scope", "log")
    with _observed() as seen:
        keyed = await world.client.get(
            "/api/v1/resource-tokens", headers=_bearer(world.connector_key)
        )
    ordinary = await world.client.get("/api/v1/resource-tokens", headers=_bearer(world.ordinary))
    assert keyed.status_code == ordinary.status_code == 200
    assert keyed.json() == ordinary.json()
    assert seen.handlers == 1
    assert seen.events == [
        (
            "connector_key_scope_would_refuse",
            {
                "surface": "rest",
                "target": "GET /api/v1/resource-tokens",
                "key_prefix": world.connector_key[:16],
                "connector_id": str(world.connector_id),
            },
        )
    ]
    assert world.connector_key not in repr(seen.events)


@pytest.mark.parametrize("mode", ["enforce", "log"])
async def test_ordinary_and_agent_keys_are_not_affected(world, monkeypatch, mode) -> None:
    monkeypatch.setattr(get_settings(), "connector_key_scope", mode)
    with _observed() as seen:
        owner = await world.client.get("/api/v1/resource-tokens", headers=_bearer(world.ordinary))
        # The agent key's member is not the owner: the route's own role answer.
        agent = await world.client.get("/api/v1/resource-tokens", headers=_bearer(world.agent_key))
        member_route = await world.client.get(
            f"/api/v1/workspaces/{world.ws.id}/members", headers=_bearer(world.agent_key)
        )
    assert owner.status_code == 200
    assert agent.status_code == 403 and agent.json()["error"] == "AUTH-101"
    assert member_route.json().get("error") != "AUTH-204"
    assert seen.events == []


@pytest.mark.parametrize("mode", ["enforce", "log"])
async def test_a_paused_connector_key_is_still_401(world, monkeypatch, mode) -> None:
    monkeypatch.setattr(get_settings(), "connector_key_scope", mode)
    await world.db.execute(
        update(Workspace).where(Workspace.id == world.ws.id).values(plan_name="free")
    )
    await world.db.commit()
    try:
        with _observed() as seen:
            response = await world.client.get(
                "/api/v1/resource-tokens", headers=_bearer(world.connector_key)
            )
        assert response.status_code == 401
        assert seen.events == [] and seen.handlers == 0
    finally:
        await world.db.execute(
            update(Workspace).where(Workspace.id == world.ws.id).values(plan_name="promax")
        )
        await world.db.commit()


@pytest.mark.parametrize("mode", ["enforce", "log"])
async def test_a_key_linked_to_another_workspaces_connector_is_still_401(
    world, connector_world, monkeypatch, mode
) -> None:
    monkeypatch.setattr(get_settings(), "connector_key_scope", mode)
    other_owner = await connector_world.user("cks-other")
    other_ws = await connector_world.workspace(other_owner)
    other = await connector_world.connector(other_ws, by=other_owner)
    from auth.api_keys import APIKeyManager

    tampered, row = await APIKeyManager(world.db).create_key(
        name="cks-tampered", user_id=world.owner, workspace_id=world.ws.id
    )
    await world.db.execute(
        update(APIKey).where(APIKey.id == row.id).values(connector_id=other.connector.id)
    )
    await world.db.commit()
    with _observed() as seen:
        response = await world.client.get("/api/v1/resource-tokens", headers=_bearer(tampered))
    assert response.status_code == 401
    assert seen.events == [] and seen.handlers == 0


@pytest.mark.parametrize("mode", ["enforce", "log"])
async def test_resource_ingest_never_reads_the_bearer(world, monkeypatch, mode) -> None:
    """The worker's resource client sends its connector key as a bearer next to
    the resource token; the route authenticates by the resource token alone."""
    monkeypatch.setattr(get_settings(), "connector_key_scope", mode)
    url = f"/api/v1/resources/{world.resource_id}/events"
    token = {"X-Resource-API-Key": world.resource_token}
    answers = []
    for version, bearer in ((1, None), (2, world.connector_key)):
        event = {
            "op": "upsert",
            "doc_id": f"doc-{mode}",
            "version": version,
            "payload": {"t": "x"},
            # A connector's events carry its id as the idempotency prefix.
            "idempotency_key": f"{world.connector_id}:{mode}-{version}",
        }
        with _observed() as seen:
            response = await world.client.post(
                url, json=event, headers={**token, **_bearer(bearer)}
            )
        assert seen.events == []
        body = response.json()
        body.pop("event_id", None)
        answers.append((response.status_code, body))
    assert answers[0] == answers[1]
    assert answers[0][0] == 201


MODERN = "2026-07-28"


async def _mcp_call(world, key: str, name: str, arguments: dict):
    """One stateless (MCP 2026-07-28) ``tools/call`` through the whole application."""
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        **_bearer(key),
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/call",
        "Mcp-Name": name,
        "Accept": "application/json, text/event-stream",
    }
    return await world.client.post("/mcp", json=body, headers=headers)


async def _usage_rows(world) -> int:
    world.db.expire_all()
    return await world.db.scalar(
        select(func.count()).select_from(UsageStats).where(UsageStats.user_id == world.owner)
    )


@pytest.mark.parametrize("key", ["connector_key", "legacy"])
async def test_mcp_refuses_an_unlisted_tool_without_charging_or_recording_it(world, key) -> None:
    """``list`` records a usage row when it runs; ``get_state`` counts against
    the daily MCP call limit. Refused, neither does either."""
    import mcp_server.tools as tools_mod

    ws_id = world.ws.id
    ctx = str(world.context_id)
    tools_mod.invalidate_rate_limit_cache(ws_id)
    before = await _usage_rows(world)
    for name, arguments in (("list", {"context_id": ctx}), ("get_state", {"context_id": ctx})):
        response = await _mcp_call(world, getattr(world, key), name, arguments)
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True, name
        envelope = json.loads(result["content"][0]["text"])
        assert (envelope["error"], envelope["error_code"]) == ("permission_denied", "AUTH-204")
    assert ws_id not in tools_mod._RATE_LIMIT_CACHE
    assert await _usage_rows(world) == before


async def test_mcp_runs_a_listed_tool_for_a_connector_key(world) -> None:
    response = await _mcp_call(
        world, world.connector_key, "get_context_info", {"context_id": str(world.context_id)}
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert "isError" not in result, result
    assert str(world.context_id) in result["content"][0]["text"]
