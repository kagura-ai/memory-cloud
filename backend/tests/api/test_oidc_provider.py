"""Real Authlib code flow, identity isolation and lifetime boundaries (#2781)."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import bcrypt
import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes import oidc
from config.settings import get_settings
from models.auth import User, Workspace
from models.oidc import OIDCAuthorizationCode, OIDCClient, OIDCGrant, OIDCToken
from utils.datetime import utcnow

ISSUER = "https://identity.example.test/oidc"
CALLBACK = "https://client.example.test/auth/callback"
CLIENT = "identity-test-client"
SECRET = "synthetic-confidential-client-secret-2781"
SUBJECT = "stable-user-2781"


@pytest.fixture
def store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    key = RSAKey.generate_key(2048)
    key_data = key.as_dict(private=True)
    key_data.update(kid="test-rsa", alg="RS256", use="sig")
    import json

    key_file = tmp_path / "signing.json"
    key_file.write_text(json.dumps({"keys": [key_data]}), encoding="utf-8")
    key_file.chmod(0o600)
    settings = get_settings()
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_signing_keys_file", str(key_file))
    monkeypatch.setattr(settings, "oidc_active_signing_kid", "test-rsa")
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (User, Workspace, OIDCClient, OIDCGrant, OIDCAuthorizationCode, OIDCToken):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add(User(user_id=SUBJECT, email="identity@example.test", name="Identity User"))
        db.add(
            OIDCClient(
                client_id=CLIENT,
                client_secret_hash=bcrypt.hashpw(SECRET.encode(), bcrypt.gensalt(4)).decode(),
                client_name="Identity test",
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
    yield SimpleNamespace(factory=factory, key=key, manager=manager)
    engine.dispose()


@pytest.fixture
def api(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = FastAPI()

    async def within_rate_limit(_request):
        return None

    # Distributed limiter behavior is covered separately with a real counter
    # contract. These protocol tests isolate the signed-in browser and DB.
    monkeypatch.setattr(oidc, "check_oidc_rate_limit", within_rate_limit)

    @app.middleware("http")
    async def browser_session(request: Request, call_next):
        if request.cookies.get("kagura_session") == "test-session":
            request.state.user = {"user_id": SUBJECT, "name": "Identity User"}
        return await call_next(request)

    app.include_router(oidc.router, prefix="/oidc")
    with TestClient(app, base_url="https://identity.example.test") as client:
        client.cookies.set("kagura_session", "test-session")
        yield client


def authorization_params(**changes: str) -> tuple[str, dict[str, str]]:
    verifier = secrets.token_urlsafe(48)
    params = {
        "client_id": CLIENT,
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "scope": "openid profile workspaces:read",
        "state": "synthetic-state",
        "nonce": secrets.token_urlsafe(24),
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    params.update(changes)
    return verifier, params


def authorize(api: TestClient, **changes: str) -> tuple[str, str]:
    verifier, params = authorization_params(**changes)
    page = api.get("/oidc/authorize", params=params)
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf_token" value="([a-f0-9]+)"', page.text)
    assert match, page.text
    response = api.post(
        "/oidc/authorize",
        params=params,
        data={"confirm": "yes", "csrf_token": match[1]},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == [params["state"]]
    return query["code"][0], verifier


def exchange(api: TestClient, code: str, verifier: str, **changes: str):
    body = {
        "grant_type": "authorization_code",
        "client_id": CLIENT,
        "client_secret": SECRET,
        "code": code,
        "redirect_uri": CALLBACK,
        "code_verifier": verifier,
    }
    body.update(changes)
    return api.post("/oidc/token", data=body)


def refresh(api: TestClient, token: str):
    return api.post(
        "/oidc/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT,
            "client_secret": SECRET,
            "refresh_token": token,
        },
    )


def test_code_flow_discovery_jwks_userinfo_and_hashed_credentials(api, store):
    metadata = api.get("/oidc/.well-known/openid-configuration").json()
    assert metadata["issuer"] == ISSUER
    assert metadata["token_endpoint"] == ISSUER + "/token"
    assert metadata["id_token_signing_alg_values_supported"] == ["RS256"]
    nonce = secrets.token_urlsafe(24)
    code, verifier = authorize(api, nonce=nonce)
    response = exchange(api, code, verifier)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert tokens["expires_in"] == 300
    claims = jwt.decode(tokens["id_token"], store.key, algorithms=["RS256"]).claims
    assert (claims["iss"], claims["sub"], claims["aud"], claims["nonce"]) == (
        ISSUER,
        SUBJECT,
        [CLIENT],
        nonce,
    )
    assert claims["exp"] - claims["iat"] == 300
    assert claims["auth_time"] < int(time.time()) - 500
    assert "workspace_id" not in claims and "role" not in claims
    info = api.get("/oidc/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]})
    assert info.status_code == 200 and info.json()["sub"] == SUBJECT
    public = api.get("/oidc/jwks").json()["keys"][0]
    assert public["kid"] == "test-rsa" and "d" not in public and "p" not in public
    with store.factory() as db:
        token = db.scalars(select(OIDCToken)).one()
        assert (
            token.access_token_hash == hashlib.sha256(tokens["access_token"].encode()).hexdigest()
        )
        assert (
            token.refresh_token_hash == hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
        )
        saved_code = db.scalars(select(OIDCAuthorizationCode)).one()
        assert saved_code.code_hash == hashlib.sha256(code.encode()).hexdigest()
        assert saved_code.consumed_at is not None
    assert exchange(api, code, verifier).status_code == 400


@pytest.mark.parametrize(
    "changes",
    [
        {"client_secret": "wrong"},
        {"redirect_uri": "https://other.example/callback"},
        {"code_verifier": "v" * 64},
        {"code_verifier": ""},
    ],
)
def test_bad_exchange_is_rejected_without_consuming_code(api, changes):
    code, verifier = authorize(api)
    assert exchange(api, code, verifier, **changes).status_code in (400, 401)
    assert exchange(api, code, verifier).status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "memory:read"},
        {"scope": "profile"},
        {"nonce": ""},
        {"code_challenge": ""},
        {"code_challenge_method": "plain"},
        {"redirect_uri": "https://other.example/callback"},
    ],
)
def test_bad_authorization_only_returns_errors_to_validated_callbacks(api, changes):
    _, params = authorization_params(**changes)
    response = api.get("/oidc/authorize", params=params, follow_redirects=False)
    if "redirect_uri" in changes:
        assert response.status_code == 400
        assert "location" not in response.headers
    else:
        assert response.status_code == 303
        assert response.headers["location"].startswith(CALLBACK)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["error"][0] in {"invalid_scope", "invalid_request"}
        assert query["state"] == [params["state"]]


def test_refresh_rotation_and_replay_revoke_the_grant(api, store):
    code, verifier = authorize(api)
    first = exchange(api, code, verifier).json()
    second = refresh(api, first["refresh_token"])
    assert second.status_code == 200, second.text
    assert second.json()["refresh_token"] != first["refresh_token"]
    assert refresh(api, first["refresh_token"]).status_code == 400
    assert refresh(api, second.json()["refresh_token"]).status_code == 400
    with store.factory() as db:
        assert db.scalars(select(OIDCGrant)).one().revoked_at is not None


def test_signing_failure_rolls_back_code_consumption(api, store, monkeypatch):
    code, verifier = authorize(api)
    from auth.oidc_server import IdentityCode

    original = IdentityCode.encode_id_token
    monkeypatch.setattr(
        IdentityCode,
        "encode_id_token",
        lambda *args: (_ for _ in ()).throw(ValueError("synthetic")),
    )
    assert exchange(api, code, verifier).status_code == 503
    with store.factory() as db:
        assert db.scalars(select(OIDCToken)).first() is None
        assert db.scalars(select(OIDCAuthorizationCode)).one().consumed_at is None
    monkeypatch.setattr(IdentityCode, "encode_id_token", original)
    assert exchange(api, code, verifier).status_code == 200


def test_account_reset_revokes_pending_and_issued_identity_grants(api, store):
    from models.auth import OAuth2AuthorizationCode, OAuth2Client, OAuth2DeviceCode, OAuth2Token
    from services.oauth_grant_revocation import revoke_oauth_grants_sync

    for model in (OAuth2Client, OAuth2AuthorizationCode, OAuth2DeviceCode, OAuth2Token):
        model.__table__.create(store.factory.kw["bind"])
    code, verifier = authorize(api)
    issued = exchange(api, code, verifier).json()
    pending, pending_verifier = authorize(api)
    with store.factory() as db:
        revoke_oauth_grants_sync(db, SUBJECT)
        db.commit()
    assert refresh(api, issued["refresh_token"]).status_code == 400
    assert exchange(api, pending, pending_verifier).status_code == 400
    assert (
        api.get(
            "/oidc/userinfo", headers={"Authorization": "Bearer " + issued["access_token"]}
        ).status_code
        == 401
    )


def test_refresh_uses_token_scope_and_original_absolute_deadline(api, store):
    code, verifier = authorize(api)
    issued = exchange(api, code, verifier).json()
    with store.factory() as db:
        grant = db.scalars(select(OIDCGrant)).one()
        grant.created_at = utcnow() - timedelta(hours=8)
        deadline = utcnow() + timedelta(seconds=30)
        grant.expires_at = deadline
        db.commit()
    response = api.post(
        "/oidc/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT,
            "client_secret": SECRET,
            "refresh_token": issued["refresh_token"],
            "scope": "openid",
        },
    )
    assert response.status_code == 200, response.text
    assert 1 <= response.json()["expires_in"] <= 30
    info = api.get(
        "/oidc/userinfo", headers={"Authorization": "Bearer " + response.json()["access_token"]}
    )
    assert info.json() == {"sub": SUBJECT}
    with store.factory() as db:
        grant = db.scalars(select(OIDCGrant)).one()
        assert grant.expires_at == deadline
        grant.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert refresh(api, response.json()["refresh_token"]).status_code == 400


def test_code_expiry_and_consent_csrf(api, store):
    verifier, params = authorization_params()
    assert (
        api.post(
            "/oidc/authorize", params=params, data={"confirm": "yes", "csrf_token": "wrong"}
        ).status_code
        == 403
    )
    code, verifier = authorize(api)
    with store.factory() as db:
        db.scalars(select(OIDCAuthorizationCode)).one().expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert exchange(api, code, verifier).status_code == 400


def test_duplicate_parameters_and_id_tokens_are_rejected(api):
    _, params = authorization_params()
    pairs = [*params.items(), ("nonce", "second-nonce")]
    assert api.get("/oidc/authorize", params=pairs).status_code == 400
    code, verifier = authorize(api)
    issued = exchange(api, code, verifier).json()
    assert (
        api.get(
            "/oidc/userinfo", headers={"Authorization": "Bearer " + issued["id_token"]}
        ).status_code
        == 401
    )
    assert (
        api.post("/oidc/token?client_id=other", data={"grant_type": "refresh_token"}).status_code
        == 400
    )


def test_client_authentication_basic_and_revocation(api):
    code, verifier = authorize(api)
    response = api.post(
        "/oidc/token",
        auth=(CLIENT, SECRET),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200, response.text
    issued = response.json()
    assert (
        api.post(
            "/oidc/revoke", auth=(CLIENT, SECRET), data={"token": issued["refresh_token"]}
        ).status_code
        == 200
    )
    assert refresh(api, issued["refresh_token"]).status_code == 400


def test_first_login_preserves_authorization_in_existing_login_return_to(api):
    api.cookies.clear()
    _, params = authorization_params()
    response = api.get("/oidc/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303
    destination = urlsplit(response.headers["location"])
    assert destination.netloc == "identity.example.test" and destination.path == "/login"
    return_to = parse_qs(destination.query)["return_to"][0]
    assert return_to.startswith("/oidc/authorize?")
    assert parse_qs(urlsplit(return_to).query)["nonce"] == [params["nonce"]]


def test_userinfo_returns_bearer_challenge(api):
    missing = api.get("/oidc/userinfo")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    bad = api.get("/oidc/userinfo", headers={"Authorization": "Bearer invalid"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    code, verifier = authorize(api)
    access = exchange(api, code, verifier).json()["access_token"]
    assert (
        api.get("/oidc/userinfo", headers={"Authorization": "bearer " + access}).status_code == 200
    )


def test_consent_is_bound_to_account_and_authorization_parameters(api, store):
    _, params = authorization_params()
    page = api.get("/oidc/authorize", params=params)
    assert "workspace memberships" in page.text and "Identity User" in page.text
    csrf = re.search(r'name="csrf_token" value="([a-f0-9]+)"', page.text)[1]
    params["scope"] = "openid"
    response = api.post(
        "/oidc/authorize", params=params, data={"confirm": "yes", "csrf_token": csrf}
    )
    assert response.status_code == 403
    with store.factory() as db:
        assert db.scalars(select(OIDCGrant)).first() is None


@pytest.mark.parametrize(
    "path,method",
    [("authorize", "GET"), ("token", "POST"), ("revoke", "POST"), ("userinfo", "GET")],
)
def test_protocol_guard_runs_before_db_or_crypto(api, monkeypatch, path, method):
    from fastapi.responses import JSONResponse

    async def limited(request):
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=429)

    def forbidden_db():
        raise AssertionError("The rate guard must run before DB work")

    monkeypatch.setattr(oidc, "check_oidc_rate_limit", limited)
    monkeypatch.setattr(oidc, "get_sync_session", forbidden_db)
    response = api.request(
        method, "/oidc/" + path, headers={"Authorization": "Bearer kmc_oidc_at_" + "A" * 43}
    )
    assert response.status_code == 429


def test_debug_diagnostics_do_not_retain_wire_credentials(api, caplog):
    caplog.set_level(logging.DEBUG, logger="authlib.oauth2.rfc6749.grants.authorization_code")
    caplog.set_level(logging.DEBUG, logger="authlib.oauth2.rfc6749.grants.refresh_token")
    code, verifier = authorize(api)
    first = exchange(api, code, verifier).json()
    second = refresh(api, first["refresh_token"]).json()
    for value in [
        code,
        verifier,
        SECRET,
        *[first[k] for k in ("access_token", "refresh_token", "id_token")],
        *[second[k] for k in ("access_token", "refresh_token")],
    ]:
        assert value not in caplog.text


def test_operator_client_revocation_disables_already_issued_tokens(api, store):
    from cli.oidc_client import revoke_client

    code, verifier = authorize(api)
    issued = exchange(api, code, verifier).json()
    with store.factory() as db:
        assert revoke_client(db, CLIENT)
        db.commit()
    denied = refresh(api, issued["refresh_token"])
    assert denied.status_code == 400 and denied.json()["error"] == "invalid_client"
    assert (
        api.get(
            "/oidc/userinfo", headers={"Authorization": "Bearer " + issued["access_token"]}
        ).status_code
        == 401
    )
