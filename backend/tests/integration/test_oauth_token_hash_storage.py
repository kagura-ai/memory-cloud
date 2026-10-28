"""Real PostgreSQL lifecycle, rollout and writer serialization for #2399."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, NoResultFound, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from api.main import app
from auth.oauth2_bearer import find_active_oauth_token
from auth.oauth2_server import save_token
from auth.oauth_token_storage import (
    HASH_PATTERN,
    NON_HASH_SQL,
    finalize_oauth_token_storage,
    hash_oauth_token,
    issuance_storage_mode,
)
from config.database import to_async_database_url
from db.base import Base
from models.auth import OAuth2AuthorizationCode, OAuth2Client, OAuth2Token, User
from tests.conftest import TEST_DATABASE_URL
from tests.integration.test_alembic_migrations import _sync_engine
from utils.datetime import utcnow


@pytest.fixture
def store():
    engine = _sync_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    ident = uuid4().hex
    client_id, user_id = "hash-client-" + ident, "hash-user-" + ident
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE oauth_tokens DROP CONSTRAINT IF EXISTS ck_oauth_tokens_hashed")
        )
        conn.execute(
            text(
                "INSERT INTO oauth_token_storage (id, mode) VALUES (1, 'bridge') ON CONFLICT (id) DO UPDATE SET mode='bridge'"
            )
        )
    with factory() as db:
        db.add(
            User(
                user_id=user_id,
                email=ident + "@example.test",
                name="Hash test",
                role="user",
                is_initial_admin=False,
            )
        )
        db.add(
            OAuth2Client(
                client_id=client_id,
                client_secret_hash=hashlib.sha256(b"client-secret").hexdigest(),
                client_name="Hash test",
                redirect_uris=["https://example.test/callback"],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="memory:read memory:write",
                token_endpoint_auth_method="client_secret_post",
                provider="custom",
            )
        )
        db.commit()
    yield SimpleNamespace(engine=engine, factory=factory, client_id=client_id, user_id=user_id)
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE oauth_tokens DROP CONSTRAINT IF EXISTS ck_oauth_tokens_hashed")
        )
        conn.execute(text("UPDATE oauth_token_storage SET mode='bridge' WHERE id=1"))
    with factory() as db:
        db.query(OAuth2Client).filter_by(client_id=client_id).delete()
        db.query(User).filter_by(user_id=user_id).delete()
        db.commit()
    engine.dispose()


def _issue(store, *, access="access", refresh="refresh"):
    payload = {
        "access_token": access + uuid4().hex,
        "refresh_token": refresh + uuid4().hex,
        "scope": "memory:read",
        "expires_in": 3600,
    }
    with store.factory() as db:
        request = SimpleNamespace(
            client=SimpleNamespace(client_id=store.client_id),
            user=SimpleNamespace(user_id=store.user_id),
        )
        save_token(payload, request, db)
    return payload


def _row(store):
    with store.factory() as db:
        return (
            db.query(OAuth2Token)
            .filter_by(client_id=store.client_id)
            .order_by(OAuth2Token.id.desc())
            .first()
        )


def _finalize(store):
    with store.factory() as db:
        return finalize_oauth_token_storage(db, writers_drained=True)


def _mode(store):
    with store.factory() as db:
        return db.execute(text("SELECT mode FROM oauth_token_storage WHERE id=1")).scalar_one()


async def _bearer(value):
    engine = create_async_engine(to_async_database_url(TEST_DATABASE_URL))
    try:
        async with async_sessionmaker(engine)() as db:
            return await find_active_oauth_token(value, db)
    finally:
        await engine.dispose()


def _api(store):
    return TestClient(app, base_url="https://testserver")


def _form(store, **values):
    return {"client_id": store.client_id, "client_secret": "client-secret", **values}


async def test_issued_authorization_code_use_refresh_introspect_revoke(store):
    _finalize(store)
    code, verifier = uuid4().hex, "a" * 64
    with store.factory() as db:
        db.add(
            OAuth2AuthorizationCode(
                code=code,
                client_id=store.client_id,
                user_id=store.user_id,
                redirect_uri="https://example.test/callback",
                scope="memory:read",
                auth_time=utcnow(),
                expires_at=utcnow() + timedelta(minutes=5),
                code_challenge=create_s256_code_challenge(verifier),
                code_challenge_method="S256",
            )
        )
        db.commit()
    with patch("api.routes.oauth.get_sync_session", side_effect=store.factory):
        api = _api(store)
        response = api.post(
            "/api/v1/oauth/token",
            data=_form(
                store,
                grant_type="authorization_code",
                code=code,
                code_verifier=verifier,
                redirect_uri="https://example.test/callback",
            ),
        )
        assert response.status_code == 200, response.text
        tokens = response.json()
        row = _row(store)
        assert row.access_token == hash_oauth_token(tokens["access_token"])
        assert row.refresh_token == hash_oauth_token(tokens["refresh_token"])
        assert len(tokens["access_token"]) == 43
        assert (await _bearer(tokens["access_token"])).user_id == store.user_id
        assert api.post(
            "/api/v1/oauth/introspect", data=_form(store, token=tokens["access_token"])
        ).json()["active"]
        response = api.post(
            "/api/v1/oauth/token",
            data=_form(store, grant_type="refresh_token", refresh_token=tokens["refresh_token"]),
        )
        assert response.status_code == 200, response.text
        new = response.json()
        assert _row(store).access_token == hash_oauth_token(new["access_token"])
        assert await _bearer(tokens["access_token"]) is None
        assert await _bearer(new["access_token"]) is not None
        assert (
            api.post(
                "/api/v1/oauth/token",
                data=_form(
                    store, grant_type="refresh_token", refresh_token=tokens["refresh_token"]
                ),
            ).status_code
            == 400
        )
        assert (
            api.post(
                "/api/v1/oauth/revoke", data=_form(store, token=new["refresh_token"])
            ).status_code
            == 200
        )
        assert await _bearer(new["access_token"]) is None
        assert not api.post(
            "/api/v1/oauth/introspect", data=_form(store, token=new["access_token"])
        ).json()["active"]
        assert (
            api.post(
                "/api/v1/oauth/token",
                data=_form(store, grant_type="refresh_token", refresh_token=new["refresh_token"]),
            ).status_code
            == 400
        )


async def test_legacy_and_mixed_rows_survive_finalization_including_expired_revoked(store):
    tokens = _issue(store)
    assert _row(store).access_token == tokens["access_token"]
    assert await _bearer(tokens["access_token"]) is not None
    # Legacy writers may insert even after the additive migration, while an
    # already-hashed row must not be double-hashed by a retried finalizer.
    with store.factory() as db:
        db.add_all(
            [
                OAuth2Token(
                    client_id=store.client_id,
                    user_id=store.user_id,
                    access_token="old-expired",
                    refresh_token="old-revoked",
                    revoked=True,
                    issued_at=utcnow() - timedelta(days=90),
                    expires_in=1,
                ),
                OAuth2Token(
                    client_id=store.client_id,
                    user_id=store.user_id,
                    access_token=hash_oauth_token("already-hashed"),
                    refresh_token=None,
                    expires_in=3600,
                ),
            ]
        )
        db.commit()
    assert _finalize(store) >= 2
    assert _mode(store) == "hashed"
    assert _finalize(store) == 0
    assert await _bearer(tokens["access_token"]) is not None
    with store.factory() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM oauth_tokens WHERE " + NON_HASH_SQL),
                {"pattern": HASH_PATTERN},
            ).scalar_one()
            == 0
        )
        assert (
            db.query(OAuth2Token)
            .filter_by(access_token=hash_oauth_token("already-hashed"))
            .one()
            .refresh_token
            is None
        )
        assert db.query(OAuth2Token).filter_by(
            access_token=hash_oauth_token("old-expired")
        ).one().refresh_token == hash_oauth_token("old-revoked")
    with patch("api.routes.oauth.get_sync_session", side_effect=store.factory):
        api = _api(store)
        response = api.post(
            "/api/v1/oauth/token",
            data=_form(store, grant_type="refresh_token", refresh_token=tokens["refresh_token"]),
        )
        assert response.status_code == 200, response.text
        fresh = response.json()
        assert (
            api.post(
                "/api/v1/oauth/revoke", data=_form(store, token=fresh["refresh_token"])
            ).status_code
            == 200
        )
        assert await _bearer(fresh["access_token"]) is None
    # A stale old binary cannot write plaintext after the flip.
    with store.factory() as db, pytest.raises(IntegrityError):
        db.add(
            OAuth2Token(
                client_id=store.client_id,
                user_id=store.user_id,
                access_token="late-old-writer",
                expires_in=3600,
            )
        )
        db.commit()


@pytest.mark.parametrize("finalized", [False, True])
async def test_stored_digest_is_not_a_credential_for_any_endpoint(store, finalized):
    if finalized:
        _finalize(store)
    tokens = _issue(store)
    with store.factory() as db:
        row = db.query(OAuth2Token).filter_by(client_id=store.client_id).one()
        # Mixed bridge state also must reject replayed digests.
        row.access_token = hash_oauth_token(tokens["access_token"])
        row.refresh_token = hash_oauth_token(tokens["refresh_token"])
        db.commit()
    stored = _row(store)
    assert await _bearer(stored.access_token) is None
    with patch("api.routes.oauth.get_sync_session", side_effect=store.factory):
        api = _api(store)
        assert not api.post(
            "/api/v1/oauth/introspect", data=_form(store, token=stored.access_token)
        ).json()["active"]
        assert (
            api.post(
                "/api/v1/oauth/token",
                data=_form(store, grant_type="refresh_token", refresh_token=stored.refresh_token),
            ).status_code
            == 400
        )
        for digest in (stored.access_token, stored.refresh_token):
            assert (
                api.post("/api/v1/oauth/revoke", data=_form(store, token=digest)).status_code == 200
            )
        assert await _bearer(tokens["access_token"]) is not None
        assert _row(store).is_refresh_token_active()


def test_finalizer_ack_missing_state_and_failed_check_are_fail_closed(store):
    tokens = _issue(store)
    with store.factory() as db, pytest.raises(ValueError, match="writers-drained"):
        finalize_oauth_token_storage(db, writers_drained=False)
    with store.engine.begin() as conn:
        conn.execute(text("DELETE FROM oauth_token_storage"))
    with pytest.raises(NoResultFound):
        _issue(store)
    with pytest.raises(NoResultFound):
        _finalize(store)
    assert _row(store).access_token == tokens["access_token"]
    with store.engine.begin() as conn:
        conn.execute(text("INSERT INTO oauth_token_storage VALUES (1, 'bridge')"))
    with store.factory() as db:
        original = db.execute

        def fail_after_backfill(statement, *args, **kwargs):
            if "ADD CONSTRAINT ck_oauth_tokens_hashed" in str(statement):
                raise RuntimeError("injected CHECK failure")
            return original(statement, *args, **kwargs)

        with (
            patch.object(db, "execute", side_effect=fail_after_backfill),
            pytest.raises(RuntimeError, match="injected"),
        ):
            finalize_oauth_token_storage(db, writers_drained=True)
    assert _mode(store) == "bridge"
    assert _row(store).access_token == tokens["access_token"]


def test_finalizer_wait_is_bounded_and_does_not_flip_past_inflight_writer(store):
    tokens = _issue(store)
    with store.factory() as writer:
        assert issuance_storage_mode(writer) == "bridge"
        with store.factory() as finalizer, pytest.raises(OperationalError):
            finalize_oauth_token_storage(finalizer, writers_drained=True, lock_timeout_ms=100)
        assert _mode(store) == "bridge"
        writer.rollback()
    assert _finalize(store) >= 1
    assert _row(store).access_token == hash_oauth_token(tokens["access_token"])


def test_writer_waiting_for_finalizer_reads_new_durable_mode(store):
    locked, connected = Event(), Event()
    writer_pid = []
    with store.factory() as finalizer, ThreadPoolExecutor(max_workers=1) as executor:
        original = finalizer.execute

        def gated_execute(statement, *args, **kwargs):
            result = original(statement, *args, **kwargs)
            if "LOCK TABLE oauth_tokens IN ACCESS EXCLUSIVE" in str(statement):
                locked.set()
                assert connected.wait(5)
                # Prove the issuing connection is actually blocked behind our
                # table lock before allowing the mode flip. This catches a
                # stale bridge read performed BEFORE the writer's lock.
                deadline = monotonic() + 5
                with store.engine.connect() as observer:
                    while monotonic() < deadline:
                        waiting = observer.execute(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE pid=:pid "
                                "AND relation='oauth_tokens'::regclass AND NOT granted"
                            ),
                            {"pid": writer_pid[0]},
                        ).scalar_one()
                        if waiting:
                            break
                        sleep(0.01)
                    else:
                        pytest.fail("issuer never waited for finalization lock")
            return result

        def issue_when_locked():
            assert locked.wait(5)
            with store.factory() as writer:
                writer_pid.append(writer.execute(text("SELECT pg_backend_pid()")).scalar_one())
                connected.set()
                payload = {
                    "access_token": "concurrent-access",
                    "refresh_token": "concurrent-refresh",
                }
                request = SimpleNamespace(
                    client=SimpleNamespace(client_id=store.client_id),
                    user=SimpleNamespace(user_id=store.user_id),
                )
                save_token(payload, request, writer)
                return payload

        task = executor.submit(issue_when_locked)
        with patch.object(finalizer, "execute", side_effect=gated_execute):
            finalize_oauth_token_storage(finalizer, writers_drained=True)
        tokens = task.result(timeout=10)
    assert _row(store).access_token == hash_oauth_token(tokens["access_token"])


@pytest.mark.parametrize("kind", ["access_token", "refresh_token"])
async def test_legacy_token_can_be_revoked_directly_after_finalization(store, kind):
    tokens = _issue(store)
    _finalize(store)
    with patch("api.routes.oauth.get_sync_session", side_effect=store.factory):
        response = _api(store).post("/api/v1/oauth/revoke", data=_form(store, token=tokens[kind]))
    assert response.status_code == 200
    assert await _bearer(tokens["access_token"]) is None
    assert _row(store).is_refresh_token_active() == (kind == "access_token")
