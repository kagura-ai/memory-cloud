"""Isolation, verifier and lifetime contracts for the first-party OIDC models."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import bcrypt
import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine, event, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.oidc import OIDCAuthorizationCode, OIDCClient, OIDCGrant, OIDCToken

NOW = datetime(2026, 11, 11, 12)
EPOCH = 1794398400
SECRET = "generated-test-client-secret"


@pytest.fixture
def client() -> OIDCClient:
    return OIDCClient(
        client_id="first-party-client",
        client_name="Example dashboard",
        client_secret_hash=bcrypt.hashpw(SECRET.encode(), bcrypt.gensalt(rounds=4)).decode(),
        redirect_uris=["https://dashboard.example.com/auth/callback"],
        scope="openid profile",
    )


@pytest.fixture
def grant(client: OIDCClient) -> OIDCGrant:
    return OIDCGrant(
        id=uuid4(),
        client=client,
        client_id=client.client_id,
        user_id="account-subject",
        scope="openid profile",
        nonce="one-use-nonce",
        auth_time=EPOCH,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=8),
    )


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("models.oidc.utcnow", lambda: NOW)


def test_client_accepts_exact_redirect_and_only_code_flow(client: OIDCClient) -> None:
    assert client.get_client_id() == "first-party-client"
    assert client.get_default_redirect_uri() == "https://dashboard.example.com/auth/callback"
    assert client.check_redirect_uri("https://dashboard.example.com/auth/callback")
    for candidate in (
        "https://dashboard.example.com/auth/callback/",
        "https://dashboard.example.com/auth/callback?next=elsewhere",
        "https://dashboard.example.com.evil.test/auth/callback",
        "http://dashboard.example.com/auth/callback",
    ):
        assert not client.check_redirect_uri(candidate)
    client.redirect_uris = ["https://dashboard.example.com/*"]
    assert not client.check_redirect_uri("https://dashboard.example.com/auth/callback")
    assert client.check_response_type("code")
    assert not client.check_response_type("token")
    assert not client.check_response_type("code id_token")
    assert client.check_grant_type("authorization_code")
    assert client.check_grant_type("refresh_token")
    assert not client.check_grant_type("client_credentials")
    assert client.check_endpoint_auth_method("client_secret_post", "token")
    assert client.check_endpoint_auth_method("client_secret_basic", "token")
    assert not client.check_endpoint_auth_method("none", "token")


def test_scopes_never_fall_back_to_mcp_permissions(client: OIDCClient) -> None:
    assert client.get_allowed_scope("profile openid") == "profile openid"
    assert client.get_allowed_scope("openid openid") == "openid"
    assert client.get_allowed_scope(None) == "openid profile"
    client.scope = "openid profile workspaces:read integrations:manage"
    assert client.get_allowed_scope("openid workspaces:read") == "openid workspaces:read"
    assert client.get_allowed_scope("openid integrations:manage") == "openid integrations:manage"
    assert client.get_allowed_scope("") is None
    assert client.get_allowed_scope("memory:read") is None
    client.scope = "openid profile memory:read memory:write"
    assert client.get_allowed_scope("openid memory:write") == "openid"
    assert client.get_allowed_scope(None) == "openid profile"


def test_client_secret_check_rejects_truncation_and_invalid_text(client: OIDCClient) -> None:
    assert client.has_client_secret()
    assert client.check_client_secret(SECRET)
    assert not client.check_client_secret("wrong-secret")
    assert not client.check_client_secret("\ud800")
    prefix = "a" * 72
    client.client_secret_hash = bcrypt.hashpw(prefix.encode(), bcrypt.gensalt(rounds=4)).decode()
    assert client.check_client_secret(prefix)
    assert not client.check_client_secret(prefix + "not-compared-by-bcrypt")
    client.client_secret_hash = "invalid-stored-hash"
    assert not client.check_client_secret(prefix)


def test_revoked_client_cannot_authenticate_or_get_permissions(client: OIDCClient) -> None:
    client.revoked_at = NOW
    assert not client.check_client_secret(SECRET)
    assert not client.check_grant_type("authorization_code")
    assert not client.check_response_type("code")
    assert not client.check_redirect_uri("https://dashboard.example.com/auth/callback")
    assert client.get_allowed_scope("openid") is None


def test_code_delegates_oidc_claims_and_expires_at_boundary(grant: OIDCGrant) -> None:
    code = OIDCAuthorizationCode(
        code_hash="a" * 64,
        grant=grant,
        redirect_uri="https://dashboard.example.com/auth/callback",
        code_challenge="c" * 43,
        code_challenge_method="S256",
        expires_at=NOW + timedelta(seconds=60),
    )
    assert code.get_client_id() == "first-party-client"
    assert code.get_user_id() == "account-subject"
    assert code.get_redirect_uri() == "https://dashboard.example.com/auth/callback"
    assert code.get_scope() == "openid profile"
    assert code.get_nonce() == "one-use-nonce"
    assert code.get_auth_time() == EPOCH
    assert code.get_acr() is None
    assert code.get_amr() is None
    assert code.get_code_challenge() == "c" * 43
    assert code.get_code_challenge_method() == "S256"
    assert not code.is_expired()
    code.expires_at = NOW
    assert code.is_expired()
    code.expires_at = NOW + timedelta(seconds=60)
    code.consumed_at = NOW
    assert code.is_expired()


def test_token_uses_epoch_seconds_and_grant_absolute_lifetime(
    grant: OIDCGrant, client: OIDCClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = OIDCToken(
        grant=grant,
        scope="openid",
        access_token_hash="a" * 64,
        refresh_token_hash="b" * 64,
        issued_at=EPOCH,
        expires_in=300,
    )
    assert token.get_client_id() == client.client_id
    assert token.get_user_id() == "account-subject"
    # An individual refresh issuance may narrow, but never inherit back the grant's scope.
    assert token.get_scope() == "openid"
    assert token.get_expires_in() == 300
    assert token.get_expires_at() == EPOCH + 300
    assert token.check_client(client)
    assert not token.check_client(OIDCClient(client_id="other"))
    assert not token.is_expired()
    assert not token.is_revoked()
    assert token.is_refresh_token_active()
    monkeypatch.setattr("models.oidc.utcnow", lambda: NOW + timedelta(seconds=300))
    assert token.is_expired()
    # Access expiry does not shorten the original 8-hour grant's refresh window.
    assert token.is_refresh_token_active()
    monkeypatch.setattr("models.oidc.utcnow", lambda: NOW + timedelta(hours=8))
    assert not token.is_refresh_token_active()
    assert token.is_expired()


@pytest.mark.parametrize(
    "reason", ["refresh_used", "token_revoked", "grant_revoked", "client_revoked"]
)
def test_refresh_stops_after_consumption_or_revocation(grant: OIDCGrant, reason: str) -> None:
    token = OIDCToken(
        grant=grant,
        scope="openid profile",
        access_token_hash="a" * 64,
        refresh_token_hash="b" * 64,
        issued_at=EPOCH,
        expires_in=300,
    )
    if reason == "refresh_used":
        token.refresh_used_at = NOW
    elif reason == "token_revoked":
        token.revoked_at = NOW
    elif reason == "grant_revoked":
        grant.revoked_at = NOW
    else:
        grant.client.revoked_at = NOW
    assert not token.is_refresh_token_active()


def test_sqlite_round_trip_preserves_consumed_verifiers_and_cascades_user_erasure(
    client: OIDCClient, grant: OIDCGrant
) -> None:
    metadata = MetaData()
    users = Table("users", metadata, Column("user_id", String(255), primary_key=True))
    for model in (OIDCClient, OIDCGrant, OIDCAuthorizationCode, OIDCToken):
        model.__table__.to_metadata(metadata)
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(insert(users).values(user_id=grant.user_id))
        code = OIDCAuthorizationCode(
            code_hash="a" * 64,
            grant=grant,
            redirect_uri=client.redirect_uris[0],
            code_challenge="c" * 43,
            code_challenge_method="S256",
            expires_at=NOW + timedelta(seconds=60),
            consumed_at=NOW,
        )
        token = OIDCToken(
            grant=grant,
            scope="openid profile",
            access_token_hash="d" * 64,
            refresh_token_hash="e" * 64,
            issued_at=EPOCH,
            expires_in=300,
            refresh_used_at=NOW,
        )
        with Session(engine) as session:
            session.add_all([client, grant, code, token])
            session.commit()
            session.expire_all()
            stored = session.get(OIDCAuthorizationCode, "a" * 64)
            assert stored is not None and stored.consumed_at == NOW
            assert stored.grant.client.redirect_uris == client.redirect_uris
            assert session.scalar(select(OIDCToken).where(OIDCToken.refresh_token_hash == "e" * 64))
            session.add(
                OIDCGrant(
                    client_id=client.client_id,
                    user_id=grant.user_id,
                    scope="openid",
                    nonce=grant.nonce,
                    auth_time=EPOCH,
                    created_at=NOW,
                    expires_at=NOW + timedelta(hours=8),
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
        with engine.begin() as conn:
            conn.execute(users.delete().where(users.c.user_id == "account-subject"))
        with Session(engine) as session:
            assert not session.scalars(select(OIDCGrant)).all()
            assert not session.scalars(select(OIDCAuthorizationCode)).all()
            assert not session.scalars(select(OIDCToken)).all()
            assert session.get(OIDCClient, "first-party-client") is not None
    finally:
        engine.dispose()
