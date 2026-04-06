"""Regression test for the OAuth /authorize TemplateResponse bug.

The route was calling Starlette's ``templates.TemplateResponse`` with the
legacy positional shape — ``TemplateResponse(name, context_with_request)``
— which newer Starlette versions interpret with ``request`` as the first
positional argument. With the legacy form, Starlette treats the dict as
the template name and Jinja2's cache lookup raises
``TypeError: unhashable type: 'dict'`` deep in the call stack, surfacing
to the client as a bare 500 "Internal Server Error".

This test mocks the session, sync DB, and Redis dependencies so the
authorize handler reaches the ``TemplateResponse`` call and verifies it
returns a 200 HTML response instead of crashing.
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


class TestOAuthAuthorizeTemplate:
    def test_authorize_renders_template_for_logged_in_user(self):
        """Regression for #205: TemplateResponse must accept request as
        the first positional arg, otherwise Jinja2 raises
        TypeError: unhashable type: 'dict' and the client sees 500."""
        fake_user = MagicMock(email="test@example.com")
        fake_client = MagicMock(client_name="Test Client")
        fake_db_user = MagicMock(locale="en")

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
            patch("redis.Redis") as mock_redis,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.side_effect = [
                fake_client,
                fake_db_user,
            ]
            mock_sess.return_value = db
            mock_redis.from_url.return_value.setex = MagicMock()

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "test",
                        "redirect_uri": "http://x",
                        "state": "s",
                        "code_challenge": "abc",
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
