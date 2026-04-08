"""Regression tests for Issue #218: redirect_uri pre-check on /authorize.

The OAuth ``GET /authorize`` endpoint used to render the consent screen
*before* validating ``redirect_uri`` against the client's registered
patterns. An attacker could craft a link with a legitimate ``client_id``
and a hostile ``redirect_uri``; Kagura would render the real consent UI
(with the real client name) and only fail at POST time. The consent
screen itself was a phishing rendering gadget.

The matching ``POST /authorize`` flow had a related CWE-601 Open Redirect:
the deny path and both exception handlers used to 303-redirect to the
unvalidated ``redirect_uri`` from the query string, even when the GET
pre-check would now refuse to render the consent screen for the same URI.

PR #264 review (Copilot) also flagged a third bug in the same area: the
deny / OAuth2Error / generic-exception handlers all built their redirect
URL with ``f"{redirect_uri}?{urlencode(params)}"``, which produces a
malformed double-``?`` URL whenever the registered ``redirect_uri``
already carries a query string (RFC 6749 §3.1.2 + ``is_valid_redirect_uri_pattern``
allow this). The fix uses ``_append_query_params`` which merges with
``urlsplit``/``urlunsplit``.

These tests assert:

1. ``GET /authorize`` returns the error page (400) instead of the
   consent screen when ``redirect_uri`` is not registered.
2. ``POST /authorize`` (deny path) returns the error page instead of
   303-redirecting to an unregistered ``redirect_uri``.
3. ``POST /authorize`` exception handlers (``OAuth2Error`` and generic
   ``Exception``) redirect to the *registered* ``redirect_uri`` with
   error params — never to an attacker URI, since the upfront pre-check
   guarantees the URI is registered by the time the exception fires.
4. Percent-encoded path traversal sequences (``%2F``, ``%2E%2E``) are
   rejected — defence in depth on top of the matcher's own decoder.
5. The happy path still works (registered ``redirect_uri`` renders the
   consent screen, registered ``POST`` deny still redirects with
   ``error=access_denied``).
6. ``_append_query_params`` regression: a registered ``redirect_uri``
   carrying its own query string still produces a well-formed URL on
   the deny path (no double ``?``, original params preserved).
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


def _make_fake_client(*, accepts: bool, client_name: str = "Test Client"):
    """Build a MagicMock OAuth2Client with a deterministic check_redirect_uri."""
    fake_client = MagicMock(client_name=client_name)
    fake_client.check_redirect_uri = MagicMock(return_value=accepts)
    return fake_client


class TestGetAuthorizeRedirectUriPreCheck:
    """GET /authorize must reject unregistered redirect_uri before render."""

    def test_unregistered_redirect_uri_returns_error_page_not_consent(self):
        fake_user = MagicMock(email="test@example.com")
        fake_client = _make_fake_client(accepts=False)

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
            patch("redis.Redis") as mock_redis,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = fake_client
            mock_sess.return_value = db
            mock_redis.from_url.return_value.setex = MagicMock()

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "test-client",
                        "redirect_uri": "https://attacker.example/cb",
                        "state": "s",
                        "locale": "en",
                    },
                    follow_redirects=False,
                )

        assert response.status_code == 400, (
            f"Expected 400 error page, got {response.status_code}: {response.text[:300]}"
        )
        assert response.headers.get("content-type", "").startswith("text/html")
        # Error template marker — present.
        assert "Authorization Error" in response.text or "blocked" in response.text.lower()
        # Consent screen marker — must NOT be present (no rendering gadget).
        assert "Authorize</button>" not in response.text
        # The offending URI is shown to the user (Jinja-escaped).
        assert "https://attacker.example/cb" in response.text
        # check_redirect_uri was actually called.
        fake_client.check_redirect_uri.assert_called_with("https://attacker.example/cb")

    def test_registered_redirect_uri_still_renders_consent(self):
        """Happy path: registered redirect_uri produces the consent screen."""
        fake_user = MagicMock(email="test@example.com")
        fake_client = _make_fake_client(accepts=True, client_name="Legit App")

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
            patch("redis.Redis") as mock_redis,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = fake_client
            mock_sess.return_value = db
            mock_redis.from_url.return_value.setex = MagicMock()

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "test-client",
                        "redirect_uri": "https://legit.example/cb",
                        "state": "s",
                        "locale": "en",
                    },
                    follow_redirects=False,
                )

        assert response.status_code == 200
        assert "Legit App" in response.text


class TestGetAuthorizePercentEncodedTraversal:
    """Percent-encoded traversal must be rejected at the route level too."""

    def test_percent_encoded_slash_rejected(self):
        """A wildcard pattern allowing /cb/* must not accept %2F as a path
        separator inside the variable segment. Defence in depth: the
        matcher already unquotes before checking, but we assert it at the
        route level so a future regression in the matcher would still be
        caught here."""
        fake_user = MagicMock(email="test@example.com")
        # Use a real-ish OAuth2Client wired to the actual matcher.
        from models.auth import OAuth2Client

        real_client = OAuth2Client()
        real_client.client_name = "Wildcard App"
        real_client.redirect_uris = ["https://legit.example/cb/*"]

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
            patch("redis.Redis") as mock_redis,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = real_client
            mock_sess.return_value = db
            mock_redis.from_url.return_value.setex = MagicMock()

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "test-client",
                        # %2Fevil tries to smuggle a path separator inside
                        # the wildcard segment to pivot endpoints.
                        "redirect_uri": "https://legit.example/cb/foo%2Fevil",
                        "state": "s",
                        "locale": "en",
                    },
                    follow_redirects=False,
                )

        assert response.status_code == 400, (
            f"Percent-encoded slash must be rejected, got {response.status_code}"
        )

    def test_percent_encoded_dot_dot_rejected(self):
        fake_user = MagicMock(email="test@example.com")
        from models.auth import OAuth2Client

        real_client = OAuth2Client()
        real_client.client_name = "Wildcard App"
        real_client.redirect_uris = ["https://legit.example/cb/*"]

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
            patch("redis.Redis") as mock_redis,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = real_client
            mock_sess.return_value = db
            mock_redis.from_url.return_value.setex = MagicMock()

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "test-client",
                        "redirect_uri": "https://legit.example/cb/%2E%2E",
                        "state": "s",
                        "locale": "en",
                    },
                    follow_redirects=False,
                )

        assert response.status_code == 400


class TestPostAuthorizeRedirectUriPreCheck:
    """POST /authorize must not 303 to an unregistered redirect_uri."""

    def test_deny_with_unregistered_redirect_uri_returns_error_not_redirect(self):
        """CWE-601 regression: confirm=no must not 303 to attacker URI."""
        fake_user = MagicMock(email="test@example.com")
        fake_client = _make_fake_client(accepts=False)

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = fake_client
            mock_sess.return_value = db

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/oauth/authorize",
                    params={
                        "client_id": "test-client",
                        "redirect_uri": "https://attacker.example/cb",
                        "state": "s",
                        "locale": "en",
                    },
                    data={"confirm": "no"},
                    follow_redirects=False,
                )

        # Must NOT be a 303 redirect to attacker.example.
        assert response.status_code == 400, (
            f"Expected 400 error page, got {response.status_code}: "
            f"location={response.headers.get('location')!r}"
        )
        assert "attacker.example" not in (response.headers.get("location") or "")
        # Body should be the error template.
        assert response.headers.get("content-type", "").startswith("text/html")
        assert "https://attacker.example/cb" in response.text

    def test_deny_with_registered_redirect_uri_still_redirects(self):
        """Happy path: registered URI deny still 303s with access_denied."""
        fake_user = MagicMock(email="test@example.com")
        fake_client = _make_fake_client(accepts=True)

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = fake_client
            mock_sess.return_value = db

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/oauth/authorize",
                    params={
                        "client_id": "test-client",
                        "redirect_uri": "https://legit.example/cb",
                        "state": "s",
                        "locale": "en",
                    },
                    data={"confirm": "no"},
                    follow_redirects=False,
                )

        assert response.status_code == 303
        location = response.headers.get("location", "")
        assert location.startswith("https://legit.example/cb")
        assert "error=access_denied" in location
        assert "state=s" in location

    def test_post_with_missing_redirect_uri_returns_error_page(self):
        """Missing redirect_uri must hit the same error page (no 500, no
        ambiguous HTTPException JSON)."""
        fake_user = MagicMock(email="test@example.com")

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = None
            mock_sess.return_value = db

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/oauth/authorize",
                    params={
                        "client_id": "test-client",
                        "state": "s",
                        "locale": "en",
                    },
                    data={"confirm": "no"},
                    follow_redirects=False,
                )

        assert response.status_code == 400
        assert response.headers.get("content-type", "").startswith("text/html")


class TestPostAuthorizeExceptionBranches:
    """Exception handlers must operate only on validated redirect_uri.

    Because the upfront pre-check now refuses any unregistered URI, by
    the time ``OAuth2Error`` or a generic ``Exception`` fires, the URI
    is guaranteed registered. These tests pin that contract: the
    exception branches still 303 to the *registered* URI with proper
    OAuth error params (RFC 6749 §4.1.2.1) — never to an attacker URI.
    """

    def test_oauth2error_redirects_to_registered_uri_with_error_params(self):
        from authlib.oauth2.rfc6749.errors import InvalidScopeError

        fake_user = MagicMock(email="test@example.com")
        fake_client = _make_fake_client(accepts=True)

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
            patch(
                "api.routes.oauth._handle_authorize_sync",
                side_effect=InvalidScopeError(description="bad scope"),
            ),
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = fake_client
            mock_sess.return_value = db

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/oauth/authorize",
                    params={
                        "client_id": "test-client",
                        "redirect_uri": "https://legit.example/cb",
                        "state": "s",
                        "locale": "en",
                    },
                    data={"confirm": "yes"},
                    follow_redirects=False,
                )

        assert response.status_code == 303
        location = response.headers.get("location", "")
        assert location.startswith("https://legit.example/cb")
        assert "error=invalid_scope" in location
        assert "state=s" in location
        assert "attacker" not in location

    def test_generic_exception_redirects_to_registered_uri_with_server_error(self):
        fake_user = MagicMock(email="test@example.com")
        fake_client = _make_fake_client(accepts=True)

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
            patch(
                "api.routes.oauth._handle_authorize_sync",
                side_effect=RuntimeError("boom"),
            ),
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = fake_client
            mock_sess.return_value = db

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/oauth/authorize",
                    params={
                        "client_id": "test-client",
                        "redirect_uri": "https://legit.example/cb",
                        "state": "s",
                        "locale": "en",
                    },
                    data={"confirm": "yes"},
                    follow_redirects=False,
                )

        assert response.status_code == 303
        location = response.headers.get("location", "")
        assert location.startswith("https://legit.example/cb")
        assert "error=server_error" in location
        assert "state=s" in location


class TestAppendQueryParamsRegression:
    """Regression: registered redirect_uri carrying its own query string
    must not produce a malformed double-``?`` URL.

    RFC 6749 §3.1.2 explicitly allows query strings on exact-match
    redirect URIs, and ``is_valid_redirect_uri_pattern`` accepts them,
    so this is a real production scenario, not a hypothetical."""

    def test_deny_with_query_string_in_registered_uri_merges_correctly(self):
        from urllib.parse import parse_qs, urlsplit

        fake_user = MagicMock(email="test@example.com")
        fake_client = _make_fake_client(accepts=True)

        with (
            patch("api.routes.oauth.get_current_user_from_session", return_value=fake_user),
            patch("api.routes.oauth.get_sync_session") as mock_sess,
        ):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = fake_client
            mock_sess.return_value = db

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/oauth/authorize",
                    params={
                        "client_id": "test-client",
                        "redirect_uri": "https://legit.example/cb?env=prod&v=1",
                        "state": "s",
                        "locale": "en",
                    },
                    data={"confirm": "no"},
                    follow_redirects=False,
                )

        assert response.status_code == 303
        location = response.headers.get("location", "")

        # Hard guard against the regression: exactly one '?' in the URL.
        assert location.count("?") == 1, f"double-? regression: {location}"

        # Original query params preserved AND new ones appended.
        parts = urlsplit(location)
        qs = parse_qs(parts.query)
        assert qs.get("env") == ["prod"]
        assert qs.get("v") == ["1"]
        assert qs.get("error") == ["access_denied"]
        assert qs.get("state") == ["s"]
