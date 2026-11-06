"""Owner routes that issue, list, rotate and revoke observation credentials (#2679)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from auth.observation_credentials import ObservationCredentialManager
from config.settings import get_settings
from models.observation_credential import ObservationCredential
from tests.observability_helpers import World, owner_client, world_rows
from utils.hashing import sha256_hex
from utils.public_id import PublicIdPrefix, public_id_pattern

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "/api/v1/observation-credentials"


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        yield w


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)


async def test_anonymous_is_401_and_member_is_403(world: World) -> None:
    some_id = "obsk_" + "0" * 22
    calls = [
        ("POST", BASE, {"name": "d"}),
        ("GET", BASE, None),
        ("POST", f"{BASE}/{some_id}/rotate", {}),
        ("DELETE", f"{BASE}/{some_id}", None),
    ]
    async with owner_client(world, None) as c:
        for method, path, body in calls:
            assert (await c.request(method, path, json=body)).status_code == 401, path
    async with owner_client(world, world.member) as c:
        for method, path, body in calls:
            assert (await c.request(method, path, json=body)).status_code == 403, path
    async with world.factory() as s:
        rows = (
            await s.execute(
                select(ObservationCredential).where(ObservationCredential.workspace_id == world.ws)
            )
        ).all()
        assert rows == []


async def test_owner_lifecycle(world: World, monkeypatch) -> None:
    # Management works while the API switch is off.
    monkeypatch.setattr(get_settings(), "observability_api_enabled", False)
    async with owner_client(world, world.owner) as c:
        r = await c.post(BASE, json={"name": "dashboard"})
        assert r.status_code == 201, r.text
        created = r.json()
        secret = created["secret"]
        assert secret.startswith("kagura_obs_")
        assert re.fullmatch(public_id_pattern(PublicIdPrefix.OBSERVATION_KEY), created["id"])
        assert created["scope"] == "workspace" and created["resource_id"] is None
        assert created["status"] == "active"
        assert created["created_at"].endswith("Z") and created["expires_at"].endswith("Z")
        lifetime = _parse(created["expires_at"]) - _parse(created["created_at"])
        assert abs(lifetime - timedelta(days=90)) < timedelta(minutes=1)

        async with world.factory() as s:
            row = (
                await s.execute(
                    select(ObservationCredential).where(
                        ObservationCredential.public_id == created["id"]
                    )
                )
            ).scalar_one()
            assert row.key_hash == sha256_hex(secret)

        r = await c.get(BASE)
        assert r.status_code == 200
        listed = r.json()
        assert [x["id"] for x in listed] == [created["id"]]
        assert "secret" not in listed[0]
        assert secret not in r.text

        r = await c.post(f"{BASE}/{created['id']}/rotate", json={"grace_seconds": 3600})
        assert r.status_code == 201, r.text
        rotated = r.json()
        assert rotated["id"] != created["id"]
        assert rotated["rotated_from"] == created["id"]
        assert rotated["secret"] != secret
        assert rotated["expires_at"] == created["expires_at"]

        r = await c.delete(f"{BASE}/{rotated['id']}")
        assert r.status_code == 204
        assert (await c.delete(f"{BASE}/{rotated['id']}")).status_code == 204

        statuses = {x["id"]: x["status"] for x in (await c.get(BASE)).json()}
        assert statuses[rotated["id"]] == "revoked"
        assert statuses[created["id"]] == "active"  # inside its grace hour

    # A revoked credential no longer verifies.
    async with world.factory() as s:
        assert await ObservationCredentialManager(s).verify(rotated["secret"]) is None


async def test_resource_scope(world: World) -> None:
    async with owner_client(world, world.owner) as c:
        r = await c.post(
            BASE,
            json={
                "name": "one",
                "scope": "resource",
                "resource_id": world.slugs["a1"],
                "ttl_days": 7,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["scope"] == "resource" and body["resource_id"] == world.slugs["a1"]
        # Another workspace's slug and an unknown slug answer the same 404.
        for slug in (world.slugs["b1"], f"nope-{world.tag}"):
            r = await c.post(BASE, json={"name": "x", "scope": "resource", "resource_id": slug})
            assert r.status_code == 404, slug
        # scope and resource_id must agree.
        for bad in (
            {"name": "x", "scope": "resource"},
            {"name": "x", "scope": "workspace", "resource_id": world.slugs["a1"]},
            {"name": "x", "scope": "deployment"},
        ):
            assert (await c.post(BASE, json=bad)).status_code == 422, bad


@pytest.mark.parametrize("ttl", [0, 366])
async def test_ttl_bounds(world: World, ttl: int) -> None:
    async with owner_client(world, world.owner) as c:
        r = await c.post(BASE, json={"name": "x", "ttl_days": ttl})
    assert r.status_code == 422


async def test_rotate_grace_bounds_and_foreign_ids(world: World) -> None:
    async with owner_client(world, world.other_owner, workspace_id=world.other_ws) as c:
        foreign = (await c.post(BASE, json={"name": "b"})).json()["id"]
    async with owner_client(world, world.owner) as c:
        mine = (await c.post(BASE, json={"name": "a"})).json()["id"]
        r = await c.post(f"{BASE}/{mine}/rotate", json={"grace_seconds": 86401})
        assert r.status_code == 422
        assert (await c.post(f"{BASE}/{foreign}/rotate", json={})).status_code == 404
        assert (await c.delete(f"{BASE}/{foreign}")).status_code == 404
        assert foreign not in (await c.get(BASE)).text
        assert (await c.delete(f"{BASE}/not-an-id")).status_code == 422
        # Rotating without a body uses grace 0.
        r = await c.post(f"{BASE}/{mine}/rotate")
        assert r.status_code == 201
        statuses = {x["id"]: x["status"] for x in (await c.get(BASE)).json()}
        assert statuses[mine] == "expired"
