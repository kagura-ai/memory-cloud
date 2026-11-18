"""Real PostgreSQL recognition behind the per-client OIDC budget (#2903).

Over the per-peer budget the limiter recognises a presented credential with
one plain read on a session of its own. These tests pin that the read finds
the credential's client and user for every kind, never waits on (or takes) the row
locks the protocol holds, leaves nothing open and changes nothing it reads,
and that real routes admit a recognised caller while an unknown one keeps the
429.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes import sync_worker
from auth import oidc_limits
from auth.oidc_limits import PresentedCredential
from auth.oidc_server import credential_hash
from models.auth import AuditLog, User
from models.oidc import OIDCAuthorizationCode, OIDCClient, OIDCGrant, OIDCToken
from models.sync_credential import SyncAccessToken
from tests.integration import sync_credential_fixtures as credentials
from tests.integration import sync_installation_fixtures as installations
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = installations.store
worker = credentials.worker
LIMIT = 120
PREFIXES = {
    "authorization_code": "kmc_oidc_code_",
    "access_token": "kmc_oidc_at_",
    "refresh_token": "kmc_oidc_rt_",
}


@pytest_asyncio.fixture(loop_scope="session")
async def issued(async_engine, monkeypatch):
    """One live grant with a code and a token pair; the limiter reads this database."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    prefix = "oidc-limits-" + uuid4().hex
    user_id, client_id = prefix + "-user", prefix + "-client"
    values = {kind: prefix + secrets.token_urlsafe(32) for kind, prefix in PREFIXES.items()}
    async with factory() as db:
        db.add(User(user_id=user_id, email=user_id + "@example.test", role="user"))
        db.add(
            OIDCClient(
                client_id=client_id,
                client_name="Limiter recognition fixture",
                client_secret_hash="not-used",
                redirect_uris=["https://client.example.test/callback"],
                scope="openid",
            )
        )
        await db.flush()
        grant = OIDCGrant(
            client_id=client_id,
            user_id=user_id,
            scope="openid",
            nonce=uuid4().hex,
            auth_time=int(time.time()),
            expires_at=utcnow() + timedelta(hours=8),
        )
        db.add(grant)
        await db.flush()
        db.add(
            OIDCAuthorizationCode(
                code_hash=credential_hash(values["authorization_code"]),
                grant_id=grant.id,
                redirect_uri="https://client.example.test/callback",
                code_challenge="A" * 43,
                code_challenge_method="S256",
                expires_at=utcnow() + timedelta(seconds=60),
            )
        )
        db.add(
            OIDCToken(
                grant_id=grant.id,
                access_token_hash=credential_hash(values["access_token"]),
                refresh_token_hash=credential_hash(values["refresh_token"]),
                scope="openid",
                issued_at=int(time.time()),
                expires_in=300,
            )
        )
        await db.commit()
        grant_id = grant.id
    monkeypatch.setattr(oidc_limits, "_get_session_factory", lambda: factory)
    try:
        yield SimpleNamespace(
            factory=factory,
            user_id=user_id,
            client_id=client_id,
            holder=f"oidc-client:{client_id}:user:{user_id}",
            grant_id=grant_id,
            values=values,
        )
    finally:
        async with factory() as db:
            await db.execute(delete(OIDCClient).where(OIDCClient.client_id == client_id))
            await db.execute(delete(User).where(User.user_id == user_id))
            await db.commit()


async def recognise(kind, value):
    return await asyncio.wait_for(oidc_limits._holder(PresentedCredential(kind, value)), 5)


async def test_each_issued_credential_is_recognised_as_its_client_and_user(issued):
    for kind, value in issued.values.items():
        assert await recognise(kind, value) == issued.holder
    for kind, prefix in PREFIXES.items():
        assert await recognise(kind, prefix + secrets.token_urlsafe(32)) is None
    # A credential presented as another kind does not match that kind's shape.
    assert await recognise("refresh_token", issued.values["access_token"]) is None


async def test_spent_expired_or_revoked_credentials_are_still_recognised(issued):
    """Existence only: the protocol, not the limiter, refuses these afterwards."""
    async with issued.factory() as db:
        code = await db.get(
            OIDCAuthorizationCode, credential_hash(issued.values["authorization_code"])
        )
        code.consumed_at = utcnow()
        code.expires_at = utcnow() - timedelta(seconds=1)
        token = await db.scalar(select(OIDCToken).where(OIDCToken.grant_id == issued.grant_id))
        token.refresh_used_at = utcnow()
        grant = await db.get(OIDCGrant, issued.grant_id)
        grant.revoked_at = utcnow()
        await db.commit()
    for kind, value in issued.values.items():
        assert await recognise(kind, value) == issued.holder


