"""Real PostgreSQL one-use credentials and account revocation for OIDC (#2781)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from queue import Queue
from time import monotonic, sleep
from types import SimpleNamespace

import bcrypt
import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import HTTPException, Request
from joserfc.jwk import RSAKey
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from api.routes import oauth, oidc
from auth import oidc_server
from auth.dependencies import get_user_from_api_key_or_session
from auth.oauth_token_storage import hash_oauth_token
from auth.oidc_server import IdentityServer
from config.database import to_async_database_url
from config.settings import get_settings
from db import base as db_base
from db.base import Base
from mcp_server.auth import InvalidTokenError, authenticate_mcp_request
from models import oidc as oidc_models
from models.auth import OAuth2AuthorizationCode, OAuth2Client, OAuth2Token, User
from models.oidc import OIDCAuthorizationCode, OIDCClient, OIDCGrant, OIDCToken
from services.oauth_grant_revocation import revoke_oauth_grants_sync
from tests.api.test_oidc_provider import (
    CALLBACK,
    CLIENT,
    ISSUER,
    SECRET,
    SUBJECT,
    authorize,
    exchange,
    refresh,
)
from tests.api.test_oidc_provider import (
    api as api,
)
from tests.conftest import TEST_DATABASE_URL
from tests.integration.test_alembic_migrations import _sync_engine
from utils.datetime import utcnow

SECOND_CLIENT = CLIENT + "-bystander"
SECOND_SUBJECT = SUBJECT + "-bystander"


@pytest.fixture
def store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Use the shared HTTP fixture with a private, real PostgreSQL store.

    Only the reserved test client/user IDs are removed, via their grant FKs.
    Never reset the schema or truncate a shared table in these flow tests.
    """
    key = RSAKey.generate_key(2048)
    private = key.as_dict(private=True)
    private.update(kid="test-rsa", alg="RS256", use="sig")
    key_file = tmp_path / "signing.json"
    key_file.write_text(json.dumps({"keys": [private]}), encoding="utf-8")
    key_file.chmod(0o600)
    settings = get_settings()
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_signing_keys_file", str(key_file))
    monkeypatch.setattr(settings, "oidc_active_signing_kid", "test-rsa")

    engine = _sync_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    client_ids = (CLIENT, SECOND_CLIENT)
    user_ids = (SUBJECT, SECOND_SUBJECT)
    with factory() as db:
        assert not db.scalars(
            select(OIDCClient.client_id).where(OIDCClient.client_id.in_(client_ids))
        ).all(), "Reserved OIDC fixture clients already exist"
        assert not db.scalars(select(User.user_id).where(User.user_id.in_(user_ids))).all(), (
            "Reserved OIDC fixture users already exist"
        )
        db.add_all(
            [
                User(user_id=SUBJECT, email="oidc-db-2781@example.test", name="Identity User"),
                User(
                    user_id=SECOND_SUBJECT,
                    email="oidc-db-2781-bystander@example.test",
                    name="Other User",
                ),
            ]
        )
        hashed_secret = bcrypt.hashpw(SECRET.encode(), bcrypt.gensalt(4)).decode()
        for client_id in client_ids:
            db.add(
                OIDCClient(
                    client_id=client_id,
                    client_name="OIDC database fixture",
                    client_secret_hash=hashed_secret,
                    redirect_uris=[CALLBACK],
                    scope="openid profile workspaces:read",
                )
            )
        db.commit()
    monkeypatch.setattr(oidc, "get_sync_session", factory)
    manager = SimpleNamespace(
        session_holds_user=lambda sid, uid: sid == "test-session" and uid == SUBJECT,
        signed_in_at=lambda sid, uid: utcnow() - timedelta(minutes=10),
    )
    monkeypatch.setattr(oidc, "get_session_manager", lambda: manager)
    try:
        yield SimpleNamespace(engine=engine, factory=factory, key=key, manager=manager)
    finally:
        with factory() as db:
            db.execute(delete(OIDCClient).where(OIDCClient.client_id.in_(client_ids)))
            db.execute(delete(User).where(User.user_id.in_(user_ids)))
            db.commit()
        engine.dispose()


