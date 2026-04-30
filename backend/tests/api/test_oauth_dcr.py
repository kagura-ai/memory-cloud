"""Tests for OAuth Dynamic Client Registration (issue #513).

Covers:
1. ``detect_dcr_provider`` helper — provider detection from redirect_uri + client_name
   - Loopback redirects (RFC 8252): localhost / 127.0.0.1 / [::1] with recognized
     client_name keyword (claude / cursor / chatgpt) are accepted.
   - Hostname suffix match (claude.ai / chatgpt.com / cursor.sh / etc.) keeps
     pre-existing providers working and structurally rejects substring spoofs
     (``https://attacker.com/?fake=chatgpt.com``) that the prior substring-match
     allowed.
2. ``POST /api/v1/oauth/register`` endpoint integration:
   - Rejection responses follow RFC 6749 §5.2 / RFC 7591 §3.2.2 format
     ``{"error": "...", "error_description": "..."}`` instead of FastAPI's
     default ``{"detail": "..."}``.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Match the sys.path layout the rest of the backend tests use.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api.routes.oauth import OAuth2ClientResponse, detect_dcr_provider  # noqa: E402


class TestDetectDcrProvider:
    """Unit tests for the ``detect_dcr_provider`` helper."""

    @pytest.mark.parametrize(
        ("redirect_uri", "client_name", "expected"),
        [
            # AC #5 (a): loopback + recognized client_name → accepted
            ("http://localhost:8080/callback", "Claude Code", "claude"),
            ("http://localhost/callback", "Claude Code", "claude"),
            ("http://127.0.0.1:54321/cb", "Cursor", "cursor"),
            ("http://[::1]:8080/callback", "claude", "claude"),
            ("http://localhost:9999/cb", "ChatGPT MCP", "chatgpt"),
            # case-insensitivity
            ("http://localhost/cb", "CLAUDE CODE", "claude"),
            ("http://localhost/cb", "cUrSoR", "cursor"),
        ],
    )
    def test_loopback_with_recognized_name_returns_provider(
        self, redirect_uri: str, client_name: str, expected: str
    ) -> None:
        assert detect_dcr_provider(redirect_uri, client_name) == expected

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "http://localhost:8080/cb",
            "http://127.0.0.1:8080/cb",
            "http://[::1]:8080/cb",
        ],
    )
    def test_loopback_with_unknown_name_returns_custom(self, redirect_uri: str) -> None:
        assert detect_dcr_provider(redirect_uri, "MyApp") == "custom"
        assert detect_dcr_provider(redirect_uri, "RandomTool") == "custom"

    def test_loopback_fullwidth_homoglyph_in_client_name_normalized(self) -> None:
        # Fullwidth Latin letters (U+FF21..U+FF5A) are visually identical to
        # ASCII letters but a naive substring("claude", name) miss. NFKC
        # collapses them to ASCII before the keyword check.
        fullwidth_claude = "Ｃｌａｕｄｅ"  # "Ｃｌａｕｄｅ"
        assert detect_dcr_provider("http://localhost/cb", fullwidth_claude) == "claude"

    def test_loopback_zwsp_in_client_name_stripped(self) -> None:
        # Zero-width space (U+200B, category Cf) is invisible but NFKC alone
        # leaves it intact. Strip Cf/Cc characters so ``Cl​aude`` still
        # matches the ``claude`` keyword.
        zwsp = "​"
        client_name = f"Cl{zwsp}aude"
        assert detect_dcr_provider("http://localhost/cb", client_name) == "claude"

    @pytest.mark.parametrize(
        ("redirect_uri", "expected"),
        [
            # AC #5 (d): existing patterns still accepted (regression).
            # Note: detection is by hostname (suffix-match), not substring.
            ("https://chatgpt.com/connector_platform_oauth_redirect", "chatgpt"),
            ("https://chat.openai.com/cb", "chatgpt"),
            ("https://chat.openai.com/aiserver/connector/oauth/abc123", "chatgpt"),
            ("https://claude.ai/api/mcp/auth_callback", "claude"),
            ("https://anthropic.com/oauth/callback", "claude"),
            ("https://cursor.sh/oauth/callback", "cursor"),
            ("https://cursor.com/api/oauth", "cursor"),
            # Subdomains of recognized hostnames are accepted.
            ("https://app.claude.ai/cb", "claude"),
            ("https://api.cursor.com/cb", "cursor"),
        ],
    )
    def test_hostname_match_keeps_existing_providers(
        self, redirect_uri: str, expected: str
    ) -> None:
        # client_name is irrelevant for non-loopback paths.
        assert detect_dcr_provider(redirect_uri, "ChatGPT MCP") == expected
        assert detect_dcr_provider(redirect_uri, "anything-else") == expected

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            # AC #5 (c): non-loopback custom URLs return custom (caller rejects).
            "https://attacker.com/cb",
            "https://example.com/oauth/cb",
            # Substring-spoof attempts that the OLD substring matcher would have
            # accepted but the new hostname-based matcher correctly rejects.
            # This is a security upgrade pinned by the test.
            "https://attacker.com/?fake=chatgpt.com",
            "https://attacker.com/path/claude.ai",
            "https://chatgpt.com.attacker.com/cb",
            "https://localhost.evil.com/cb",  # NOT a loopback, just hostname trick.
        ],
    )
    def test_non_loopback_unrecognized_returns_custom(self, redirect_uri: str) -> None:
        assert detect_dcr_provider(redirect_uri, "Claude Code") == "custom"

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            # https:// loopback is unusual (TLS to self) and not what RFC 8252
            # describes — only http loopback is the native-app convention.
            "https://localhost/cb",
            "https://127.0.0.1/cb",
            # Other schemes
            "ftp://localhost/cb",
            "myapp://localhost/cb",
        ],
    )
    def test_non_http_loopback_does_not_use_client_name_path(self, redirect_uri: str) -> None:
        # These are not RFC 8252 loopback URIs (wrong scheme), so the
        # client_name fallback must NOT activate.
        assert detect_dcr_provider(redirect_uri, "Claude Code") == "custom"

    def test_empty_redirect_uri_returns_custom(self) -> None:
        assert detect_dcr_provider("", "Claude Code") == "custom"

    def test_malformed_redirect_uri_returns_custom(self) -> None:
        # urlparse is permissive but a totally broken string should still
        # default to custom rather than raising.
        assert detect_dcr_provider("not a url", "Claude Code") == "custom"


def _patch_dcr_dependencies():
    """Build the patch context for DCR endpoint tests.

    DCR's success path needs a sync DB session, encryption, and the rate-limit
    counter. Rejection paths short-circuit before the DB is touched, so they
    only need the rate-limit counter mocked. Each test composes the minimum it
    needs; this helper just centralizes the mock wiring.
    """
    rate_limit = AsyncMock(return_value=1)

    fake_session = MagicMock()

    # Make refresh() populate the client.id so the response model can build.
    def _refresh(client):
        if client.id is None:
            client.id = 12345

    fake_session.refresh.side_effect = _refresh

    fake_encryptor = MagicMock()
    # APIKeyEncryption.encrypt() returns a base64-encoded ``str`` in production,
    # not ``bytes`` (and ``OAuth2Client.plaintext_secret_encrypted`` is a
    # SQLAlchemy ``String`` column). Match the production type so the mock
    # doesn't mask serialization issues.
    fake_encryptor.encrypt = MagicMock(return_value="encrypted-secret-blob-base64")

    return rate_limit, fake_session, fake_encryptor


class TestDcrEndpointRejection:
    """Integration tests for ``/api/v1/oauth/register`` rejection paths.

    All rejection responses must use RFC 6749 §5.2 / RFC 7591 §3.2.2 format:

        {"error": "<code>", "error_description": "<human readable>"}

    Specifically NOT FastAPI's default ``{"detail": "..."}``.
    """

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            yield c

    @pytest.mark.parametrize(
        ("redirect_uri", "client_name", "case_label"),
        [
            # AC #5 (b): loopback + unrecognized client_name.
            ("http://localhost:8080/cb", "MyRandomApp", "loopback_unknown_name"),
            # AC #5 (c): non-loopback custom URL.
            ("https://attacker.com/cb", "Claude Code", "non_loopback_custom"),
            # Substring-spoof regression: the OLD provider detection accepted
            # this because ``"chatgpt.com" in redirect_uri`` matched the query
            # string, but the new hostname-based matcher correctly rejects it.
            (
                "https://attacker.com/?fake=chatgpt.com",
                "ChatGPT MCP",
                "substring_spoof",
            ),
        ],
    )
    def test_provider_rejected_uses_rfc6749_envelope(
        self, client, redirect_uri: str, client_name: str, case_label: str
    ):
        rate_limit, _, _ = _patch_dcr_dependencies()
        with patch("db.redis.increment_counter", rate_limit):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": client_name,
                    "redirect_uris": [redirect_uri],
                    "token_endpoint_auth_method": "none",
                },
            )

        assert response.status_code == 400, f"case={case_label}: {response.text}"
        body = response.json()
        assert body.get("error") == "invalid_client_metadata", (
            f"case={case_label}: expected RFC 6749 'error' field, got {body!r}"
        )
        assert "error_description" in body, f"case={case_label}: {body!r}"
        # FastAPI's default ``{"detail": "..."}`` envelope must not appear —
        # SDKs that schema-validate against RFC 6749 fields will trip on it.
        assert "detail" not in body, f"case={case_label}: {body!r}"

    def test_rate_limit_returns_rfc6749_format(self, client):
        """6th request from the same IP returns 429 with RFC 6749 envelope.

        RFC 6749 §5.2 only enumerates 400-class error codes; 429 is outside
        the spec's strict scope but we still use the RFC envelope shape so a
        single SDK parser handles every DCR rejection. The error code follows
        OAuth 2.0 Authorization Server Metadata convention by using
        ``invalid_request`` as the closest fit (RFC 6749 §5.2 enumerates it
        for the 400 case; reusing it for 429 is a deliberate consistency
        choice — the human-readable ``error_description`` carries the
        rate-limit specifics).
        """
        rate_limit_over = AsyncMock(return_value=6)
        with patch("db.redis.increment_counter", rate_limit_over):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": "ChatGPT MCP",
                    "redirect_uris": ["https://chatgpt.com/cb"],
                    "token_endpoint_auth_method": "none",
                },
            )

        assert response.status_code == 429
        body = response.json()
        assert body.get("error") == "invalid_request"
        assert "error_description" in body


class TestDcrEndpointAcceptance:
    """Integration tests for ``/api/v1/oauth/register`` happy paths."""

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            yield c

    @pytest.mark.parametrize(
        ("redirect_uri", "client_name", "expected_provider"),
        [
            # AC #5 (a) loopback variants
            ("http://localhost:8080/callback", "Claude Code", "claude"),
            ("http://127.0.0.1:54321/cb", "Cursor", "cursor"),
            # AC #5 (e) IPv6 loopback
            ("http://[::1]:8080/callback", "claude", "claude"),
            # AC #5 (d) regression for existing patterns
            ("https://chatgpt.com/connector_platform_oauth_redirect", "ChatGPT MCP", "chatgpt"),
            ("https://claude.ai/cb", "Claude.ai", "claude"),
            ("https://anthropic.com/oauth/callback", "Anthropic", "claude"),
            ("https://cursor.sh/oauth/callback", "Cursor", "cursor"),
            ("https://cursor.com/api/oauth", "Cursor", "cursor"),
        ],
    )
    def test_accepted_returns_201(
        self, client, redirect_uri: str, client_name: str, expected_provider: str
    ):
        rate_limit, fake_session, fake_encryptor = _patch_dcr_dependencies()
        with (
            patch("db.redis.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
        ):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": client_name,
                    "redirect_uris": [redirect_uri],
                    "token_endpoint_auth_method": "none",
                },
            )

        assert response.status_code == 201, (
            f"expected 201 for {redirect_uri!r} + {client_name!r}, got "
            f"{response.status_code}: {response.text}"
        )
        body = response.json()
        assert body.get("provider") == expected_provider
        assert body.get("token_endpoint_auth_method") == "none"
        assert "client_secret" in body
        # DB write was attempted
        fake_session.add.assert_called_once()
        fake_session.commit.assert_called_once()


class TestOAuth2ClientResponseOwnerIdSerialization:
    """Pin that ``OAuth2ClientResponse.owner_id: str | None`` keeps both paths working.

    Issue #513 widened the ``owner_id`` type from ``str`` to ``str | None`` so
    DCR-registered clients (which have no owner) can serialize. This test class
    pins both paths so a future tightening of the field back to ``str`` (or to
    a different type) is caught immediately:

    - Admin-managed clients (``POST /api/v1/oauth/clients/*``) always set
      ``owner_id`` to the creating user's id — must continue to round-trip.
    - DCR-registered clients (``POST /api/v1/oauth/register``) set
      ``owner_id=None`` — the new behavior, must serialize to ``null``.
    """

    @staticmethod
    def _common_fields() -> dict:
        """Field skeleton shared by both admin and DCR client responses."""
        return {
            "id": 42,
            "client_id": "oauth_test_client_id",
            "redirect_uris": ["https://chatgpt.com/cb"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "memory:read memory:write",
            "provider": "chatgpt",
            "created_at": "2026-04-30T00:00:00Z",
            "plaintext_secret": None,
            "is_visible": False,
            "visibility_expires_at": None,
        }

    def test_admin_managed_client_with_string_owner_id(self):
        response = OAuth2ClientResponse(
            **self._common_fields(),
            client_name="Admin-Created Client",
            token_endpoint_auth_method="client_secret_post",
            owner_id="user-12345",
        )
        assert response.owner_id == "user-12345"
        # Round-trip via model_dump to confirm serializer accepts the value.
        assert response.model_dump()["owner_id"] == "user-12345"

    def test_dcr_registered_client_with_none_owner_id(self):
        response = OAuth2ClientResponse(
            **self._common_fields(),
            client_name="DCR-Registered Client",
            token_endpoint_auth_method="none",
            owner_id=None,
        )
        assert response.owner_id is None
        assert response.model_dump()["owner_id"] is None
