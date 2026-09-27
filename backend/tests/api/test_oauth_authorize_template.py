"""Regression test for the OAuth /authorize TemplateResponse bug.

The route was calling Starlette's ``templates.TemplateResponse`` with the
legacy positional shape — ``TemplateResponse(name, context_with_request)``
— which newer Starlette versions interpret with ``request`` as the first
positional argument. With the legacy form, Starlette treats the dict as
the template name and Jinja2's cache lookup raises
``TypeError: unhashable type: 'dict'`` deep in the call stack, surfacing
to the client as a bare 500 "Internal Server Error".

This test mocks the session and sync DB dependencies so the authorize
handler reaches the ``TemplateResponse`` call and verifies it returns a 200
HTML response instead of crashing.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Match the sys.path layout the rest of the backend tests use.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from utils.oauth_messages import OAUTH_MESSAGES  # noqa: E402

_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _render_consent(
    registered: list[str], redirect_uri: str, locale: str = "en"
) -> tuple[int, str]:
    """Render the consent page for a public client with ``registered`` URIs."""
    from models.auth import OAuth2Client

    fake_user = MagicMock(email="test@example.com")
    fake_client = OAuth2Client(
        client_id="test",
        client_name="Some App",
        client_secret_hash="x",
        redirect_uris=registered,
        scope="memory:read memory:write",
        token_endpoint_auth_method="none",
        grant_types=["authorization_code"],
        response_types=["code"],
    )
    fake_db_user = MagicMock(locale=locale)

    with (
        patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
        patch("api.routes.oauth.get_sync_session") as mock_sess,
    ):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            fake_client,
            fake_db_user,
        ]
        mock_sess.return_value = db

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "test",
                    "redirect_uri": redirect_uri,
                    "state": "s",
                    "code_challenge": _CHALLENGE,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
    return response.status_code, response.text


class TestConsentRedirectHost:
    """The consent page names where the user is sent next (#1741)."""

    def test_shows_redirect_host(self):
        status, body = _render_consent(
            ["https://claude.ai/api/mcp/auth_callback"],
            "https://claude.ai/api/mcp/auth_callback",
        )
        assert status == 200, body[:300]
        assert OAUTH_MESSAGES["en"]["redirect_notice"] in body
        assert '<strong class="redirect-host">claude.ai</strong>' in body
        assert OAUTH_MESSAGES["en"]["loopback_warning_title"] not in body

    def test_loopback_only_client_shows_port_and_warning(self):
        status, body = _render_consent(
            ["http://127.0.0.1:8989/oauth/callback"],
            "http://127.0.0.1:51234/oauth/callback",
        )
        assert status == 200, body[:300]
        assert '<strong class="redirect-host">127.0.0.1:51234</strong>' in body
        assert OAUTH_MESSAGES["en"]["loopback_warning_title"] in body
        assert OAUTH_MESSAGES["en"]["loopback_warning_body"] in body

    def test_mixed_client_has_no_loopback_warning(self):
        status, body = _render_consent(
            ["https://claude.ai/api/mcp/auth_callback", "http://localhost:3118/callback"],
            "http://localhost:40000/callback",
        )
        assert status == 200, body[:300]
        assert '<strong class="redirect-host">localhost:40000</strong>' in body
        assert OAUTH_MESSAGES["en"]["loopback_warning_title"] not in body

    def test_japanese_strings(self):
        status, body = _render_consent(
            ["http://localhost:3118/callback"], "http://localhost:3118/callback", "ja"
        )
        assert status == 200, body[:300]
        assert OAUTH_MESSAGES["ja"]["redirect_notice"] in body
        assert OAUTH_MESSAGES["ja"]["loopback_warning_title"] in body
        assert OAUTH_MESSAGES["ja"]["loopback_warning_body"] in body

    def test_message_keys_match_between_locales(self):
        assert set(OAUTH_MESSAGES["en"]) == set(OAUTH_MESSAGES["ja"])


class TestOAuthAuthorizeTemplate:
    def test_authorize_renders_template_for_logged_in_user(self):
        """Regression for #205: TemplateResponse must accept request as
        the first positional arg, otherwise Jinja2 raises
        TypeError: unhashable type: 'dict' and the client sees 500."""
        fake_user = MagicMock(email="test@example.com")
        fake_client = MagicMock(
            client_name="Test Client",
            scope="memory:read memory:write",
            token_endpoint_auth_method="none",
        )
        fake_db_user = MagicMock(locale="en")

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.side_effect = [
                fake_client,
                fake_db_user,
            ]
            mock_sess.return_value = db

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "test",
                        "redirect_uri": "http://x",
                        "state": "s",
                        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                        "code_challenge_method": "S256",
                    },
                    follow_redirects=False,
                )

        assert response.status_code == 200, (
            f"Expected 200 HTML, got {response.status_code}: {response.text[:300]}"
        )
        assert response.headers.get("content-type", "").startswith("text/html")
        # Body should mention the client name from our mock.
        assert "Test Client" in response.text

    def test_authorize_redirects_to_login_when_no_session(self):
        """Sanity guard: the no-session redirect path still works."""
        with patch("api.routes.oauth.get_current_user_from_session", return_value=None):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "test",
                        "redirect_uri": "http://x",
                        "state": "s",
                    },
                    follow_redirects=False,
                )
        assert response.status_code in (302, 307)
        assert "/login" in response.headers.get("location", "")