def _grant_id(store, code: str):
    with store.factory() as db:
        return db.scalar(
            select(OIDCAuthorizationCode.grant_id).where(
                OIDCAuthorizationCode.code_hash == hashlib.sha256(code.encode()).hexdigest()
            )
        )


def _wait_for_blockers(store, waiters: list[int], blocker: int) -> None:
    """Observe actual PostgreSQL lock waits, rather than timing a sleep."""
    pending = set(waiters)
    deadline = monotonic() + 5
    while pending and monotonic() < deadline:
        with store.engine.connect() as db:
            pending = {
                pid
                for pid in pending
                if not db.scalar(
                    text(
                        "WITH RECURSIVE blockers(pid) AS ("
                        " SELECT unnest(pg_blocking_pids(:waiter))"
                        " UNION SELECT unnest(pg_blocking_pids(pid)) FROM blockers"
                        ") SELECT EXISTS (SELECT 1 FROM blockers WHERE pid = :blocker)"
                    ),
                    {
                        "blocker": blocker,
                        "waiter": pid,
                    },
                )
            }
        if pending:
            sleep(0.02)
    assert not pending, f"Expected requests to wait on the fixture transaction: {pending}"


def _race_same_grant(store, monkeypatch, grant_id, operation):
    """Hold the grant until both HTTP exchanges are waiting on its row lock."""
    entered: Queue[int] = Queue()
    original = IdentityServer.lock_grant

    def observed(server, *args, **kwargs):
        entered.put(server.db.scalar(select(func.pg_backend_pid())))
        return original(server, *args, **kwargs)

    monkeypatch.setattr(IdentityServer, "lock_grant", observed)
    with store.factory() as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        blocker_pid = blocker.scalar(select(func.pg_backend_pid()))
        blocker.scalar(select(OIDCGrant).where(OIDCGrant.id == grant_id).with_for_update())
        futures = [pool.submit(operation) for _ in range(2)]
        try:
            waiters = [entered.get(timeout=5), entered.get(timeout=5)]
            assert len(set(waiters)) == 2
            _wait_for_blockers(store, waiters, blocker_pid)
        finally:
            blocker.rollback()
        return [future.result(timeout=10) for future in futures]


def _refresh_as(api, client_id: str, token: str):
    return api.post(
        "/oidc/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": SECRET,
            "refresh_token": token,
        },
    )


def _userinfo(api, token: str):
    return api.get("/oidc/userinfo", headers={"Authorization": "Bearer " + token})


def _seed_other_user_token(store) -> dict[str, str]:
    """Seed an unrelated account to prove account-level revocation confinement."""
    access = "kmc_oidc_at_" + secrets.token_urlsafe(32)
    refresh_token = "kmc_oidc_rt_" + secrets.token_urlsafe(32)
    with store.factory() as db:
        grant = OIDCGrant(
            client_id=CLIENT,
            user_id=SECOND_SUBJECT,
            scope="openid profile",
            nonce=secrets.token_urlsafe(24),
            auth_time=int(time.time()) - 10,
            expires_at=utcnow() + timedelta(hours=8),
        )
        db.add(grant)
        db.flush()
        db.add(
            OIDCToken(
                grant_id=grant.id,
                access_token_hash=hashlib.sha256(access.encode()).hexdigest(),
                refresh_token_hash=hashlib.sha256(refresh_token.encode()).hexdigest(),
                scope=grant.scope,
                issued_at=int(time.time()),
                expires_in=300,
            )
        )
        db.commit()
    return {"access_token": access, "refresh_token": refresh_token}


def test_concurrent_code_exchange_creates_exactly_one_token(api, store, monkeypatch):
    code, verifier = authorize(api)
    grant_id = _grant_id(store, code)
    results = _race_same_grant(store, monkeypatch, grant_id, lambda: exchange(api, code, verifier))
    assert sorted(response.status_code for response in results) == [200, 400]
    with store.factory() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(OIDCToken).where(OIDCToken.grant_id == grant_id)
            )
            == 1
        )
        assert db.get(OIDCAuthorizationCode, hashlib.sha256(code.encode()).hexdigest()).consumed_at


