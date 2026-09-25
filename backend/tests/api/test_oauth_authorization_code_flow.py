"""Authorization code and refresh grants end to end (#1686).

Drives ``GET /authorize`` → consent ``POST /authorize`` → ``POST /token`` →
refresh through the real Authlib server against an in-memory SQLite database
holding the OAuth tables (the pattern of ``test_device_code_endpoints.py``),
and pins the authorization server rules:

- the token endpoint logs the grant type and parameter names, never values;
- a request refused before consent gets an error page, not a redirect;
- PKCE accepts ``S256`` only, and a public client must send a challenge;
- the granted scope is requested ∩ registered ∩ advertised, or the registered
  scope when that leaves no ``memory:*`` scope;
- the authorization request's ``resource`` travels with the code and becomes
  the token's audience, stored as the published MCP resource; refresh keeps
  it; another resource is ``invalid_target``;
- a code is exchanged once.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from authlib.oauth2.rfc7636 import create_s256_code_challenge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

from api.main import app  # noqa: E402
from auth.mcp_scopes import DCR_DEFAULT_SCOPE  # noqa: E402
from auth.oauth2_server import _OAuthUser  # noqa: E402
from config.settings import get_settings  # noqa: E402
from models.auth import (  # noqa: E402
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2Token,
    User,
    Workspace,
)
from utils.datetime import utcnow  # noqa: E402

FRONTEND = "https://memory.example.test"
MCP_RESOURCE = f"{FRONTEND}/mcp"
REDIRECT_URI = "http://127.0.0.1:53682/callback"
WORKSPACE_ID = "3f2b8c1e-5d6a-4b7c-9e0f-1a2b3c4d5e6f"
FOREIGN_RESOURCE = "https://other.example/mcp"
PUBLIC_CLIENT = "oauth_flow_public"
NO_MEMORY_CLIENT = "oauth_flow_no_memory_scope"
LEGACY_DCR_CLIENT = "oauth_flow_dcr_claudeai"
CONFIDENTIAL_CLIENT = "oauth_flow_confidential"
CONFIDENTIAL_SECRET = "confidential-client-secret-for-the-flow-test"
USER_ID = "flow-user"


@pytest.fixture
def db_factory() -> Iterator[sessionmaker]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (User, Workspace, OAuth2Client, OAuth2AuthorizationCode, OAuth2Token):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    common: dict[str, Any] = {
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    with factory() as db:
        db.add_all(
            [
                # A DCR registration: public client, the DCR default scope.
                OAuth2Client(
                    client_id=PUBLIC_CLIENT,
                    client_secret_hash="",
                    client_name="Flow Public Client",
                    scope=DCR_DEFAULT_SCOPE,
                    token_endpoint_auth_method="none",
                    provider="claude",
                    **common,
                ),
                # An admin-managed public client registered without a memory scope.
                OAuth2Client(
                    client_id=NO_MEMORY_CLIENT,
                    client_secret_hash="",
                    client_name="Flow No Memory Scope Client",
                    scope="openid offline_access",
                    token_endpoint_auth_method="none",
                    provider="custom",
                    owner_id="admin-user",
                    **common,
                ),
                # A DCR row stored with the scope the client asked for.
                OAuth2Client(
                    client_id=LEGACY_DCR_CLIENT,
                    client_secret_hash="",
                    client_name="Flow DCR claudeai Client",
                    scope="claudeai",
                    token_endpoint_auth_method="none",
                    provider="claude",
                    **common,
                ),
                # An admin-managed confidential client that registered memory:admin.
                OAuth2Client(
                    client_id=CONFIDENTIAL_CLIENT,
                    client_secret_hash=hashlib.sha256(CONFIDENTIAL_SECRET.encode()).hexdigest(),
                    client_name="Flow Confidential Client",
                    scope="memory:read memory:write memory:admin",
                    token_endpoint_auth_method="client_secret_post",
                    provider="custom",
                    **common,
                ),
            ]
        )
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def api(db_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FRONTEND_URL", FRONTEND)
    monkeypatch.delenv("MCP_BASE_PATH", raising=False)
    monkeypatch.setattr(get_settings(), "oauth_pkce_required", True)
    user = _OAuthUser(user_id=USER_ID, email="flow-user@example.test")
    with (
        patch("api.routes.oauth.get_sync_session", side_effect=db_factory),
        patch("api.routes.oauth.get_current_user_from_session", return_value=user),
    ):
        # Authlib refuses the OAuth endpoints over plain http.
        yield TestClient(app, base_url="https://testserver")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, create_s256_code_challenge(verifier)


def _authorize_params(
    client_id: str = PUBLIC_CLIENT,
    *,
    challenge: str | None = None,
    method: str | None = "S256",
    scope: str | None = "memory:read memory:write",
    resource: str | None = None,
    state: str = "flow-state",
) -> dict[str, str]:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    if challenge is not None:
        params["code_challenge"] = challenge
    if method is not None:
        params["code_challenge_method"] = method
    if scope is not None:
        params["scope"] = scope
    if resource is not None:
        params["resource"] = resource
    return params


def _redirect_params(response: Any) -> dict[str, str]:
    location = response.headers["location"]
    assert location.startswith(REDIRECT_URI), location
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def _assert_error_page(response: Any, error: str) -> None:
    """The authorization request was refused before consent: page, no redirect."""
    assert response.status_code == 400, response.text[:300]
    assert "location" not in response.headers
    assert response.headers["content-type"].startswith("text/html")
    assert error in response.text
    assert 'name="confirm" value="yes"' not in response.text


def _submit_consent(api: TestClient, params: dict[str, str]) -> Any:
    """POST the consent form directly (without loading the consent page)."""
    return api.post(
        f"/api/v1/oauth/authorize?{urlencode(params)}",
        data={"confirm": "yes"},
        follow_redirects=False,
    )


def _consent(api: TestClient, params: dict[str, str]) -> dict[str, str]:
    """Approve on the consent page; return the redirect's query parameters."""
    page = api.get("/api/v1/oauth/authorize", params=params, follow_redirects=False)
    assert page.status_code == 200, page.text[:300]
    approved = api.post(
        f"/api/v1/oauth/authorize?{urlencode(params)}",
        data={"confirm": "yes"},
        follow_redirects=False,
    )
    assert approved.status_code == 303, approved.text[:300]
    return _redirect_params(approved)


