"""Kept refresh tokens of Google connected apps on PostgreSQL (#2773).

The SQLite tests in ``tests/api/test_oauth_google_connected_apps_refresh.py``
pin the behaviour; these pin what needs the real database: the bearer check
against the rewritten row in both storage modes, the row lock that
serializes two refreshes with one kept refresh token, and a revocation
racing a refresh.
"""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from api.main import app
from auth.mcp_scopes import DCR_DEFAULT_SCOPE
from auth.oauth2_bearer import find_active_oauth_token
from auth.oauth_token_storage import hash_oauth_token
from config.database import to_async_database_url
from config.settings import get_settings
from db.base import Base
from models.auth import OAuth2Client, OAuth2Token, User
from tests.conftest import TEST_DATABASE_URL
from tests.integration.test_alembic_migrations import _sync_engine

APP_URI = "https://oauth-redirect.googleusercontent.com/r/example-connected-app"


@pytest.fixture(params=["bridge", "hashed"])
def store(request, monkeypatch):
    mode = request.param
    monkeypatch.setattr(get_settings(), "google_connected_apps_redirect_uris", APP_URI)
    engine = _sync_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    ident = uuid4().hex
    client_id, user_id = "gca-client-" + ident, "gca-user-" + ident
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE oauth_tokens DROP CONSTRAINT IF EXISTS ck_oauth_tokens_hashed")
        )
        conn.execute(
            text(
                "INSERT INTO oauth_token_storage (id, mode) VALUES (1, :mode) "
                "ON CONFLICT (id) DO UPDATE SET mode = :mode"
            ),
            {"mode": mode},
        )
    with factory() as db:
        db.add(
            User(
                user_id=user_id,
                email=ident + "@example.test",
                name="GCA test",
                role="user",
                is_initial_admin=False,
            )
        )
        db.add(
            OAuth2Client(
                client_id=client_id,
                client_secret_hash="",
                client_name="Google",
                redirect_uris=[APP_URI],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=DCR_DEFAULT_SCOPE,
                token_endpoint_auth_method="none",
                provider="google_connected_apps",
            )
        )
        db.commit()
    yield SimpleNamespace(
        engine=engine, factory=factory, client_id=client_id, user_id=user_id, mode=mode
    )
    with factory() as db:
        db.query(OAuth2Token).filter_by(client_id=client_id).delete()
        db.query(OAuth2Client).filter_by(client_id=client_id).delete()
        db.query(User).filter_by(user_id=user_id).delete()
        db.commit()
    with engine.begin() as conn:
        conn.execute(text("UPDATE oauth_token_storage SET mode='bridge' WHERE id=1"))
    engine.dispose()


def _stored(store, value: str) -> str:
    return hash_oauth_token(value) if store.mode == "hashed" else value


def _issue(store) -> dict[str, str]:
    tokens = {"access_token": secrets.token_urlsafe(32), "refresh_token": secrets.token_urlsafe(32)}
    with store.factory() as db:
        db.add(
            OAuth2Token(
                client_id=store.client_id,
                user_id=store.user_id,
                token_type="Bearer",
                access_token=_stored(store, tokens["access_token"]),
                refresh_token=_stored(store, tokens["refresh_token"]),
                scope="memory:read memory:write",
                expires_in=3600,
                revoked=False,
            )
        )
        db.commit()
    return tokens


def _refresh(store, refresh_token: str):
    return TestClient(app, base_url="https://testserver").post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": store.client_id,
        },
    )


def _revoke(store, token: str):
    return TestClient(app, base_url="https://testserver").post(
        "/api/v1/oauth/revoke", data={"token": token, "client_id": store.client_id}
    )


async def _bearer(value: str):
    engine = create_async_engine(to_async_database_url(TEST_DATABASE_URL))
    try:
        async with async_sessionmaker(engine)() as db:
            return await find_active_oauth_token(value, db)
    finally:
        await engine.dispose()


def _rows(store) -> list[OAuth2Token]:
    with store.factory() as db:
        return db.query(OAuth2Token).filter_by(client_id=store.client_id).all()