def test_concurrent_refresh_rotates_once_then_commits_family_revocation(api, store, monkeypatch):
    code, verifier = authorize(api)
    first = exchange(api, code, verifier)
    assert first.status_code == 200, first.text
    grant_id = _grant_id(store, code)
    results = _race_same_grant(
        store,
        monkeypatch,
        grant_id,
        lambda: refresh(api, first.json()["refresh_token"]),
    )
    assert sorted(response.status_code for response in results) == [200, 400]
    rotated = next(response.json() for response in results if response.status_code == 200)
    assert refresh(api, rotated["refresh_token"]).status_code == 400
    assert _userinfo(api, rotated["access_token"]).status_code == 401
    with store.factory() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(OIDCToken).where(OIDCToken.grant_id == grant_id)
            )
            == 2
        )
        assert db.get(OIDCGrant, grant_id).revoked_at is not None


def test_password_reset_lock_wins_before_waiting_code_exchange(api, store, monkeypatch):
    code, verifier = authorize(api)
    entered: Queue[int] = Queue()
    original = IdentityServer.lock_user

    def observed(server, user_id):
        entered.put(server.db.scalar(select(func.pg_backend_pid())))
        return original(server, user_id)

    monkeypatch.setattr(IdentityServer, "lock_user", observed)
    with store.factory() as reset_db, ThreadPoolExecutor(max_workers=1) as pool:
        reset_pid = reset_db.scalar(select(func.pg_backend_pid()))
        reset_db.scalar(select(User).where(User.user_id == SUBJECT).with_for_update())
        revoke_oauth_grants_sync(reset_db, SUBJECT)
        future = pool.submit(exchange, api, code, verifier)
        try:
            _wait_for_blockers(store, [entered.get(timeout=5)], reset_pid)
            reset_db.commit()
        finally:
            reset_db.rollback()
        response = future.result(timeout=10)
    assert response.status_code == 400, response.text
    with store.factory() as db:
        assert db.scalar(select(func.count()).select_from(OIDCToken)) == 0


def test_client_revocation_cannot_revoke_another_client_grant(api, store):
    code, verifier = authorize(api)
    first = exchange(api, code, verifier).json()
    second_code, second_verifier = authorize(api, client_id=SECOND_CLIENT)
    second = exchange(api, second_code, second_verifier, client_id=SECOND_CLIENT).json()
    wrong_client = api.post(
        "/oidc/revoke",
        data={
            "client_id": SECOND_CLIENT,
            "client_secret": SECRET,
            "token": first["refresh_token"],
        },
    )
    assert wrong_client.status_code == 200
    assert _userinfo(api, first["access_token"]).status_code == 200
    revoked = api.post(
        "/oidc/revoke",
        data={
            "client_id": CLIENT,
            "client_secret": SECRET,
            "token": first["refresh_token"],
        },
    )
    assert revoked.status_code == 200
    assert _userinfo(api, first["access_token"]).status_code == 401
    assert _refresh_as(api, SECOND_CLIENT, second["refresh_token"]).status_code == 200


def test_account_revocation_leaves_other_user_grants_usable(api, store):
    code, verifier = authorize(api)
    first = exchange(api, code, verifier).json()
    other = _seed_other_user_token(store)
    with store.factory() as db:
        revoke_oauth_grants_sync(db, SUBJECT)
        db.commit()
    assert _userinfo(api, first["access_token"]).status_code == 401
    assert refresh(api, first["refresh_token"]).status_code == 400
    info = _userinfo(api, other["access_token"])
    assert info.status_code == 200 and info.json()["sub"] == SECOND_SUBJECT
    assert refresh(api, other["refresh_token"]).status_code == 200