def _token(api: TestClient, form: dict[str, str]) -> Any:
    return api.post("/api/v1/oauth/token", data=form)


def _exchange(api: TestClient, code: str, verifier: str | None, **extra: str) -> dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": PUBLIC_CLIENT,
        **extra,
    }
    if verifier is not None:
        form["code_verifier"] = verifier
    response = _token(api, form)
    assert response.status_code == 200, response.text
    return response.json()


def _refresh(api: TestClient, refresh_token: str, **extra: str) -> Any:
    return _token(
        api,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": PUBLIC_CLIENT,
            **extra,
        },
    )


def _stored_token(db_factory: sessionmaker, access_token: str) -> OAuth2Token:
    with db_factory() as db:
        return db.query(OAuth2Token).filter_by(access_token=access_token).one()


def _codes(db_factory: sessionmaker) -> int:
    with db_factory() as db:
        return db.query(OAuth2AuthorizationCode).count()


def _public_tokens(api: TestClient, *, resource: str | None) -> dict[str, Any]:
    verifier, challenge = _pkce()
    redirect = _consent(api, _authorize_params(challenge=challenge, resource=resource))
    return _exchange(api, redirect["code"], verifier)


# ---------------------------------------------------------------------------
# Token-endpoint logging
# ---------------------------------------------------------------------------