async def test_recognition_neither_waits_on_nor_takes_the_protocol_row_locks(issued):
    """Another transaction holds FOR UPDATE on every row the read touches."""
    async with issued.factory() as holder:
        locker_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        for statement in (
            select(User.id).where(User.user_id == issued.user_id),
            select(OIDCClient.client_id).where(OIDCClient.client_id == issued.client_id),
            select(OIDCGrant.id).where(OIDCGrant.id == issued.grant_id),
            select(OIDCAuthorizationCode.code_hash).where(
                OIDCAuthorizationCode.grant_id == issued.grant_id
            ),
            select(OIDCToken.id).where(OIDCToken.grant_id == issued.grant_id),
        ):
            assert (await holder.execute(statement.with_for_update())).first() is not None
        for kind, value in issued.values.items():
            assert await recognise(kind, value) == issued.holder
        open_elsewhere = await holder.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> :pid AND state LIKE 'idle in transaction%'"
            ),
            {"pid": locker_pid},
        )
        assert open_elsewhere == 0
        await holder.rollback()


async def test_recognition_changes_nothing_it_reads(issued):
    async with issued.factory() as db:
        audits = await db.scalar(select(func.count()).select_from(AuditLog))
    for _ in range(3):
        for kind, value in issued.values.items():
            await recognise(kind, value)
    async with issued.factory() as db:
        code = await db.get(
            OIDCAuthorizationCode, credential_hash(issued.values["authorization_code"])
        )
        token = await db.scalar(select(OIDCToken).where(OIDCToken.grant_id == issued.grant_id))
        grant = await db.get(OIDCGrant, issued.grant_id)
        assert (code.consumed_at, token.refresh_used_at, token.revoked_at, grant.revoked_at) == (
            None,
            None,
            None,
            None,
        )
        assert await db.scalar(select(func.count()).select_from(AuditLog)) == audits


class Counter:
    """The Redis counter with every peer bucket already over the limit."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def __call__(self, key: str, ttl: int) -> int:
        self.keys.append(key)
        if ":holder:" in key:
            return self.keys.count(key)
        return LIMIT + 1

    def holder(self, operation: str, holder: str) -> int:
        digest = hashlib.sha256(holder.encode()).hexdigest()
        return sum(
            key.startswith(f"oidc_rate_limit:{operation}:holder:{digest}:") for key in self.keys
        )


def exhaust_peer(monkeypatch, factory) -> Counter:
    counter = Counter()
    monkeypatch.setattr(oidc_limits, "increment_counter", counter)
    monkeypatch.setattr(oidc_limits, "_get_session_factory", lambda: factory)
    return counter


async def test_over_budget_owner_still_records_a_sync_authorization_counted_per_client(
    store, monkeypatch
):
    """``POST .../sync-authorizations`` is counted (it was not), per client over budget."""
    counter = exhaust_peer(monkeypatch, store.factory)
    response = await installations.http(
        store, "POST", "sync-authorizations", body=installations.approval_body()
    )
    assert response.status_code == 201, response.text
    assert (
        counter.holder("sync-human", f"oidc-client:{store.client_id}:user:{store.users['owner']}")
        == 1
    )
    store.tokens["owner"] = "kmc_oidc_at_" + secrets.token_urlsafe(32)
    refused = await installations.http(
        store, "POST", "sync-authorizations", body=installations.approval_body()
    )
    assert refused.status_code == 429
    assert (
        counter.holder("sync-human", f"oidc-client:{store.client_id}:user:{store.users['owner']}")
        == 1
    )


async def test_over_budget_worker_still_reads_its_installation_counted_per_sync_client(
    worker, monkeypatch
):
    worker.store.app.include_router(sync_worker.router, prefix="/api/v1")
    token = (await credentials.mint(worker))["access_token"]
    counter = exhaust_peer(monkeypatch, worker.factory)
    assert await recognise("sync_token", token) == "sync-client:" + worker.client_id
    path = "/api/v1/sync/installations/" + worker.target["installation_id"]
    async with AsyncClient(
        transport=ASGITransport(app=worker.store.app), base_url="https://test"
    ) as client:
        admitted = await client.get(path, headers={"Authorization": "Bearer " + token})
        unknown = await client.get(
            path, headers={"Authorization": "Bearer kmc_sync_at_" + secrets.token_urlsafe(32)}
        )
    assert admitted.status_code == 200, admitted.text
    assert unknown.status_code == 429
    assert counter.holder("sync-service", "sync-client:" + worker.client_id) == 1
    async with worker.factory() as db:
        assert await db.get(SyncAccessToken, credential_hash(token)) is not None