def test_refresh_keeps_absolute_deadline_and_caps_final_access_token(api, store, monkeypatch):
    code, verifier = authorize(api)
    first_response = exchange(api, code, verifier)
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()
    grant_id = _grant_id(store, code)
    with store.factory() as db:
        original = db.get(OIDCGrant, grant_id)
        deadline = original.expires_at
        assert timedelta(hours=8, seconds=-1) < deadline - original.created_at <= timedelta(hours=8)
    normal = refresh(api, first["refresh_token"])
    assert normal.status_code == 200, normal.text
    clock = deadline - timedelta(seconds=40)
    monkeypatch.setattr(oidc_server, "utcnow", lambda: clock)
    monkeypatch.setattr(oidc_models, "utcnow", lambda: clock)
    monkeypatch.setattr(
        oidc_server, "time", SimpleNamespace(time=lambda: clock.replace(tzinfo=UTC).timestamp())
    )
    final_response = refresh(api, normal.json()["refresh_token"])
    assert final_response.status_code == 200, final_response.text
    final = final_response.json()
    assert 0 < final["expires_in"] <= 40
    with store.factory() as db:
        assert db.get(OIDCGrant, grant_id).expires_at == deadline
        final_row = db.scalar(
            select(OIDCToken).where(
                OIDCToken.access_token_hash
                == hashlib.sha256(final["access_token"].encode()).hexdigest()
            )
        )
        assert (
            final_row.issued_at + final_row.expires_in <= deadline.replace(tzinfo=UTC).timestamp()
        )
    clock = deadline + timedelta(seconds=1)
    assert refresh(api, final["refresh_token"]).status_code == 400


def test_signing_failure_rolls_back_the_real_database_exchange(api, store, monkeypatch):
    code, verifier = authorize(api)
    original = oidc_server.IdentityCode.encode_id_token

    def unavailable_signer(*_args):
        raise ValueError("Synthetic unavailable signer")

    monkeypatch.setattr(oidc_server.IdentityCode, "encode_id_token", unavailable_signer)
    assert exchange(api, code, verifier).status_code == 503
    with store.factory() as db:
        pending = db.get(OIDCAuthorizationCode, hashlib.sha256(code.encode()).hexdigest())
        assert pending.consumed_at is None
        assert db.scalar(select(func.count()).select_from(OIDCToken)) == 0
    monkeypatch.setattr(oidc_server.IdentityCode, "encode_id_token", original)
    assert exchange(api, code, verifier).status_code == 200


@pytest.fixture
def legacy_credentials(store) -> Iterator[SimpleNamespace]:
    """Give the same client ID independent, valid MCP credentials in PostgreSQL."""
    code = "legacy-code-" + secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    access = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    with store.factory() as db:
        assert db.scalar(select(OAuth2Client.id).where(OAuth2Client.client_id == CLIENT)) is None, (
            "Reserved legacy fixture client already exists"
        )
        db.add(
            OAuth2Client(
                client_id=CLIENT,
                client_name="Independent memory client",
                client_secret_hash=hashlib.sha256(SECRET.encode()).hexdigest(),
                redirect_uris=[CALLBACK],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="memory:read memory:write memory:admin",
                token_endpoint_auth_method="client_secret_post",
                provider="custom",
            )
        )
        db.flush()
        db.add(
            OAuth2AuthorizationCode(
                code=code,
                client_id=CLIENT,
                user_id=SUBJECT,
                redirect_uri=CALLBACK,
                scope="memory:read memory:write memory:admin",
                code_challenge=create_s256_code_challenge(verifier),
                code_challenge_method="S256",
                expires_at=utcnow() + timedelta(minutes=5),
            )
        )
        db.add(
            OAuth2Token(
                client_id=CLIENT,
                user_id=SUBJECT,
                access_token=hash_oauth_token(access),
                refresh_token=hash_oauth_token(refresh_token),
                scope="memory:read memory:write memory:admin",
                issued_at=utcnow(),
                expires_in=3600,
            )
        )
        db.commit()
    try:
        yield SimpleNamespace(code=code, verifier=verifier, access=access, refresh=refresh_token)
    finally:
        with store.factory() as db:
            db.execute(
                delete(OAuth2AuthorizationCode).where(
                    OAuth2AuthorizationCode.client_id == CLIENT,
                    OAuth2AuthorizationCode.user_id == SUBJECT,
                )
            )
            db.execute(delete(OAuth2Client).where(OAuth2Client.client_id == CLIENT))
            db.commit()


