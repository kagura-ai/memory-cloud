"""The Microsoft 365 Copilot client's redirect URI matches exactly (#2778).

The client Microsoft uses is a manual confidential client created with
``POST /api/v1/oauth/clients`` and registered with Teams' OAuth redirect URI
(docs/microsoft-365-copilot.md). The create request keeps that URI as given,
and ``/authorize`` accepts exactly that URI: no other path, query, scheme,
host or trailing slash. The whole flow for a user of another workspace runs
on PostgreSQL in ``tests/integration/test_oauth_cross_workspace_client_db.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api.routes.oauth import OAuth2ClientCreateRequest  # noqa: E402
from models.auth import OAuth2Client  # noqa: E402

TEAMS_REDIRECT_URI = "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect"

NOT_ACCEPTED = [
    TEAMS_REDIRECT_URI + "/",
    TEAMS_REDIRECT_URI + "/other",
    TEAMS_REDIRECT_URI + "?next=https://example.com",
    TEAMS_REDIRECT_URI + "#fragment",
    TEAMS_REDIRECT_URI.replace("https://", "http://"),
    TEAMS_REDIRECT_URI.replace("oAuthRedirect", "oauthredirect"),
    TEAMS_REDIRECT_URI.replace("teams.microsoft.com", "teams.microsoft.com.example.com"),
    TEAMS_REDIRECT_URI.replace("teams.microsoft.com", "example.com"),
    "https://teams.microsoft.com:8443/api/platform/v1.0/oAuthRedirect",
    "https://teams.microsoft.com/api/platform/v1.0/",
    "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect%2F..%2Fother",
]


def _client() -> OAuth2Client:
    request = OAuth2ClientCreateRequest(
        client_name="Microsoft 365 Copilot", redirect_uris=[TEAMS_REDIRECT_URI]
    )
    assert request.redirect_uris == [TEAMS_REDIRECT_URI]  # kept as given
    assert request.token_endpoint_auth_method == "client_secret_post"  # confidential
    return OAuth2Client(
        client_id="oauth_microsoft",
        client_secret_hash="0" * 64,
        client_name=request.client_name,
        redirect_uris=request.redirect_uris,
        grant_types=request.grant_types,
        response_types=request.response_types,
        scope=request.scope,
        token_endpoint_auth_method=request.token_endpoint_auth_method,
        provider=request.provider,
        owner_id="operator",
    )


def test_the_client_accepts_the_teams_uri_and_nothing_broader():
    client = _client()
    assert client.check_redirect_uri(TEAMS_REDIRECT_URI)
    assert [uri for uri in NOT_ACCEPTED if client.check_redirect_uri(uri)] == []


def _authorize(redirect_uri: str):
    user = MagicMock(user_id="user-b", email="b@example.test", session_id="sid-b")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = _client()
    with (
        patch("api.routes.oauth.get_current_user_from_session", return_value=user),
        patch("api.routes.oauth.get_sync_session", return_value=db),
        patch(
            "api.routes.oauth._consent_default_context_choices",
            AsyncMock(return_value=([], None)),
        ),
    ):
        return TestClient(app, base_url="https://testserver").get(
            "/api/v1/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "oauth_microsoft",
                "redirect_uri": redirect_uri,
                "state": "s",
                "scope": "memory:read memory:write",
                "locale": "en",
            },
            follow_redirects=False,
        )


def test_the_teams_uri_gets_the_consent_page():
    response = _authorize(TEAMS_REDIRECT_URI)
    assert response.status_code == 200, response.text[:300]
    assert "Microsoft 365 Copilot" in response.text


@pytest.mark.parametrize("redirect_uri", NOT_ACCEPTED)
def test_any_other_uri_gets_the_error_page(redirect_uri):
    response = _authorize(redirect_uri)
    assert response.status_code == 400, response.text[:300]
    assert "location" not in response.headers
    assert "Microsoft 365 Copilot" not in response.text
