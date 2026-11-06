"""Observation credential manager and principal (#2679).

The manager runs on real rows (test database). The dependency runs through
the real app on an observability-shaped probe route, so the gate order —
switch, address allow-list, rate limit, bearer — is exercised as deployed.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy import select

from auth.observation_credentials import (
    INVALID_CREDENTIAL_MESSAGE,
    OBSERVATION_KEY_PREFIX,
    ObservationCredentialManager,
    ObservationScope,
    clamp_ttl_days,
    get_observation_principal,
)
from config.settings import get_settings
from models.observation_credential import ObservationCredential
from tests.observability_helpers import World, bearer, issue_credential, world_rows
from utils.datetime import utcnow
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        yield w


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def test_ttl_clamps() -> None:
    assert clamp_ttl_days(None) == 90
    assert clamp_ttl_days(0) == 1
    assert clamp_ttl_days(-5) == 1
    assert clamp_ttl_days(30) == 30
    assert clamp_ttl_days(365) == 365
    assert clamp_ttl_days(1000) == 365


async def test_create_stores_only_the_verifier(world: World) -> None:
    now = utcnow()
    async with world.factory() as s:
        row, secret = await ObservationCredentialManager(s).create(
            world.ws, name="dash", resource_pk=None, ttl_days=None, created_by=world.owner, now=now
        )
        await s.commit()
    assert secret.startswith(OBSERVATION_KEY_PREFIX)
    assert len(secret) > len(OBSERVATION_KEY_PREFIX) + 40
    assert row.key_hash == sha256_hex(secret)
    assert row.key_prefix == secret[:16]
    assert row.public_id.startswith("obsk_")
    assert row.scope == "workspace" and row.resource_pk is None
    assert row.expires_at == now + timedelta(days=90)
    # No column holds the plaintext.
    async with world.factory() as s:
        stored = await s.get(ObservationCredential, row.id)
        assert stored is not None
        values = [getattr(stored, c.key) for c in ObservationCredential.__table__.columns]
        assert secret not in [str(v) for v in values]


async def test_resource_scope_and_ttl(world: World) -> None:
    now = utcnow()
    async with world.factory() as s:
        row, _ = await ObservationCredentialManager(s).create(
            world.ws,
            name="dash",
            resource_pk=world.resource_pk["a1"],
            ttl_days=7,
            created_by=world.owner,
            now=now,
        )
        await s.commit()
    assert row.scope == "resource" and row.resource_pk == world.resource_pk["a1"]
    assert row.expires_at == now + timedelta(days=7)


async def test_verify_accepts_a_live_credential(world: World) -> None:
    public_id, secret = await issue_credential(world, resource="a1")
    async with world.factory() as s:
        scope = await ObservationCredentialManager(s).verify(secret)
    assert isinstance(scope, ObservationScope)
    assert scope.public_id == public_id
    assert scope.workspace_id == world.ws
    assert scope.resource_pk == world.resource_pk["a1"]
    assert scope.kind == "resource"


async def test_verify_rejects_unknown_revoked_expired_and_foreign_prefix(world: World) -> None:
    public_id, secret = await issue_credential(world, ttl_days=1)
    async with world.factory() as s:
        manager = ObservationCredentialManager(s)
        assert await manager.verify(OBSERVATION_KEY_PREFIX + "x" * 43) is None
        assert await manager.verify("kagura_sk_" + secret[len(OBSERVATION_KEY_PREFIX) :]) is None
        assert await manager.verify(secret[len(OBSERVATION_KEY_PREFIX) :]) is None
        # Expired: a clock past the expiry.
        assert await manager.verify(secret, now=utcnow() + timedelta(days=2)) is None
        assert await manager.verify(secret) is not None
        assert await manager.revoke(public_id, world.ws) is True
        await s.commit()
        assert await manager.verify(secret) is None


async def test_last_used_is_touched_at_most_once_a_minute(world: World) -> None:
    public_id, secret = await issue_credential(world)
    t0 = utcnow()
    async with world.factory() as s:
        manager = ObservationCredentialManager(s)
        await manager.verify(secret, now=t0)
        await manager.verify(secret, now=t0 + timedelta(seconds=30))
        row = (
            await s.execute(
                select(ObservationCredential).where(ObservationCredential.public_id == public_id)
            )
        ).scalar_one()
        assert row.last_used_at == t0
        await manager.verify(secret, now=t0 + timedelta(seconds=61))
        assert row.last_used_at == t0 + timedelta(seconds=61)


@pytest.mark.parametrize("grace", [0, 3600])
async def test_rotate_keeps_binding_and_remaining_lifetime(world: World, grace: int) -> None:
    old_public_id, old_secret = await issue_credential(world, resource="a2", ttl_days=30)
    now = utcnow()
    async with world.factory() as s:
        manager = ObservationCredentialManager(s)
        rotated = await manager.rotate(
            old_public_id, world.ws, grace_seconds=grace, created_by=world.owner, now=now
        )
        assert rotated is not None
        new, new_secret = rotated
        await s.commit()
        old = (
            await s.execute(
                select(ObservationCredential).where(
                    ObservationCredential.public_id == old_public_id
                )
            )
        ).scalar_one()
        assert new.public_id != old_public_id
        assert new.rotated_from_id == old.id
        assert new.resource_pk == world.resource_pk["a2"] and new.scope == "resource"
        assert new.key_hash == sha256_hex(new_secret)
        assert new_secret != old_secret
        assert old.expires_at == now + timedelta(seconds=grace)
        # The new credential inherits the remaining lifetime, not a fresh one.
        assert abs((new.expires_at - (now + timedelta(days=30))).total_seconds()) < 60
        assert await manager.verify(new_secret, now=now + timedelta(seconds=1)) is not None
        old_ok = await manager.verify(old_secret, now=now + timedelta(seconds=1))
        assert (old_ok is not None) == (grace > 1)


async def test_rotate_refuses_unknown_foreign_and_revoked(world: World) -> None:
    public_id, _ = await issue_credential(world)
    other_public_id, _ = await issue_credential(world, workspace="b")
    async with world.factory() as s:
        manager = ObservationCredentialManager(s)
        assert await manager.rotate(other_public_id, world.ws, created_by=world.owner) is None
        assert await manager.rotate("obsk_" + "0" * 22, world.ws, created_by=world.owner) is None
        assert await manager.revoke(public_id, world.ws)
        assert await manager.rotate(public_id, world.ws, created_by=world.owner) is None


async def test_revoke_is_idempotent_and_workspace_scoped(world: World) -> None:
    public_id, _ = await issue_credential(world)
    async with world.factory() as s:
        manager = ObservationCredentialManager(s)
        assert await manager.revoke(public_id, world.other_ws) is False
        assert await manager.revoke(public_id, world.ws) is True
        await s.commit()
        first = (
            await s.execute(
                select(ObservationCredential.revoked_at).where(
                    ObservationCredential.public_id == public_id
                )
            )
        ).scalar_one()
        assert await manager.revoke(public_id, world.ws) is True
        second = (
            await s.execute(
                select(ObservationCredential.revoked_at).where(
                    ObservationCredential.public_id == public_id
                )
            )
        ).scalar_one()
        assert first is not None and first == second
        listed = {r.public_id for r in await manager.list_for_workspace(world.ws)}
        assert public_id in listed
        assert all(
            r.workspace_id == world.other_ws
            for r in await manager.list_for_workspace(world.other_ws)
        )


# ---------------------------------------------------------------------------
# Principal (gate order)
# ---------------------------------------------------------------------------


def _probe_app(w: World) -> FastAPI:
    from api.main import memory_cloud_exception_handler
    from db.base import get_db
    from tests.observability_helpers import _db_override
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_db] = _db_override(w)

    @app.get("/probe")
    async def probe(scope: ObservationScope = Depends(get_observation_principal)) -> dict:
        return {"public_id": scope.public_id, "kind": scope.kind}

    return app


async def _get(w: World, headers: dict[str, str] | None = None, client=("127.0.0.1", 1)):
    import httpx

    transport = httpx.ASGITransport(app=_probe_app(w), client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get("/probe", headers=headers or {})


@pytest.fixture
def api_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "observability_api_enabled", True)


async def test_switch_off_is_404_before_the_header_is_read(world: World, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "observability_api_enabled", False)
    _, secret = await issue_credential(world)
    with patch(
        "auth.observation_credentials.ObservationCredentialManager.verify",
        new=AsyncMock(side_effect=AssertionError("verified while off")),
    ):
        for headers in (bearer(secret), {}, {"Authorization": "Basic x"}):
            r = await _get(world, headers)
            assert r.status_code == 404
            assert r.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}


async def test_valid_credential_is_served(world: World, api_on) -> None:
    public_id, secret = await issue_credential(world, resource="a1")
    r = await _get(world, bearer(secret))
    assert r.status_code == 200, r.text
    assert r.json() == {"public_id": public_id, "kind": "resource"}


async def test_uniform_401(world: World, api_on) -> None:
    public_id, secret = await issue_credential(world)
    async with world.factory() as s:
        await ObservationCredentialManager(s).revoke(public_id, world.ws)
        await s.commit()
    _, live = await issue_credential(world)
    cases = [
        {},
        {"Authorization": secret},
        {"Authorization": f"Basic {live}"},
        bearer("kagura_" + "x" * 43),
        bearer(OBSERVATION_KEY_PREFIX + "unknown"),
        bearer(secret),  # revoked
    ]
    bodies = set()
    for headers in cases:
        r = await _get(world, headers)
        assert r.status_code == 401, headers
        body = r.json()
        assert body["message"] == INVALID_CREDENTIAL_MESSAGE
        bodies.add((body["error"], body["message"]))
    assert len(bodies) == 1


async def test_expired_credential_is_401(world: World, api_on) -> None:
    public_id, secret = await issue_credential(world, ttl_days=1)
    async with world.factory() as s:
        row = (
            await s.execute(
                select(ObservationCredential).where(ObservationCredential.public_id == public_id)
            )
        ).scalar_one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    r = await _get(world, bearer(secret))
    assert r.status_code == 401


async def test_cidr_allow_list(world: World, api_on, monkeypatch) -> None:
    _, secret = await issue_credential(world)
    monkeypatch.setattr(get_settings(), "observability_api_allowed_cidrs", "10.0.0.0/8")
    inside = await _get(world, bearer(secret), client=("10.0.0.5", 1234))
    assert inside.status_code == 200
    outside = await _get(world, bearer(secret), client=("192.0.2.7", 1234))
    assert outside.status_code == 404
    assert outside.json()["error"] == "HTTP-404"
    # Outside with no credential at all: the same 404, no 401 oracle.
    assert (await _get(world, {}, client=("192.0.2.7", 1234))).status_code == 404


def test_cidr_setting_is_validated() -> None:
    from config.settings import Settings

    with pytest.raises(ValueError):
        Settings(observability_api_allowed_cidrs="10.0.0.5/8")
    assert Settings(
        observability_api_allowed_cidrs="10.0.0.0/8, ::1/128"
    ).observability_api_allowed_networks


async def test_rate_limit(world: World, api_on, monkeypatch) -> None:
    _, secret = await issue_credential(world)
    monkeypatch.setattr(get_settings(), "observability_api_rate_limit_per_minute", 2)
    counter = AsyncMock(side_effect=[1, 2, 3])
    with patch("auth.observation_credentials.incrby_counter", new=counter):
        assert (await _get(world, bearer(secret))).status_code == 200
        assert (await _get(world, bearer(secret))).status_code == 200
        r = await _get(world, bearer(secret))
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"
    key = counter.await_args_list[0].args[0]
    assert secret not in key


async def test_rate_limit_fails_open(world: World, api_on) -> None:
    _, secret = await issue_credential(world)
    with patch(
        "auth.observation_credentials.incrby_counter",
        new=AsyncMock(side_effect=RuntimeError("redis down")),
    ):
        assert (await _get(world, bearer(secret))).status_code == 200