def _wait_for_lock_waiters(store, count: int) -> None:
    """Block until ``count`` backends wait on a lock of ``oauth_tokens``."""
    deadline = monotonic() + 10
    with store.engine.connect() as observer:
        while monotonic() < deadline:
            waiting = observer.execute(
                text(
                    "SELECT count(DISTINCT pid) FROM pg_locks "
                    "WHERE NOT granted AND locktype IN ('transactionid', 'tuple', 'relation')"
                    " AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
            if waiting >= count:
                return
            sleep(0.02)
    pytest.fail(f"never saw {count} lock waiters")


async def test_refresh_keeps_the_token_and_the_bearer_follows_the_row(store):
    tokens = _issue(store)
    with patch("api.routes.oauth.get_sync_session", side_effect=store.factory):
        first = _refresh(store, tokens["refresh_token"])
        assert first.status_code == 200, first.text
        assert first.json()["refresh_token"] == tokens["refresh_token"]
        new_access = first.json()["access_token"]

        rows = _rows(store)
        assert len(rows) == 1
        assert rows[0].access_token == _stored(store, new_access)
        assert rows[0].refresh_token == _stored(store, tokens["refresh_token"])
        assert await _bearer(tokens["access_token"]) is None
        assert (await _bearer(new_access)).user_id == store.user_id

        second = _refresh(store, tokens["refresh_token"])
        assert second.status_code == 200, second.text
        assert second.json()["refresh_token"] == tokens["refresh_token"]
        assert await _bearer(new_access) is None
        assert await _bearer(second.json()["access_token"]) is not None

        # Revoking the current access token ends the link (gate1 #2773).
        assert _revoke(store, second.json()["access_token"]).status_code == 200
        assert await _bearer(second.json()["access_token"]) is None
        third = _refresh(store, tokens["refresh_token"])
        assert third.status_code == 400
        assert third.json()["error"] == "invalid_grant"


def test_concurrent_refreshes_with_one_kept_token_both_succeed(store):
    # Both wait on the token row; each then rewrites it in turn. Two 200s,
    # the same refresh token, and only the later access token works: a client
    # retrying a refresh only loses the earlier answer's access token.
    tokens = _issue(store)
    with (
        patch("api.routes.oauth.get_sync_session", side_effect=store.factory),
        store.factory() as holder,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        holder.query(OAuth2Token).filter_by(client_id=store.client_id).with_for_update().one()
        futures = [executor.submit(_refresh, store, tokens["refresh_token"]) for _ in range(2)]
        _wait_for_lock_waiters(store, 2)
        holder.commit()
        responses = [future.result(timeout=30) for future in futures]

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert {r.json()["refresh_token"] for r in responses} == {tokens["refresh_token"]}
    accesses = [r.json()["access_token"] for r in responses]
    assert accesses[0] != accesses[1]
    rows = _rows(store)
    assert len(rows) == 1
    live = [a for a in accesses if rows[0].access_token == _stored(store, a)]
    assert len(live) == 1
    assert rows[0].refresh_token_revoked_at is None


def test_revocation_waiting_on_a_refresh_still_ends_the_link(store):
    tokens = _issue(store)
    with (
        patch("api.routes.oauth.get_sync_session", side_effect=store.factory),
        store.factory() as holder,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        holder.query(OAuth2Token).filter_by(client_id=store.client_id).with_for_update().one()
        refresh = executor.submit(_refresh, store, tokens["refresh_token"])
        revoke = executor.submit(_revoke, store, tokens["refresh_token"])
        _wait_for_lock_waiters(store, 2)
        holder.commit()
        refreshed, revoked = refresh.result(timeout=30), revoke.result(timeout=30)

    assert revoked.status_code == 200
    # Whichever ran first, the refresh token is revoked afterwards and no
    # access token of the row works.
    assert refreshed.status_code in (200, 400)
    row = _rows(store)[0]
    assert row.refresh_token_revoked_at is not None
    assert row.access_token_revoked_at is not None
    with patch("api.routes.oauth.get_sync_session", side_effect=store.factory):
        assert _refresh(store, tokens["refresh_token"]).status_code == 400