class TestTokenEndpointLogging:
    """No credential value reaches the logs on the code and refresh grants."""

    def test_code_and_refresh_grants_log_no_credential_values(
        self, api: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        verifier, challenge = _pkce()
        with capture_logs() as events:
            public = _consent(api, _authorize_params(challenge=challenge, resource=MCP_RESOURCE))
            first = _exchange(api, public["code"], verifier, resource=MCP_RESOURCE)
            refreshed = _refresh(api, first["refresh_token"], resource=MCP_RESOURCE)
            assert refreshed.status_code == 200, refreshed.text

            confidential = _consent(
                api, _authorize_params(CONFIDENTIAL_CLIENT, method=None, scope=None)
            )
            confidential_tokens = _token(
                api,
                {
                    "grant_type": "authorization_code",
                    "code": confidential["code"],
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CONFIDENTIAL_CLIENT,
                    "client_secret": CONFIDENTIAL_SECRET,
                },
            )
            assert confidential_tokens.status_code == 200, confidential_tokens.text

        secret_values = {
            "authorization code": public["code"],
            "confidential authorization code": confidential["code"],
            "code_verifier": verifier,
            "access token": first["access_token"],
            "refresh token": first["refresh_token"],
            "refreshed access token": refreshed.json()["access_token"],
            "refreshed refresh token": refreshed.json()["refresh_token"],
            "client secret": CONFIDENTIAL_SECRET,
            "confidential access token": confidential_tokens.json()["access_token"],
        }
        logged = [repr(event) for event in events] + [
            f"{record.getMessage()} {record.args!r}" for record in caplog.records
        ]
        assert logged, "expected the flow to log"
        for name, value in secret_values.items():
            assert not any(value in line for line in logged), f"{name} found in a log record"

        token_requests = [event for event in events if event["event"] == "token_request"]
        assert [event["grant_type"] for event in token_requests] == [
            "authorization_code",
            "refresh_token",
            "authorization_code",
        ]
        assert token_requests[0]["params"] == [
            "client_id",
            "code",
            "code_verifier",
            "grant_type",
            "redirect_uri",
            "resource",
        ]

    def test_authlib_loggers_stay_at_info_under_a_debug_root(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Authlib logs issued token dicts at DEBUG only.
        caplog.set_level(logging.DEBUG)
        grant_logger = logging.getLogger("authlib.oauth2.rfc6749.grants.authorization_code")
        assert not grant_logger.isEnabledFor(logging.DEBUG)


# ---------------------------------------------------------------------------
# PKCE: S256 only
# ---------------------------------------------------------------------------


class TestPkceS256Only:
    def test_plain_method_is_refused_before_consent(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier = secrets.token_urlsafe(48)
        response = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(challenge=verifier, method="plain"),
            follow_redirects=False,
        )

        _assert_error_page(response, "invalid_request")
        assert "S256" in response.text
        assert _codes(db_factory) == 0

    def test_plain_method_is_refused_at_consent_submission(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier = secrets.token_urlsafe(48)
        response = _submit_consent(api, _authorize_params(challenge=verifier, method="plain"))

        assert response.status_code == 303
        redirect = _redirect_params(response)
        assert redirect["error"] == "invalid_request"
        assert "code" not in redirect
        assert _codes(db_factory) == 0

    def test_challenge_without_method_is_refused(self, api: TestClient) -> None:
        # An omitted method means plain (RFC 7636 §4.3).
        _, challenge = _pkce()
        response = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(challenge=challenge, method=None),
            follow_redirects=False,
        )

        _assert_error_page(response, "invalid_request")

    def test_public_client_without_challenge_is_refused(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        response = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(method=None),
            follow_redirects=False,
        )

        _assert_error_page(response, "invalid_request")
        assert "code_challenge" in response.text
        assert _codes(db_factory) == 0

    def test_public_client_without_challenge_is_refused_at_consent_submission(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        response = _submit_consent(api, _authorize_params(method=None))

        assert response.status_code == 303
        assert _redirect_params(response)["error"] == "invalid_request"
        assert _codes(db_factory) == 0

    def test_confidential_client_without_challenge_is_served(self, api: TestClient) -> None:
        redirect = _consent(api, _authorize_params(CONFIDENTIAL_CLIENT, method=None))
        assert redirect["code"]

    def test_code_stored_with_plain_method_is_not_exchanged(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier = secrets.token_urlsafe(48)
        with db_factory() as db:
            db.add(
                OAuth2AuthorizationCode(
                    code="plain-method-code",
                    client_id=PUBLIC_CLIENT,
                    user_id=USER_ID,
                    redirect_uri=REDIRECT_URI,
                    scope="memory:read",
                    code_challenge=verifier,
                    code_challenge_method="plain",
                    auth_time=utcnow(),
                    expires_at=utcnow() + timedelta(seconds=600),
                )
            )
            db.commit()

        response = _token(
            api,
            {
                "grant_type": "authorization_code",
                "code": "plain-method-code",
                "redirect_uri": REDIRECT_URI,
                "client_id": PUBLIC_CLIENT,
                "code_verifier": verifier,
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    def test_s256_exchange_requires_the_matching_verifier(self, api: TestClient) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge))
        wrong = _token(
            api,
            {
                "grant_type": "authorization_code",
                "code": redirect["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": PUBLIC_CLIENT,
                "code_verifier": secrets.token_urlsafe(48),
            },
        )
        assert wrong.status_code == 400
        assert wrong.json()["error"] == "invalid_grant"

        assert _exchange(api, redirect["code"], verifier)["access_token"]


# ---------------------------------------------------------------------------
# Scope at /authorize
# ---------------------------------------------------------------------------


class TestGrantedScope:
    def test_undefined_scope_is_dropped(self, api: TestClient) -> None:
        tokens = _public_tokens_with_scope(api, "memory:read memory:write undefined:scope")
        assert tokens["scope"] == "memory:read memory:write"

    def test_admin_is_not_granted_to_a_client_that_did_not_register_it(
        self, api: TestClient
    ) -> None:
        tokens = _public_tokens_with_scope(api, "memory:read memory:admin")
        assert tokens["scope"] == "memory:read"

    def test_admin_is_granted_to_a_client_that_registered_it(self, api: TestClient) -> None:
        redirect = _consent(
            api,
            _authorize_params(CONFIDENTIAL_CLIENT, method=None, scope="memory:read memory:admin"),
        )
        response = _token(
            api,
            {
                "grant_type": "authorization_code",
                "code": redirect["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": CONFIDENTIAL_CLIENT,
                "client_secret": CONFIDENTIAL_SECRET,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["scope"] == "memory:read memory:admin"

    def test_request_without_scope_gets_the_registered_scope(self, api: TestClient) -> None:
        tokens = _public_tokens_with_scope(api, None)
        assert tokens["scope"] == DCR_DEFAULT_SCOPE

    @pytest.mark.parametrize(
        "scope",
        ["claudeai", "openid offline_access", "", "memory:admin undefined:scope"],
    )
    def test_no_memory_scope_requested_gets_the_registered_scope(
        self, api: TestClient, scope: str
    ) -> None:
        tokens = _public_tokens_with_scope(api, scope)
        assert tokens["scope"] == DCR_DEFAULT_SCOPE

    def test_client_registered_without_memory_scope_gets_an_error_page(
        self, api: TestClient
    ) -> None:
        _, challenge = _pkce()
        response = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(NO_MEMORY_CLIENT, challenge=challenge, scope="openid"),
            follow_redirects=False,
        )

        _assert_error_page(response, "invalid_scope")

    def test_consent_submission_checks_the_scope_again(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        _, challenge = _pkce()
        response = _submit_consent(
            api, _authorize_params(NO_MEMORY_CLIENT, challenge=challenge, scope="openid")
        )

        assert response.status_code == 303
        redirect = _redirect_params(response)
        assert redirect["error"] == "invalid_scope"
        assert redirect["state"] == "flow-state"
        assert _codes(db_factory) == 0

    def test_consent_submission_grants_only_the_granted_scope(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        # A consent POST that did not come from the page asks for memory:admin,
        # which the public client did not register.
        verifier, challenge = _pkce()
        response = _submit_consent(
            api,
            _authorize_params(challenge=challenge, scope="memory:read memory:admin"),
        )
        code = _redirect_params(response)["code"]
        with db_factory() as db:
            assert db.query(OAuth2AuthorizationCode).filter_by(code=code).one().scope == (
                "memory:read"
            )

        assert _exchange(api, code, verifier)["scope"] == "memory:read"

    def test_consent_page_for_a_request_without_memory_scope(self, api: TestClient) -> None:
        _, challenge = _pkce()
        page = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(challenge=challenge, scope="claudeai"),
            headers={"Accept-Language": "en"},
        )

        assert page.status_code == 200
        for line in ("Read your memories", "Write new memories", "Delete memories"):
            assert line in page.text
        assert "Manage your memory cloud" not in page.text

    def test_consent_page_lists_the_granted_permissions(self, api: TestClient) -> None:
        _, challenge = _pkce()
        page = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(challenge=challenge, scope="memory:read memory:admin"),
            headers={"Accept-Language": "en"},
        )

        assert page.status_code == 200
        assert "Read your memories" in page.text
        assert "Write new memories" not in page.text
        assert "Manage your memory cloud" not in page.text

    def test_consent_page_for_the_registered_scope(self, api: TestClient) -> None:
        _, challenge = _pkce()
        page = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(challenge=challenge, scope=None),
            headers={"Accept-Language": "en"},
        )

        assert page.status_code == 200
        for line in ("Read your memories", "Write new memories", "Delete memories"):
            assert line in page.text
        assert "Manage your memory cloud" not in page.text

    def test_refresh_cannot_widen_the_scope(self, api: TestClient) -> None:
        tokens = _public_tokens_with_scope(api, "memory:read")
        response = _refresh(api, tokens["refresh_token"], scope="memory:read memory:write")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_scope"


def _public_tokens_with_scope(api: TestClient, scope: str | None) -> dict[str, Any]:
    verifier, challenge = _pkce()
    redirect = _consent(api, _authorize_params(challenge=challenge, scope=scope))
    return _exchange(api, redirect["code"], verifier)


# ---------------------------------------------------------------------------
# resource (RFC 8707)
# ---------------------------------------------------------------------------


class TestResourceBinding:
    def test_authorization_resource_becomes_the_audience(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        # The token request does not repeat ``resource``.
        tokens = _public_tokens(api, resource=MCP_RESOURCE)
        assert _stored_token(db_factory, tokens["access_token"]).resource == MCP_RESOURCE

    def test_trailing_slash_names_the_same_resource(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(
            api, _authorize_params(challenge=challenge, resource=f"{MCP_RESOURCE}/")
        )
        tokens = _exchange(api, redirect["code"], verifier, resource=MCP_RESOURCE)
        assert _stored_token(db_factory, tokens["access_token"]).resource == MCP_RESOURCE

    def test_token_request_resource_sets_the_audience(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge))
        tokens = _exchange(api, redirect["code"], verifier, resource=f"{MCP_RESOURCE}/")
        assert _stored_token(db_factory, tokens["access_token"]).resource == MCP_RESOURCE

    def test_no_resource_anywhere_issues_no_audience(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _public_tokens(api, resource=None)
        assert _stored_token(db_factory, tokens["access_token"]).resource is None

    def test_foreign_resource_is_refused_at_authorization(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        _, challenge = _pkce()
        response = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(challenge=challenge, resource=FOREIGN_RESOURCE),
            follow_redirects=False,
        )

        _assert_error_page(response, "invalid_target")
        assert _codes(db_factory) == 0

    def test_foreign_resource_is_refused_at_consent_submission(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        _, challenge = _pkce()
        params = _authorize_params(challenge=challenge, resource="https://other.example/mcp")
        response = api.post(
            f"/api/v1/oauth/authorize?{urlencode(params)}",
            data={"confirm": "yes"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert _redirect_params(response)["error"] == "invalid_target"
        assert _codes(db_factory) == 0

    def test_foreign_resource_at_the_token_endpoint(self, api: TestClient) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge, resource=MCP_RESOURCE))
        refused = _token(
            api,
            {
                "grant_type": "authorization_code",
                "code": redirect["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": PUBLIC_CLIENT,
                "code_verifier": verifier,
                "resource": "https://other.example/mcp",
            },
        )
        assert refused.status_code == 400
        assert refused.json()["error"] == "invalid_target"

        # The refused request did not consume the code.
        assert _exchange(api, redirect["code"], verifier)["access_token"]

    def test_token_resource_differing_from_the_code_is_invalid_target(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier, challenge = _pkce()
        with db_factory() as db:
            db.add(
                OAuth2AuthorizationCode(
                    code="other-resource-code",
                    client_id=PUBLIC_CLIENT,
                    user_id=USER_ID,
                    redirect_uri=REDIRECT_URI,
                    scope="memory:read",
                    code_challenge=challenge,
                    code_challenge_method="S256",
                    resource="https://other.example/mcp",
                    auth_time=utcnow(),
                    expires_at=utcnow() + timedelta(seconds=600),
                )
            )
            db.commit()

        response = _token(
            api,
            {
                "grant_type": "authorization_code",
                "code": "other-resource-code",
                "redirect_uri": REDIRECT_URI,
                "client_id": PUBLIC_CLIENT,
                "code_verifier": verifier,
                "resource": MCP_RESOURCE,
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_target"

    def test_refresh_keeps_the_audience(self, api: TestClient, db_factory: sessionmaker) -> None:
        tokens = _public_tokens(api, resource=MCP_RESOURCE)
        refreshed = _refresh(api, tokens["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        stored = _stored_token(db_factory, refreshed.json()["access_token"])
        assert stored.resource == MCP_RESOURCE

    def test_refresh_with_another_resource_is_invalid_target(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _public_tokens(api, resource=MCP_RESOURCE)
        with db_factory() as db:
            row = db.query(OAuth2Token).filter_by(access_token=tokens["access_token"]).one()
            row.resource = "https://other.example/mcp"
            db.commit()

        response = _refresh(api, tokens["refresh_token"], resource=MCP_RESOURCE)

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_target"

    def test_refresh_with_a_foreign_resource_is_invalid_target(self, api: TestClient) -> None:
        tokens = _public_tokens(api, resource=MCP_RESOURCE)
        response = _refresh(api, tokens["refresh_token"], resource="https://other.example/mcp")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_target"

    def test_refresh_of_a_token_without_audience(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _public_tokens(api, resource=None)
        unchanged = _refresh(api, tokens["refresh_token"])
        assert unchanged.status_code == 200, unchanged.text
        assert _stored_token(db_factory, unchanged.json()["access_token"]).resource is None

        # Naming this server's MCP resource narrows the new token to it.
        narrowed = _refresh(api, unchanged.json()["refresh_token"], resource=MCP_RESOURCE)
        assert narrowed.status_code == 200, narrowed.text
        stored = _stored_token(db_factory, narrowed.json()["access_token"])
        assert stored.resource == MCP_RESOURCE


class TestResourceForms:
    """Every form of the MCP resource is accepted and stored as published."""

    @pytest.mark.parametrize(
        "resource",
        [
            f"{MCP_RESOURCE}/w/{WORKSPACE_ID}",
            f"{MCP_RESOURCE}?profile=core",
            f"{MCP_RESOURCE}/w/{WORKSPACE_ID}?profile=core",
            "HTTPS://Memory.Example.Test:443/mcp",
        ],
    )
    def test_authorization_resource_forms(
        self, api: TestClient, db_factory: sessionmaker, resource: str
    ) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge, resource=resource))
        with db_factory() as db:
            stored = db.query(OAuth2AuthorizationCode).filter_by(code=redirect["code"]).one()
            assert stored.resource == MCP_RESOURCE

        tokens = _exchange(api, redirect["code"], verifier, resource=resource)
        assert _stored_token(db_factory, tokens["access_token"]).resource == MCP_RESOURCE

    @pytest.mark.parametrize("resource", [f"{MCP_RESOURCE}/sse", f"{MCP_RESOURCE}/w/x"])
    def test_token_request_resource_forms(
        self, api: TestClient, db_factory: sessionmaker, resource: str
    ) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge, resource=MCP_RESOURCE))
        tokens = _exchange(api, redirect["code"], verifier, resource=resource)
        assert _stored_token(db_factory, tokens["access_token"]).resource == MCP_RESOURCE

    @pytest.mark.parametrize(
        "stored",
        [f"{MCP_RESOURCE}/w/{WORKSPACE_ID}", f"{MCP_RESOURCE}?profile=core", f"{MCP_RESOURCE}/"],
    )
    @pytest.mark.parametrize("requested", [None, MCP_RESOURCE, f"{MCP_RESOURCE}/w/{WORKSPACE_ID}"])
    def test_refresh_of_a_token_stored_with_another_form(
        self,
        api: TestClient,
        db_factory: sessionmaker,
        stored: str,
        requested: str | None,
    ) -> None:
        # A token whose audience was stored in another accepted form.
        tokens = _public_tokens(api, resource=None)
        with db_factory() as db:
            row = db.query(OAuth2Token).filter_by(access_token=tokens["access_token"]).one()
            row.resource = stored
            db.commit()

        extra = {"resource": requested} if requested else {}
        refreshed = _refresh(api, tokens["refresh_token"], **extra)

        assert refreshed.status_code == 200, refreshed.text
        stored_token = _stored_token(db_factory, refreshed.json()["access_token"])
        assert stored_token.resource == MCP_RESOURCE

    def test_code_stored_with_another_form(self, api: TestClient, db_factory: sessionmaker) -> None:
        verifier, challenge = _pkce()
        with db_factory() as db:
            db.add(
                OAuth2AuthorizationCode(
                    code="workspace-form-code",
                    client_id=PUBLIC_CLIENT,
                    user_id=USER_ID,
                    redirect_uri=REDIRECT_URI,
                    scope="memory:read",
                    code_challenge=challenge,
                    code_challenge_method="S256",
                    resource=f"{MCP_RESOURCE}/w/{WORKSPACE_ID}",
                    auth_time=utcnow(),
                    expires_at=utcnow() + timedelta(seconds=600),
                )
            )
            db.commit()

        tokens = _exchange(api, "workspace-form-code", verifier, resource=MCP_RESOURCE)
        assert _stored_token(db_factory, tokens["access_token"]).resource == MCP_RESOURCE

    @pytest.mark.parametrize(
        "resource",
        [
            "https://memory.example.test/api/v1",
            "https://memory.example.test",
            "http://memory.example.test/mcp",
            "https://memory.example.test:8443/mcp",
            f"{MCP_RESOURCE}/../api",
        ],
    )
    def test_other_paths_and_origins_are_refused(self, api: TestClient, resource: str) -> None:
        _, challenge = _pkce()
        response = api.get(
            "/api/v1/oauth/authorize",
            params=_authorize_params(challenge=challenge, resource=resource),
            follow_redirects=False,
        )
        _assert_error_page(response, "invalid_target")


class TestSingleUse:
    def test_second_exchange_of_a_code_is_invalid_grant(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge))
        first = _exchange(api, redirect["code"], verifier)

        second = _token(
            api,
            {
                "grant_type": "authorization_code",
                "code": redirect["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": PUBLIC_CLIENT,
                "code_verifier": verifier,
            },
        )

        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"
        with db_factory() as db:
            assert db.query(OAuth2Token).count() == 1
            assert db.query(OAuth2Token).one().access_token == first["access_token"]
        assert _codes(db_factory) == 0


class TestDcrClientsWithoutMemoryScope:
    """A DCR client that registered only non-memory scopes is served."""

    def test_dcr_row_registered_with_claudeai_authorizes(self, api: TestClient) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(
            api, _authorize_params(LEGACY_DCR_CLIENT, challenge=challenge, scope="claudeai")
        )
        tokens = _exchange(api, redirect["code"], verifier, client_id=LEGACY_DCR_CLIENT)
        assert tokens["scope"] == DCR_DEFAULT_SCOPE

    @pytest.mark.parametrize("scope", ["claudeai", "openid offline_access", "openid profile"])
    def test_registration_then_authorization(
        self, api: TestClient, db_factory: sessionmaker, scope: str
    ) -> None:
        with patch("api.routes.oauth.increment_counter", AsyncMock(return_value=1)):
            registered = api.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": "Claude Code",
                    "redirect_uris": [REDIRECT_URI],
                    "scope": scope,
                },
            )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        assert registered.json()["scope"] == DCR_DEFAULT_SCOPE
        with db_factory() as db:
            assert db.query(OAuth2Client).filter_by(client_id=client_id).one().scope == (
                DCR_DEFAULT_SCOPE
            )

        verifier, challenge = _pkce()
        redirect = _consent(
            api,
            _authorize_params(client_id, challenge=challenge, scope=scope, resource=MCP_RESOURCE),
        )
        tokens = _exchange(api, redirect["code"], verifier, client_id=client_id)
        assert tokens["scope"] == DCR_DEFAULT_SCOPE
        assert _stored_token(db_factory, tokens["access_token"]).resource == MCP_RESOURCE


class TestRefreshSingleUse:
    def test_second_refresh_with_one_refresh_token_is_invalid_grant(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _public_tokens(api, resource=MCP_RESOURCE)
        first = _refresh(api, tokens["refresh_token"])
        assert first.status_code == 200, first.text

        second = _refresh(api, tokens["refresh_token"])

        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"
        with db_factory() as db:
            # The original pair and the one refresh.
            assert db.query(OAuth2Token).count() == 2
            original = db.query(OAuth2Token).filter_by(access_token=tokens["access_token"]).one()
            assert original.refresh_token_revoked_at is not None
            assert original.access_token_revoked_at is not None


def _form(api: TestClient, pairs: list[tuple[str, str]]) -> Any:
    """POST a token request whose form may repeat a parameter."""
    return api.post(
        "/api/v1/oauth/token",
        content=urlencode(pairs),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


_RESOURCE_ORDERS = pytest.mark.parametrize(
    "resources",
    [[FOREIGN_RESOURCE, MCP_RESOURCE], [MCP_RESOURCE, FOREIGN_RESOURCE]],
    ids=["foreign-then-valid", "valid-then-foreign"],
)


class TestRepeatedParameters:
    """Every value of a repeated ``resource`` is checked; other parameters the
    token endpoint reads may be sent once (RFC 6749 §3.1)."""

    @_RESOURCE_ORDERS
    def test_authorization_request_with_two_resources(
        self, api: TestClient, resources: list[str]
    ) -> None:
        _, challenge = _pkce()
        pairs = [*_authorize_params(challenge=challenge).items()]
        pairs += [("resource", resource) for resource in resources]
        response = api.get(f"/api/v1/oauth/authorize?{urlencode(pairs)}", follow_redirects=False)
        _assert_error_page(response, "invalid_target")

    @_RESOURCE_ORDERS
    def test_consent_submission_with_two_resources(
        self, api: TestClient, db_factory: sessionmaker, resources: list[str]
    ) -> None:
        _, challenge = _pkce()
        pairs = [*_authorize_params(challenge=challenge).items()]
        pairs += [("resource", resource) for resource in resources]
        response = api.post(
            f"/api/v1/oauth/authorize?{urlencode(pairs)}",
            data={"confirm": "yes"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert _redirect_params(response)["error"] == "invalid_target"
        assert _codes(db_factory) == 0

    @_RESOURCE_ORDERS
    def test_code_exchange_with_two_resources(self, api: TestClient, resources: list[str]) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge, resource=MCP_RESOURCE))
        pairs = [
            ("grant_type", "authorization_code"),
            ("code", redirect["code"]),
            ("redirect_uri", REDIRECT_URI),
            ("client_id", PUBLIC_CLIENT),
            ("code_verifier", verifier),
        ]
        response = _form(api, pairs + [("resource", resource) for resource in resources])

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_target"
        # The refused request did not consume the code.
        assert _exchange(api, redirect["code"], verifier)["access_token"]

    @_RESOURCE_ORDERS
    def test_refresh_with_two_resources(self, api: TestClient, resources: list[str]) -> None:
        tokens = _public_tokens(api, resource=MCP_RESOURCE)
        pairs = [
            ("grant_type", "refresh_token"),
            ("refresh_token", tokens["refresh_token"]),
            ("client_id", PUBLIC_CLIENT),
        ]
        response = _form(api, pairs + [("resource", resource) for resource in resources])

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_target"
        # The refresh token is still usable.
        assert _refresh(api, tokens["refresh_token"]).status_code == 200

    def test_two_valid_resources_are_accepted(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge))
        response = _form(
            api,
            [
                ("grant_type", "authorization_code"),
                ("code", redirect["code"]),
                ("redirect_uri", REDIRECT_URI),
                ("client_id", PUBLIC_CLIENT),
                ("code_verifier", verifier),
                ("resource", MCP_RESOURCE),
                ("resource", f"{MCP_RESOURCE}/w/{WORKSPACE_ID}"),
            ],
        )
        assert response.status_code == 200, response.text
        stored = _stored_token(db_factory, response.json()["access_token"])
        assert stored.resource == MCP_RESOURCE

    @pytest.mark.parametrize("repeated", ["code", "code_verifier", "grant_type", "client_id"])
    def test_repeated_single_valued_parameter_is_invalid_request(
        self, api: TestClient, repeated: str
    ) -> None:
        verifier, challenge = _pkce()
        redirect = _consent(api, _authorize_params(challenge=challenge))
        pairs = [
            ("grant_type", "authorization_code"),
            ("code", redirect["code"]),
            ("redirect_uri", REDIRECT_URI),
            ("client_id", PUBLIC_CLIENT),
            ("code_verifier", verifier),
        ]
        pairs.append(next(pair for pair in pairs if pair[0] == repeated))
        response = _form(api, pairs)

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
        assert repeated in response.json()["error_description"]
        # The refused request did not consume the code.
        assert _exchange(api, redirect["code"], verifier)["access_token"]

    def test_repeated_refresh_token_is_invalid_request(self, api: TestClient) -> None:
        tokens = _public_tokens(api, resource=None)
        response = _form(
            api,
            [
                ("grant_type", "refresh_token"),
                ("refresh_token", tokens["refresh_token"]),
                ("refresh_token", tokens["refresh_token"]),
                ("client_id", PUBLIC_CLIENT),
            ],
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"


class TestVerifierWithoutChallenge:
    def test_code_issued_without_challenge_is_not_exchanged_with_a_verifier(
        self, api: TestClient
    ) -> None:
        # RFC 9700 §4.8.
        redirect = _consent(api, _authorize_params(CONFIDENTIAL_CLIENT, method=None))
        response = _token(
            api,
            {
                "grant_type": "authorization_code",
                "code": redirect["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": CONFIDENTIAL_CLIENT,
                "client_secret": CONFIDENTIAL_SECRET,
                "code_verifier": secrets.token_urlsafe(48),
            },
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