def _legacy_form(**values: str) -> dict[str, str]:
    return {"client_id": CLIENT, "client_secret": SECRET, **values}


def test_identity_and_memory_token_routes_reject_each_others_credentials(
    api, store, legacy_credentials, monkeypatch
):
    """Client-ID collision cannot bridge the distinct tables or scope namespaces."""
    monkeypatch.setattr(oauth, "get_sync_session", store.factory)
    api.app.include_router(oauth.router, prefix="/api/v1")
    code, verifier = authorize(api)
    rejected_code = api.post(
        "/api/v1/oauth/token",
        data=_legacy_form(
            grant_type="authorization_code",
            code=code,
            redirect_uri=CALLBACK,
            code_verifier=verifier,
        ),
    )
    assert rejected_code.status_code == 400, rejected_code.text
    assert rejected_code.json()["error"] == "invalid_grant"
    issued = exchange(api, code, verifier)
    assert issued.status_code == 200, issued.text
    identity = issued.json()

    rejected_refresh = api.post(
        "/api/v1/oauth/token",
        data=_legacy_form(
            grant_type="refresh_token",
            refresh_token=identity["refresh_token"],
        ),
    )
    assert rejected_refresh.status_code == 400, rejected_refresh.text
    assert rejected_refresh.json()["error"] == "invalid_grant"
    introspection = api.post(
        "/api/v1/oauth/introspect",
        data=_legacy_form(
            token=identity["access_token"],
        ),
    )
    assert introspection.status_code == 200 and introspection.json()["active"] is False
    existing = api.post(
        "/api/v1/oauth/introspect",
        data=_legacy_form(
            token=legacy_credentials.access,
        ),
    )
    assert existing.status_code == 200 and existing.json()["active"] is True
    assert "memory:admin" in existing.json()["scope"]

    assert exchange(api, legacy_credentials.code, legacy_credentials.verifier).status_code == 400
    assert refresh(api, legacy_credentials.refresh).status_code == 400
    assert _userinfo(api, legacy_credentials.access).status_code == 401
    assert (
        api.post(
            "/oidc/revoke",
            data=_legacy_form(
                token=legacy_credentials.refresh,
            ),
        ).status_code
        == 200
    )
    assert (
        api.post(
            "/api/v1/oauth/introspect",
            data=_legacy_form(
                token=legacy_credentials.access,
            ),
        ).json()["active"]
        is True
    )
    assert refresh(api, identity["refresh_token"]).status_code == 200


def test_identity_access_token_is_rejected_by_real_rest_and_mcp_verifiers(
    api, store, legacy_credentials, monkeypatch
):
    """Only the session provider is replaced; both transports use real PG lookups."""
    code, verifier = authorize(api)
    issued = exchange(api, code, verifier)
    assert issued.status_code == 200, issued.text
    identity_access = issued.json()["access_token"]

    async def verify_transports() -> None:
        engine = create_async_engine(to_async_database_url(TEST_DATABASE_URL), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def sessions():
            async with factory() as db:
                yield db

        monkeypatch.setattr(db_base, "get_db", sessions)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "headers": [],
                "path": "/api/v1/memory/list",
                "query_string": b"",
                "server": ("identity.example.test", 443),
            }
        )
        try:
            async with factory() as db:
                with pytest.raises(HTTPException) as denied:
                    await get_user_from_api_key_or_session(request, api_key=identity_access, db=db)
                assert denied.value.status_code == 401
                accepted = await get_user_from_api_key_or_session(
                    request, api_key=legacy_credentials.access, db=db
                )
                assert accepted["user_id"] == SUBJECT
                assert "memory:admin" in accepted["oauth_scope"]
            with pytest.raises(InvalidTokenError):
                await authenticate_mcp_request("Bearer " + identity_access)
            principal = await authenticate_mcp_request("Bearer " + legacy_credentials.access)
            assert principal == (SUBJECT, None, None)
        finally:
            await engine.dispose()

    asyncio.run(verify_transports())
