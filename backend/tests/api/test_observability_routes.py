"""The observability API's gates, scope and envelopes (#2679).

Real app, real dependencies, test database. Route bodies are covered in
depth by the DB tests under ``tests/integration/test_observability_*``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from auth.api_keys import APIKeyManager
from config.settings import get_settings
from tests.observability_helpers import World, app_client, bearer, issue_credential, world_rows

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "/api/v1/observability"


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        yield w


@pytest.fixture
def api_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "observability_api_enabled", True)


def _get_routes(w: World) -> list[str]:
    slug = w.slugs["a1"]
    return [
        f"{BASE}/capabilities",
        f"{BASE}/resources",
        f"{BASE}/snapshots",
        f"{BASE}/snapshots?resource_id={slug}",
        f"{BASE}/ingestion-events?resource_id={slug}",
    ]


def observability_routes() -> list[tuple[str, str]]:
    """Every observability route the app registers."""
    from api.main import app

    paths = app.openapi()["paths"]
    return sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith(BASE)
        for method in operations
    )


async def test_switch_off_is_404_everywhere_even_with_a_valid_credential(
    world: World, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "observability_api_enabled", False)
    _, secret = await issue_credential(world)
    routes = observability_routes()
    assert len(routes) >= 4
    async with app_client(world) as c:
        unknown = await c.get(f"{BASE}/no-such-route")
        for method, path in routes:
            for headers in (bearer(secret), {}):
                r = await c.request(method, path, headers=headers, json={})
                assert r.status_code == 404, (method, path)
                assert r.json() == unknown.json()
        # Query validation does not run before the switch either.
        r = await c.get(f"{BASE}/ingestion-events?limit=501", headers=bearer(secret))
        assert r.status_code == 404


async def test_no_bearer_and_owner_credentials_are_401(world: World, api_on) -> None:
    async with world.factory() as s:
        owner_key, _ = await APIKeyManager(s).create_key(
            name=f"owner-{world.tag}", user_id=world.owner, workspace_id=world.ws
        )
        await s.commit()
    async with app_client(world) as c:
        for path in _get_routes(world):
            assert (await c.get(path)).status_code == 401, path
            assert (await c.get(path, headers=bearer(owner_key))).status_code == 401, path
            r = await c.get(path, cookies={"kagura_session": "owner-session-token"})
            assert r.status_code == 401, path


async def test_envelopes_carry_version_deployment_and_as_of(world: World, api_on) -> None:
    _, secret = await issue_credential(world)
    async with app_client(world) as c:
        for path in _get_routes(world):
            r = await c.get(path, headers=bearer(secret))
            assert r.status_code == 200, (path, r.text)
            body = r.json()
            assert body["schema_version"] == 1
            assert body["deployment_id"]
            assert body["as_of"].endswith("Z")


async def test_unsupported_schema_version(world: World, api_on) -> None:
    _, secret = await issue_credential(world)
    async with app_client(world) as c:
        for path in _get_routes(world):
            sep = "&" if "?" in path else "?"
            r = await c.get(f"{path}{sep}schema_version=2", headers=bearer(secret))
            assert r.status_code == 400, path
            body = r.json()
            assert body["error"] == "unsupported_schema_version"
            assert body["details"]["supported"] == [1]


async def test_limits_and_cursor_errors(world: World, api_on) -> None:
    _, secret = await issue_credential(world)
    slug = world.slugs["a1"]
    async with app_client(world) as c:
        for limit in (0, 501):
            r = await c.get(
                f"{BASE}/ingestion-events?resource_id={slug}&limit={limit}", headers=bearer(secret)
            )
            assert r.status_code == 422, limit
        for cursor in ("not-a-cursor", "eyJ2IjoyLCJpIjoxfQ", "x" * 300):
            r = await c.get(
                f"{BASE}/ingestion-events?resource_id={slug}&cursor={cursor}",
                headers=bearer(secret),
            )
            assert r.status_code == 400, cursor
            assert r.json()["error"] == "cursor_invalid"
        # A workspace credential must name the resource of a per-resource read.
        r = await c.get(f"{BASE}/ingestion-events", headers=bearer(secret))
        assert r.status_code == 422


async def test_capabilities(world: World, api_on) -> None:
    public_id, secret = await issue_credential(world, resource="a2")
    async with app_client(world) as c:
        r = await c.get(f"{BASE}/capabilities", headers=bearer(secret))
    body = r.json()
    assert body["scope"] == {
        "kind": "resource",
        "workspace_id": str(world.ws),
        "resource_id": world.slugs["a2"],
    }
    assert body["credential"]["id"] == public_id
    assert body["limits"] == {
        "events_page_max": 500,
        "ingestion_page_max": 500,
        "documents_resolve_max": 100,
        "resources_max": 500,
    }
    assert body["retention"]["ingestion_events"] == "resource lifetime"
    assert body["ingestion_seal_seconds"] == get_settings().observability_ingestion_seal_seconds
    assert set(body["schema_versions"]) == {
        "resources",
        "snapshots",
        "events",
        "ingestion_events",
        "documents",
    }


async def test_scope_escape_is_one_404(world: World, api_on) -> None:
    _, workspace_secret = await issue_credential(world)
    _, resource_secret = await issue_credential(world, resource="a1")
    _, other_secret = await issue_credential(world, workspace="b")
    cases = [
        # A credential of workspace A on workspace B's resource.
        (workspace_secret, world.slugs["b1"]),
        # Workspace B's credential on workspace A's resource.
        (other_secret, world.slugs["a1"]),
        # A resource-scoped credential on a sibling resource of its workspace.
        (resource_secret, world.slugs["a2"]),
        # No such resource at all.
        (workspace_secret, f"missing-{world.tag}"),
    ]
    bodies = []
    async with app_client(world) as c:
        for secret, slug in cases:
            for path in (
                f"{BASE}/snapshots?resource_id={slug}",
                f"{BASE}/ingestion-events?resource_id={slug}",
            ):
                r = await c.get(path, headers=bearer(secret))
                assert r.status_code == 404, (slug, path)
                bodies.append(r.json())
        # Lists only show what is in scope.
        listed = (await c.get(f"{BASE}/resources", headers=bearer(resource_secret))).json()
        assert [x["resource_id"] for x in listed["resources"]] == [world.slugs["a1"]]
        listed = (await c.get(f"{BASE}/resources", headers=bearer(workspace_secret))).json()
        assert {x["resource_id"] for x in listed["resources"]} == {
            world.slugs["a1"],
            world.slugs["a2"],
        }
        # A resource-scoped credential may omit resource_id.
        r = await c.get(f"{BASE}/ingestion-events", headers=bearer(resource_secret))
        assert r.status_code == 200 and r.json()["resource_id"] == world.slugs["a1"]
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0]["error"] == "resource_not_found"


async def test_cidr_allow_list(world: World, api_on, monkeypatch) -> None:
    _, secret = await issue_credential(world)
    monkeypatch.setattr(get_settings(), "observability_api_allowed_cidrs", "10.0.0.0/8")
    async with app_client(world, client_addr=("10.0.0.5", 1234)) as c:
        assert (await c.get(f"{BASE}/resources", headers=bearer(secret))).status_code == 200
    async with app_client(world, client_addr=("192.0.2.9", 1234)) as c:
        r = await c.get(f"{BASE}/resources", headers=bearer(secret))
        assert r.status_code == 404
        assert r.json()["error"] == "HTTP-404"


async def test_docs_name_every_route() -> None:
    from pathlib import Path

    doc = Path(__file__).resolve().parents[3] / "docs" / "observability-api.md"
    if not doc.exists():
        pytest.skip("docs/observability-api.md not written yet")
    text = doc.read_text(encoding="utf-8")
    for method, path in observability_routes():
        assert f"{method} {path}" in text, (method, path)
