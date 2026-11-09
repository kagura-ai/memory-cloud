"""An observation credential is honoured by the observability router only (#2679).

Every other surface authenticates against ``api_keys`` / sessions / OAuth /
resource tokens, where a ``kagura_obs_`` secret does not exist, so it answers
401: ingestion (header and bearer), a memory write, owner routes, the
credential management routes themselves, and the authenticated MCP endpoint.
The observability router's own module never imports a session or API-key
dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import pytest_asyncio

from config.settings import get_settings
from tests.observability_helpers import World, app_client, bearer, issue_credential, world_rows

pytestmark = pytest.mark.asyncio(loop_scope="session")

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        yield w


@pytest.fixture(autouse=True)
def api_on(monkeypatch):
    # The switch on: the rejection below is the credential's, not the switch's.
    monkeypatch.setattr(get_settings(), "observability_api_enabled", True)


async def test_rejected_everywhere_else(world: World, monkeypatch) -> None:
    import db.base
    from tests.observability_helpers import _db_override

    # Code that opens its own session through ``db.base.get_db`` stays on the
    # test database too.
    monkeypatch.setattr(db.base, "get_db", _db_override(world))
    _, secret = await issue_credential(world)
    slug = world.slugs["a1"]
    event = {"op": "upsert", "doc_id": "d1", "version": 1, "payload": {"t": "x"}}
    cases = [
        ("POST", f"/api/v1/resources/{slug}/events", {"X-Resource-API-Key": secret}, event),
        ("POST", f"/api/v1/resources/{slug}/events", bearer(secret), event),
        (
            "POST",
            "/api/v1/memory/remember",
            bearer(secret),
            {"summary": "s", "content": "c", "context_id": str(world.ctx["a1"])},
        ),
        ("GET", "/api/v1/resources", bearer(secret), None),
        ("GET", f"/api/v1/resources/{slug}/indexer-status", bearer(secret), None),
        ("GET", f"/api/v1/resources/{slug}/events", bearer(secret), None),
        ("GET", "/api/v1/observation-credentials", bearer(secret), None),
        (
            "POST",
            "/api/v1/observation-credentials",
            bearer(secret),
            {"name": "escalate", "scope": "workspace"},
        ),
        ("GET", "/api/v1/resource-tokens", bearer(secret), None),
    ]
    async with app_client(world) as c:
        for method, path, headers, body in cases:
            r = await c.request(method, path, headers=headers, json=body)
            assert r.status_code == 401, (method, path, r.status_code, r.text)


async def test_rejected_by_the_authenticated_mcp_endpoint(world: World, monkeypatch) -> None:
    import db.base
    from tests.observability_helpers import _db_override

    # The MCP auth path opens its own sessions through ``db.base.get_db``
    # (imported at call time); point it at the test database.
    monkeypatch.setattr(db.base, "get_db", _db_override(world))
    _, secret = await issue_credential(world)
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with app_client(world) as c:
        r = await c.post(
            "/mcp",
            headers={
                **bearer(secret),
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json=rpc,
        )
    assert r.status_code == 401, r.text


def test_observability_router_imports_no_session_or_api_key_dependency() -> None:
    path = SRC / "api" / "routes" / "observability.py"
    if not path.exists():
        pytest.skip("observability router not written yet")
    banned = {
        "APIKeyOrSessionUser",
        "get_current_user",
        "get_current_user_optional",
        "get_user_from_api_key_or_session",
        "verify_api_key_user",
        "WorkspaceOwner",
        "require_workspace_owner",
        "CurrentUser",
        "SessionUser",
        "APIKeyUser",
        "get_share_key_principal",
    }
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert not (names & banned), names & banned


def test_only_the_observability_router_uses_the_principal() -> None:
    users = sorted(
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "get_observation_principal" in p.read_text(encoding="utf-8")
    )
    allowed = {"auth/observation_credentials.py", "api/routes/observability.py"}
    assert set(users) <= allowed, users
