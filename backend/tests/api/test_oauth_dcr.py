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
from auth.mcp_scopes import DCR_DEFAULT_SCOPE  # noqa: E402


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

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            # urlparse extracts hostname="chatgpt.com" but port is malformed.
            # Without the port-parse guard, this would spoof "chatgpt".
            "https://chatgpt.com:443.evil.com/cb",
            "https://claude.ai:80.attacker.com/cb",
            # Port out of range — also raises ValueError on parsed.port.
            "https://chatgpt.com:99999/cb",
            # Userinfo — not legitimate for any DCR provider.
            "https://attacker@chatgpt.com/cb",
            "https://user:pass@claude.ai/cb",
        ],
    )
    def test_malformed_authority_rejected(self, redirect_uri: str) -> None:
        # Pin the security upgrade: ``urlparse`` extracts a clean hostname
        # from these inputs, but the explicit port-parse + userinfo check
        # blocks them. Without the guards, the substring/suffix match on
        # the host alone would re-introduce provider spoofing.
        assert detect_dcr_provider(redirect_uri, "ChatGPT MCP") == "custom"
        assert detect_dcr_provider(redirect_uri, "Claude Code") == "custom"


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
        with patch("api.routes.oauth.increment_counter", rate_limit):
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
        # RFC 6749 §5.1/§5.2 mandates Cache-Control: no-store + Pragma: no-cache
        # so intermediaries don't cache OAuth error envelopes.
        assert response.headers.get("cache-control") == "no-store", (
            f"case={case_label}: missing/wrong Cache-Control"
        )
        assert response.headers.get("pragma") == "no-cache", (
            f"case={case_label}: missing/wrong Pragma"
        )

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
        with patch("api.routes.oauth.increment_counter", rate_limit_over):
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
            patch("api.routes.oauth.increment_counter", rate_limit),
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
        # RFC 7591 §3.2.1 (Issue #689): public clients
        # (``token_endpoint_auth_method="none"``) MUST NOT receive a
        # ``client_secret``. Returning one made Claude Code's OAuth2 stack
        # treat the registration as a confidential client and Basic-Auth the
        # token endpoint, which authlib then rejected with invalid_client 401.
        #
        # Pin field *absence* (not just `null`): some OAuth client libraries
        # treat ``{"client_secret": null}`` as "confidential client with empty
        # secret" and still Basic-Auth the token endpoint. ``response_model_exclude``
        # makes the field absent from JSON, which is the load-bearing fix.
        assert "client_secret" not in body, (
            f"RFC 7591 §3.2.1 violation: public client response includes "
            f"client_secret={body.get('client_secret')!r}"
        )
        assert "plaintext_secret" not in body, (
            f"Visibility plaintext must also be omitted for public clients "
            f"(got {body.get('plaintext_secret')!r})"
        )
        assert body.get("is_visible") is False
        assert body.get("visibility_expires_at") is None
        # The DB row stores an empty sentinel hash; no encryptor invocation.
        added_client = fake_session.add.call_args[0][0]
        assert added_client.client_secret_hash == ""
        assert added_client.plaintext_secret_encrypted is None
        fake_session.add.assert_called_once()
        fake_session.commit.assert_called_once()
        fake_encryptor.encrypt.assert_not_called()


# --- Issue #1657: loopback-only native MCP clients -----------------------
#
# Codex CLI, Hermes Agent and OpenClaw register through DCR with a loopback
# redirect and a ``client_name`` outside the original chatgpt/claude/cursor
# keywords. The bodies below mirror what each client actually sends.

_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

_CODEX_BODY = {
    "client_name": "Codex",
    "redirect_uris": ["http://127.0.0.1:53682/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}
_HERMES_BROWSER_BODY = {
    "client_name": "Hermes Agent",
    "redirect_uris": ["http://127.0.0.1:8420/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
    "application_type": "native",
}
_HERMES_DEVICE_BODY = {
    **_HERMES_BROWSER_BODY,
    "grant_types": [_DEVICE_CODE_GRANT, "refresh_token"],
    "response_types": [],
}
_OPENCLAW_BODY = {
    "client_name": "OpenClaw MCP",
    "redirect_uris": [
        "http://127.0.0.1:8989/oauth/callback",
        "http://localhost:8989/oauth/callback",
    ],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


class TestDetectDcrProviderLoopbackClients:
    """``detect_dcr_provider`` for the loopback-only clients (#1657)."""

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1:53682", "localhost:53682", "[::1]:53682", "127.0.0.1", "localhost"],
    )
    @pytest.mark.parametrize(
        ("client_name", "expected"),
        [
            ("Codex", "codex"),
            ("Hermes Agent", "hermes"),
            ("OpenClaw MCP", "openclaw"),
            # case-insensitivity and NFKC folding apply to the new keywords too
            ("CODEX", "codex"),
            ("ｈｅｒｍｅｓ", "hermes"),
        ],
    )
    def test_loopback_new_clients_detected(self, host: str, client_name: str, expected: str):
        assert detect_dcr_provider(f"http://{host}/callback", client_name) == expected

    @pytest.mark.parametrize(
        ("client_name", "expected"),
        [
            # A name holding both a pre-#1657 keyword and a new one keeps the
            # pre-#1657 result: the old keywords are checked first.
            ("Claude Codex", "claude"),
            ("Codex for Cursor", "cursor"),
            ("ChatGPT Hermes bridge", "chatgpt"),
            ("OpenClaw Claude", "claude"),
        ],
    )
    def test_name_matching_old_and_new_keyword_keeps_old_provider(
        self, client_name: str, expected: str
    ):
        assert detect_dcr_provider("http://127.0.0.1:8080/cb", client_name) == expected

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "https://codex.example.com/callback",
            "https://example.com/codex/callback",
            "https://hermes.example.org/cb",
            "https://openclaw.example.net/oauth/callback",
            "https://localhost/callback",  # https loopback is not RFC 8252
        ],
    )
    @pytest.mark.parametrize("client_name", ["Codex", "Hermes Agent", "OpenClaw MCP"])
    def test_new_names_on_non_loopback_redirect_return_custom(
        self, redirect_uri: str, client_name: str
    ):
        # The new names exist only on the loopback path: there is no hostname
        # to match, so a non-loopback redirect stays rejected as before.
        assert detect_dcr_provider(redirect_uri, client_name) == "custom"

    def test_allowed_providers_cover_every_detected_provider(self):
        # Every hostname and exact-redirect provider is allowed, every hostname
        # provider is also a loopback keyword, and the pre-#1657 keywords stay
        # ahead of the new ones. The exact-redirect providers (Gemini, #2222)
        # must NOT be loopback keywords: a loopback registration naming them
        # in ``client_name`` stays refused.
        from api.routes.oauth import (
            _DCR_ALLOWED_PROVIDERS,
            _LOOPBACK_PROVIDER_KEYWORDS,
            _PROVIDER_EXACT_REDIRECTS,
            _PROVIDER_HOSTNAMES,
        )

        assert set(_PROVIDER_HOSTNAMES) | set(_PROVIDER_EXACT_REDIRECTS) <= set(
            _DCR_ALLOWED_PROVIDERS
        )
        assert set(_PROVIDER_HOSTNAMES) <= set(_LOOPBACK_PROVIDER_KEYWORDS)
        assert _LOOPBACK_PROVIDER_KEYWORDS[:3] == ("chatgpt", "claude", "cursor")
        assert "gemini" not in _LOOPBACK_PROVIDER_KEYWORDS
        assert "gemini" not in _PROVIDER_HOSTNAMES
        assert "codex" not in _PROVIDER_HOSTNAMES
        assert "hermes" not in _PROVIDER_HOSTNAMES
        assert "openclaw" not in _PROVIDER_HOSTNAMES


class TestDcrEndpointLoopbackClients:
    """``POST /api/v1/oauth/register`` with each client's real body (#1657)."""

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            yield c

    @staticmethod
    def _register(client, body: dict):
        rate_limit, fake_session, fake_encryptor = _patch_dcr_dependencies()
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
        ):
            response = client.post("/api/v1/oauth/register", json=body)
        return response, fake_session

    @pytest.mark.parametrize(
        ("body", "expected_provider"),
        [
            (_CODEX_BODY, "codex"),
            (_HERMES_BROWSER_BODY, "hermes"),
            (_HERMES_DEVICE_BODY, "hermes"),
            (_OPENCLAW_BODY, "openclaw"),
        ],
        ids=["codex", "hermes-browser", "hermes-device", "openclaw"],
    )
    def test_real_registration_body_returns_201(self, client, body: dict, expected_provider: str):
        response, fake_session = self._register(client, body)

        assert response.status_code == 201, response.text
        resp = response.json()
        assert resp["provider"] == expected_provider
        assert resp["client_name"] == body["client_name"]
        assert resp["redirect_uris"] == body["redirect_uris"]
        assert resp["token_endpoint_auth_method"] == "none"
        assert "client_secret" not in resp

        stored = fake_session.add.call_args[0][0]
        assert stored.provider == expected_provider
        assert stored.owner_id is None
        assert stored.token_endpoint_auth_method == "none"
        assert stored.grant_types == body["grant_types"]
        assert stored.response_types == body["response_types"]

    def test_hermes_device_flow_keeps_device_code_grant(self, client):
        response, fake_session = self._register(client, _HERMES_DEVICE_BODY)

        assert response.status_code == 201, response.text
        assert response.json()["grant_types"] == [_DEVICE_CODE_GRANT, "refresh_token"]
        assert response.json()["response_types"] == []
        stored = fake_session.add.call_args[0][0]
        assert stored.grant_types == [_DEVICE_CODE_GRANT, "refresh_token"]
        assert stored.response_types == []

    def test_openclaw_localhost_retry_body_returns_201(self, client):
        # OpenClaw retries once with the localhost redirect alone.
        body = {**_OPENCLAW_BODY, "redirect_uris": ["http://localhost:8989/oauth/callback"]}
        response, _ = self._register(client, body)

        assert response.status_code == 201, response.text
        assert response.json()["provider"] == "openclaw"

    @pytest.mark.parametrize("client_name", ["Codex", "Hermes Agent", "OpenClaw MCP"])
    def test_new_names_on_non_loopback_redirect_rejected(self, client, client_name: str):
        rate_limit, fake_session, _ = _patch_dcr_dependencies()
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
        ):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": client_name,
                    "redirect_uris": ["https://codex.example.com/callback"],
                    "token_endpoint_auth_method": "none",
                },
            )

        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_client_metadata"
        fake_session.add.assert_not_called()

    def test_rejection_description_lists_loopback_clients(self, client):
        rate_limit, _, _ = _patch_dcr_dependencies()
        with patch("api.routes.oauth.increment_counter", rate_limit):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": "MyRandomApp",
                    "redirect_uris": ["http://127.0.0.1:8080/callback"],
                    "token_endpoint_auth_method": "none",
                },
            )

        assert response.status_code == 400
        description = response.json()["error_description"]
        for label in ("ChatGPT", "Claude", "Cursor", "Codex", "Hermes Agent", "OpenClaw"):
            assert label in description, f"{label!r} missing from {description!r}"
        for provider in ("codex", "hermes", "openclaw"):
            assert provider in description


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


# --- Issue #1686 / #2232: every redirect_uri is checked -----------------------


def _warnings(fake_logger, event: str) -> list:
    """The ``logger.warning`` calls of one event name."""
    return [c for c in fake_logger.warning.call_args_list if c.args[0] == event]


class TestDcrEveryRedirectUri:
    """Each entry of ``redirect_uris`` passes provider detection on its own (#1686).

    Entries that fail are left out of the registration; a registration is
    refused only when none passes (#2232).
    """

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            yield c

    def _register(self, client, body: dict):
        rate_limit, fake_session, fake_encryptor = _patch_dcr_dependencies()
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
            patch("api.routes.oauth.logger") as fake_logger,
        ):
            response = client.post("/api/v1/oauth/register", json=body)
        return response, fake_session, fake_logger

    @pytest.mark.parametrize(
        ("client_name", "redirect_uris", "expected_provider"),
        [
            # Claude's connector callback.
            ("Claude", ["https://claude.ai/api/mcp/auth_callback"], "claude"),
            (
                "ChatGPT",
                [
                    "https://chatgpt.com/connector_platform_oauth_redirect",
                    "https://chat.openai.com/connector_platform_oauth_redirect",
                ],
                "chatgpt",
            ),
            ("Claude Code", ["http://localhost:53682/callback"], "claude"),
            (
                "Claude Code",
                ["http://localhost:53682/callback", "http://127.0.0.1:53682/callback"],
                "claude",
            ),
            # A provider host and a loopback entry of the same provider.
            (
                "Claude",
                ["https://claude.ai/api/mcp/auth_callback", "http://localhost:9000/callback"],
                "claude",
            ),
            # Entries of two different allowed providers: the first one's is stored.
            (
                "Claude",
                [
                    "https://claude.ai/api/mcp/auth_callback",
                    "https://chatgpt.com/connector_platform_oauth_redirect",
                ],
                "claude",
            ),
            # Both Claude hosts, and both ChatGPT hosts, in one registration.
            (
                "Claude",
                [
                    "https://claude.ai/api/mcp/auth_callback",
                    "https://claude.com/api/mcp/auth_callback",
                ],
                "claude",
            ),
            (
                "ChatGPT",
                [
                    "https://chatgpt.com/connector_platform_oauth_redirect",
                    "https://platform.openai.com/connector_platform_oauth_redirect",
                ],
                "chatgpt",
            ),
        ],
    )
    def test_every_entry_allowed_is_registered(
        self, client, client_name: str, redirect_uris: list[str], expected_provider: str
    ):
        response, fake_session, fake_logger = self._register(
            client, {"client_name": client_name, "redirect_uris": redirect_uris}
        )

        assert response.status_code == 201, response.text
        assert response.json()["provider"] == expected_provider
        assert response.json()["redirect_uris"] == redirect_uris
        fake_session.add.assert_called_once()
        assert fake_session.add.call_args.args[0].redirect_uris == redirect_uris
        assert _warnings(fake_logger, "dcr_redirect_uris_dropped") == []

    @pytest.mark.parametrize(
        ("client_name", "redirect_uris", "kept", "expected_provider"),
        [
            (
                "Claude",
                ["https://claude.ai/api/mcp/auth_callback", "https://attacker.example/cb"],
                ["https://claude.ai/api/mcp/auth_callback"],
                "claude",
            ),
            (
                "Claude Code",
                ["http://localhost:53682/callback", "https://attacker.example/cb"],
                ["http://localhost:53682/callback"],
                "claude",
            ),
            # The loopback entry needs a recognized client_name of its own, so it
            # is the one dropped.
            (
                "Anthropic",
                ["https://claude.ai/api/mcp/auth_callback", "http://localhost:9/cb"],
                ["https://claude.ai/api/mcp/auth_callback"],
                "claude",
            ),
            (
                "ChatGPT",
                ["https://chatgpt.com/cb", "https://attacker.example/?fake=chatgpt.com"],
                ["https://chatgpt.com/cb"],
                "chatgpt",
            ),
            # The provider is the first passing entry's, not the first entry's.
            (
                "Claude",
                ["https://attacker.example/cb", "https://claude.ai/api/mcp/auth_callback"],
                ["https://claude.ai/api/mcp/auth_callback"],
                "claude",
            ),
            # Two allowed providers around a refused entry: both kept, in order.
            (
                "Claude",
                [
                    "https://attacker.example/cb",
                    "https://claude.ai/api/mcp/auth_callback",
                    "https://chatgpt.com/connector_platform_oauth_redirect",
                ],
                [
                    "https://claude.ai/api/mcp/auth_callback",
                    "https://chatgpt.com/connector_platform_oauth_redirect",
                ],
                "claude",
            ),
        ],
    )
    def test_refused_entry_is_dropped(
        self,
        client,
        client_name: str,
        redirect_uris: list[str],
        kept: list[str],
        expected_provider: str,
    ):
        response, fake_session, fake_logger = self._register(
            client, {"client_name": client_name, "redirect_uris": redirect_uris}
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["provider"] == expected_provider
        fake_session.add.assert_called_once()
        stored = fake_session.add.call_args.args[0]
        # RFC 7591 §3.2.1: the response carries the registered metadata, so it
        # echoes exactly the stored list.
        assert body["redirect_uris"] == stored.redirect_uris == kept
        assert stored.provider == expected_provider

        dropped_uris = [uri for uri in redirect_uris if uri not in kept]
        dropped = _warnings(fake_logger, "dcr_redirect_uris_dropped")
        assert len(dropped) == 1
        assert dropped[0].kwargs["provider"] == expected_provider
        assert dropped[0].kwargs["dropped_redirect_uris"] == len(dropped_uris)
        assert dropped[0].kwargs["registered_redirect_uris"] == len(kept)
        assert dropped[0].kwargs["redirect_uris"] == len(redirect_uris)
        # Every dropped entry is logged, query stripped (#218).
        assert dropped[0].kwargs["dropped"] == [uri.split("?")[0] for uri in dropped_uris]
        assert _warnings(fake_logger, "dcr_provider_rejected") == []

    @pytest.mark.parametrize(
        ("client_name", "redirect_uris", "logged"),
        [
            ("Claude", ["https://attacker.example/cb"], ["https://attacker.example/cb"]),
            (
                "Claude",
                ["https://attacker.example/cb", "https://attacker2.example/cb?x=1"],
                ["https://attacker.example/cb", "https://attacker2.example/cb"],
            ),
            ("Anthropic", ["http://localhost:9/cb"], ["http://localhost:9/cb"]),
        ],
    )
    def test_no_passing_entry_refuses_the_registration(
        self, client, client_name: str, redirect_uris: list[str], logged: list[str]
    ):
        response, fake_session, fake_logger = self._register(
            client, {"client_name": client_name, "redirect_uris": redirect_uris}
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_client_metadata"
        fake_session.add.assert_not_called()
        rejected = _warnings(fake_logger, "dcr_provider_rejected")
        assert len(rejected) == 1
        assert rejected[0].kwargs["refused_redirect_uris"] == len(redirect_uris)
        assert rejected[0].kwargs["redirect_uris"] == len(redirect_uris)
        assert rejected[0].kwargs["refused"] == logged
        # The single-URI field kept from before #2232: the first refused entry.
        assert rejected[0].kwargs["redirect_uri"] == logged[0]
        assert _warnings(fake_logger, "dcr_redirect_uris_dropped") == []

    def test_refused_list_in_the_log_is_capped(self, client):
        refused = [f"https://attacker{i}.example/cb" for i in range(12)]
        response, _, fake_logger = self._register(
            client, {"client_name": "Claude", "redirect_uris": refused}
        )

        assert response.status_code == 400, response.text
        rejected = _warnings(fake_logger, "dcr_provider_rejected")
        assert rejected[0].kwargs["refused_redirect_uris"] == 12
        assert rejected[0].kwargs["refused"] == refused[:10]

    def test_dropped_list_in_the_log_is_capped(self, client):
        refused = [f"https://attacker{i}.example/cb" for i in range(12)]
        uris = [*refused, "https://claude.ai/api/mcp/auth_callback"]
        response, fake_session, fake_logger = self._register(
            client, {"client_name": "Claude", "redirect_uris": uris}
        )

        assert response.status_code == 201, response.text
        assert fake_session.add.call_args.args[0].redirect_uris == [
            "https://claude.ai/api/mcp/auth_callback"
        ]
        dropped = _warnings(fake_logger, "dcr_redirect_uris_dropped")
        assert dropped[0].kwargs["dropped_redirect_uris"] == 12
        assert dropped[0].kwargs["redirect_uris"] == 13
        assert dropped[0].kwargs["dropped"] == refused[:10]

    @pytest.mark.parametrize(
        ("redirect_uri", "expected_provider"),
        [
            ("https://claude.ai/api/mcp/auth_callback", "claude"),
            ("https://claude.com/api/mcp/auth_callback", "claude"),
            ("https://chatgpt.com/connector_platform_oauth_redirect", "chatgpt"),
            ("https://platform.openai.com/connector_platform_oauth_redirect", "chatgpt"),
        ],
    )
    def test_provider_hosts(self, client, redirect_uri: str, expected_provider: str):
        assert detect_dcr_provider(redirect_uri, "Any Client") == expected_provider
        response, _, _ = self._register(
            client, {"client_name": "Any Client", "redirect_uris": [redirect_uri]}
        )
        assert response.status_code == 201, response.text
        assert response.json()["provider"] == expected_provider


class TestDcrRegisteredScope:
    """#1686: ``/register`` stores the requested scopes the server defines, or
    ``DCR_DEFAULT_SCOPE`` when those hold no memory scope."""

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            yield c

    @pytest.mark.parametrize(
        ("requested", "stored"),
        [
            (None, DCR_DEFAULT_SCOPE),
            ("claudeai", DCR_DEFAULT_SCOPE),
            ("openid offline_access", DCR_DEFAULT_SCOPE),
            ("openid profile", DCR_DEFAULT_SCOPE),
            ("memory:read claudeai", "memory:read"),
            ("memory:read memory:admin", "memory:read memory:admin"),
        ],
    )
    def test_stored_scope(self, client, requested: str | None, stored: str):
        rate_limit, fake_session, fake_encryptor = _patch_dcr_dependencies()
        body = {
            "client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        }
        if requested is not None:
            body["scope"] = requested
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
        ):
            response = client.post("/api/v1/oauth/register", json=body)

        assert response.status_code == 201, response.text
        assert response.json()["scope"] == stored
        assert fake_session.add.call_args.args[0].scope == stored


class TestDcrRateLimitExemption:
    """``OAUTH_DCR_RATE_LIMIT_EXEMPT_CIDRS`` lifts the per-IP DCR limit (#1741).

    A connector platform registers a client on every fresh connection from a
    shared egress range, so the operator can exempt that range; everyone else
    keeps the per-IP limit.
    """

    _BODY = {
        # Rejected at provider detection (400): proves the request got past
        # the rate limit without touching the database.
        "client_name": "MyRandomApp",
        "redirect_uris": ["https://attacker.example/cb"],
        "token_endpoint_auth_method": "none",
    }

    def _post(self, monkeypatch, client_ip: str, exempt: str):
        from config.settings import get_settings

        monkeypatch.setattr(get_settings(), "oauth_dcr_rate_limit_exempt_cidrs", exempt)
        over_limit = AsyncMock(return_value=6)
        with (
            patch("api.routes.oauth.increment_counter", over_limit),
            TestClient(app, client=(client_ip, 50000)) as client,
        ):
            response = client.post("/api/v1/oauth/register", json=self._BODY)
        return response, over_limit

    @pytest.mark.parametrize(
        ("client_ip", "exempt"),
        [
            ("203.0.113.7", "203.0.113.0/24"),
            ("203.0.113.7", "198.51.100.0/24, 203.0.113.0/24"),
            ("2001:db8::1", "2001:db8::/32"),
            ("::ffff:203.0.113.7", "203.0.113.0/24"),
        ],
    )
    def test_exempt_address_skips_the_limit(self, monkeypatch, client_ip: str, exempt: str):
        response, counter = self._post(monkeypatch, client_ip, exempt)
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_client_metadata"
        counter.assert_not_awaited()

    @pytest.mark.parametrize(
        ("client_ip", "exempt"),
        [
            ("198.51.100.7", "203.0.113.0/24"),
            ("203.0.113.7", ""),
            ("testclient", "203.0.113.0/24"),
        ],
    )
    def test_other_addresses_keep_the_limit(self, monkeypatch, client_ip: str, exempt: str):
        response, counter = self._post(monkeypatch, client_ip, exempt)
        assert response.status_code == 429, response.text
        counter.assert_awaited_once()


# Gemini custom MCP connector callbacks (#2222). ``123456789`` stands in for the
# per-user binding id Google appends; it is an example value, not a real id.
_GEMINI_HOST = "oauth-redirect.googleusercontent.com"
_GEMINI_SANDBOX_HOST = "oauth-redirect-sandbox.googleusercontent.com"
_GEMINI_TEST_HOST = "oauth-redirect-test.googleusercontent.com"
_GEMINI_PREFIX = "/r/user_bound_custom-mcp-"
_GEMINI_URI = f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}123456789"
_GEMINI_SANDBOX_URI = f"https://{_GEMINI_SANDBOX_HOST}{_GEMINI_PREFIX}123456789"
_GEMINI_TEST_URI = f"https://{_GEMINI_TEST_HOST}{_GEMINI_PREFIX}123456789"


class TestDetectDcrProviderGemini:
    """Exact host + pinned path prefix + one segment, compared as written."""

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            _GEMINI_URI,
            _GEMINI_SANDBOX_URI,
            _GEMINI_TEST_URI,
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}abc-123-slug",
            f"https://{_GEMINI_SANDBOX_HOST}{_GEMINI_PREFIX}abc-123-slug",
            f"https://{_GEMINI_TEST_HOST}{_GEMINI_PREFIX}abc-123-slug",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}a",
            # Percent-encoded unreserved character ("-").
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}abc%2D1",
        ],
    )
    @pytest.mark.parametrize("client_name", ["Gemini", "Any Client"])
    def test_accepted(self, redirect_uri: str, client_name: str):
        assert detect_dcr_provider(redirect_uri, client_name) == "gemini"

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            f"http://{_GEMINI_HOST}{_GEMINI_PREFIX}x",
            f"HTTPS://{_GEMINI_HOST}{_GEMINI_PREFIX}x",
            # Case variant of the host: refused, the host is compared as written.
            f"https://OAUTH-REDIRECT.googleusercontent.com{_GEMINI_PREFIX}x",
            f"https://evil.googleusercontent.com{_GEMINI_PREFIX}x",
            f"https://googleusercontent.com{_GEMINI_PREFIX}x",
            f"https://x.{_GEMINI_TEST_HOST}{_GEMINI_PREFIX}x",
            f"https://OAUTH-REDIRECT-TEST.googleusercontent.com{_GEMINI_PREFIX}x",
            f"https://{_GEMINI_TEST_HOST}/r/*",
            f"https://{_GEMINI_TEST_HOST}{_GEMINI_PREFIX}x?next=1",
            f"https://x.{_GEMINI_HOST}{_GEMINI_PREFIX}x",
            f"https://{_GEMINI_HOST}.evil.com{_GEMINI_PREFIX}x",
            f"https://{_GEMINI_HOST}.{_GEMINI_PREFIX}x",
            f"https://user@{_GEMINI_HOST}{_GEMINI_PREFIX}x",
            f"https://user:pw@{_GEMINI_HOST}{_GEMINI_PREFIX}x",
            f"https://{_GEMINI_HOST}:443{_GEMINI_PREFIX}x",
            f"https://{_GEMINI_HOST}:443.evil.com{_GEMINI_PREFIX}x",
            f"https://xn--oauth-redirect-xyz.googleusercontent.com{_GEMINI_PREFIX}x",
            f"https://oauth-rédirect.googleusercontent.com{_GEMINI_PREFIX}x",
            f"https://{_GEMINI_HOST}/r/other-x",
            f"https://{_GEMINI_HOST}/r/user_bound_custom-mcp",
            f"https://{_GEMINI_HOST}/R/user_bound_custom-mcp-x",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x/",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x/y",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%2Fy",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%2fy",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x\\y",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%5Cy",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}.",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}..",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x..",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%2E%2E",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}%2E",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%252F",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x?next=1",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x?",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x#f",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%23f",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x;p",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x%3Bp",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}*",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x y",
            # Wildcard patterns pass the pydantic pattern check but are never Gemini.
            f"https://{_GEMINI_HOST}/r/*",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}x/*",
            f" https://{_GEMINI_HOST}{_GEMINI_PREFIX}x",
        ],
    )
    def test_refused(self, redirect_uri: str):
        assert detect_dcr_provider(redirect_uri, "Gemini") == "custom"

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "http://localhost/cb",
            "http://127.0.0.1:8080/oauth/callback",
            "http://[::1]:8080/oauth/callback",
        ],
    )
    @pytest.mark.parametrize("client_name", ["Gemini", "Gemini CLI", "gemini"])
    def test_gemini_is_not_a_loopback_keyword(self, redirect_uri: str, client_name: str):
        assert detect_dcr_provider(redirect_uri, client_name) == "custom"


class TestRedactDcrRedirectUrisForLog:
    """Log form of DCR redirect URIs (#2232): #218 redaction plus a masked binding."""

    @pytest.mark.parametrize(
        ("redirect_uri", "expected"),
        [
            # Unknown shapes: scheme + host + path, query and fragment dropped (#218).
            ("https://attacker.example/cb?code=x#f", "https://attacker.example/cb"),
            ("http://localhost:9/cb", "http://localhost:9/cb"),
            # The Gemini binding segment is masked on any host, known or not.
            (_GEMINI_URI, f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}<segment>"),
            (
                f"https://oauth-redirect-example.googleusercontent.com{_GEMINI_PREFIX}x/y?z=1",
                f"https://oauth-redirect-example.googleusercontent.com{_GEMINI_PREFIX}<segment>",
            ),
            (
                f"https://{_GEMINI_HOST}/R/USER_BOUND_CUSTOM-MCP-example-1",
                f"https://{_GEMINI_HOST}/R/USER_BOUND_CUSTOM-MCP-<segment>",
            ),
            (
                f"https://attacker.example/prefix{_GEMINI_PREFIX}example-1",
                f"https://attacker.example/prefix{_GEMINI_PREFIX}<segment>",
            ),
            # A path outside the binding namespace is logged as is.
            (f"https://{_GEMINI_HOST}/r/other", f"https://{_GEMINI_HOST}/r/other"),
            # The prefix with an empty segment still gets the mask.
            (
                f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}",
                f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}<segment>",
            ),
            # Userinfo is never logged; host and port are.
            ("https://user:s3cret@attacker.example/cb", "https://attacker.example/cb"),
            ("https://user@attacker.example:8443/cb", "https://attacker.example:8443/cb"),
            # A percent-encoded prefix: the whole path is masked, since the raw
            # text has no prefix to cut after.
            (
                f"https://{_GEMINI_HOST}/r/user_bound_custom%2Dmcp-example-1",
                f"https://{_GEMINI_HOST}/<encoded binding path>",
            ),
            (
                f"https://{_GEMINI_HOST}/r/user%5Fbound_custom-mcp-example-1",
                f"https://{_GEMINI_HOST}/<encoded binding path>",
            ),
        ],
    )
    def test_redacts_one_uri(self, redirect_uri: str, expected: str):
        from api.routes.oauth import _redact_dcr_redirect_uri_for_log

        assert _redact_dcr_redirect_uri_for_log(redirect_uri) == expected

    def test_masks_before_the_length_cap(self):
        # A long path must not push the prefix past the 256-char cut unmasked.
        from api.routes.oauth import _redact_dcr_redirect_uri_for_log

        uri = f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}{'x' * 400}"
        assert _redact_dcr_redirect_uri_for_log(uri) == (
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}<segment>"
        )

    def test_list_is_capped(self):
        from api.routes.oauth import (
            _DCR_LOG_MAX_REDIRECT_URIS,
            _redact_dcr_redirect_uris_for_log,
        )

        uris = [f"https://attacker{i}.example/cb?x=1" for i in range(12)]
        assert _DCR_LOG_MAX_REDIRECT_URIS == 10
        assert _redact_dcr_redirect_uris_for_log(uris) == [
            f"https://attacker{i}.example/cb" for i in range(10)
        ]
        assert _redact_dcr_redirect_uris_for_log([]) == []


class TestDcrEndpointGemini:
    """``POST /api/v1/oauth/register`` with Gemini callbacks (#2222)."""

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            yield c

    def _register(self, client, body: dict):
        rate_limit, fake_session, fake_encryptor = _patch_dcr_dependencies()
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
            patch("api.routes.oauth.logger") as fake_logger,
        ):
            response = client.post("/api/v1/oauth/register", json=body)
        return response, fake_session, fake_logger

    @staticmethod
    def _six_uris() -> list[str]:
        # Example binding ids, not real ones.
        return [
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}example-1",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}example-2",
            f"https://{_GEMINI_HOST}{_GEMINI_PREFIX}example-3",
            f"https://{_GEMINI_SANDBOX_HOST}{_GEMINI_PREFIX}example-1",
            f"https://{_GEMINI_SANDBOX_HOST}{_GEMINI_PREFIX}example-2",
            f"https://{_GEMINI_SANDBOX_HOST}{_GEMINI_PREFIX}example-3",
        ]

    def test_six_gemini_uris_register_verbatim(self, client):
        uris = self._six_uris()
        response, fake_session, _ = self._register(
            client, {"client_name": "Gemini", "redirect_uris": uris}
        )

        assert response.status_code == 201, response.text
        assert response.json()["provider"] == "gemini"
        assert response.json()["redirect_uris"] == uris
        fake_session.add.assert_called_once()
        stored = fake_session.add.call_args.args[0]
        assert stored.provider == "gemini"
        assert stored.redirect_uris == uris
        assert stored.token_endpoint_auth_method == "none"

    def test_one_foreign_uri_is_dropped(self, client):
        six = self._six_uris()
        uris = [*six, f"https://{_GEMINI_HOST}/r/other"]
        response, fake_session, fake_logger = self._register(
            client, {"client_name": "Gemini", "redirect_uris": uris}
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["provider"] == "gemini"
        assert body["redirect_uris"] == six
        fake_session.add.assert_called_once()
        stored = fake_session.add.call_args.args[0]
        assert stored.provider == "gemini"
        assert stored.redirect_uris == six
        dropped = _warnings(fake_logger, "dcr_redirect_uris_dropped")
        assert len(dropped) == 1
        assert dropped[0].kwargs["dropped_redirect_uris"] == 1
        assert dropped[0].kwargs["redirect_uris"] == 7
        assert dropped[0].kwargs["registered_redirect_uris"] == 6
        assert dropped[0].kwargs["dropped"] == [f"https://{_GEMINI_HOST}/r/other"]
        assert _warnings(fake_logger, "dcr_provider_rejected") == []

    def test_refused_first_then_gemini(self, client):
        uris = ["https://attacker.example/cb", _GEMINI_URI]
        response, fake_session, _ = self._register(
            client, {"client_name": "Gemini", "redirect_uris": uris}
        )

        assert response.status_code == 201, response.text
        assert response.json()["provider"] == "gemini"
        assert response.json()["redirect_uris"] == [_GEMINI_URI]
        assert fake_session.add.call_args.args[0].redirect_uris == [_GEMINI_URI]

    def test_wildcard_next_to_gemini_is_dropped_not_stored(self, client):
        # #2222 invariant: a ``/r/*`` wildcard is never stored for Gemini.
        wildcard = f"https://{_GEMINI_HOST}/r/*"
        response, fake_session, _ = self._register(
            client, {"client_name": "Gemini", "redirect_uris": [_GEMINI_URI, wildcard]}
        )

        assert response.status_code == 201, response.text
        stored = fake_session.add.call_args.args[0]
        assert stored.redirect_uris == [_GEMINI_URI]
        assert response.json()["redirect_uris"] == [_GEMINI_URI]

    def test_dropped_gemini_shaped_uri_has_its_segment_masked(self, client):
        # A Gemini-shaped callback on a host outside the table: dropped, and the
        # per-user binding segment never reaches the log (#2232).
        foreign = f"https://oauth-redirect-example.googleusercontent.com{_GEMINI_PREFIX}example-9"
        response, _, fake_logger = self._register(
            client, {"client_name": "Gemini", "redirect_uris": [_GEMINI_URI, f"{foreign}?q=1"]}
        )

        assert response.status_code == 201, response.text
        dropped = _warnings(fake_logger, "dcr_redirect_uris_dropped")
        assert dropped[0].kwargs["dropped"] == [
            f"https://oauth-redirect-example.googleusercontent.com{_GEMINI_PREFIX}<segment>"
        ]
        assert "example-9" not in repr(fake_logger.mock_calls)

    def test_refused_gemini_shaped_uri_has_its_segment_masked(self, client):
        foreign = f"https://evil.googleusercontent.com{_GEMINI_PREFIX}example-9"
        response, _, fake_logger = self._register(
            client, {"client_name": "Gemini", "redirect_uris": [foreign]}
        )

        assert response.status_code == 400, response.text
        rejected = _warnings(fake_logger, "dcr_provider_rejected")
        assert rejected[0].kwargs["refused"] == [
            f"https://evil.googleusercontent.com{_GEMINI_PREFIX}<segment>"
        ]
        assert "example-9" not in repr(fake_logger.mock_calls)

    def test_test_host_registers_with_the_others(self, client):
        # The list Gemini sends: production, sandbox and test redirector hosts.
        uris = [
            *self._six_uris(),
            f"https://{_GEMINI_TEST_HOST}{_GEMINI_PREFIX}example-1",
            f"https://{_GEMINI_TEST_HOST}{_GEMINI_PREFIX}example-2",
        ]
        response, fake_session, fake_logger = self._register(
            client, {"client_name": "Gemini", "redirect_uris": uris}
        )

        assert response.status_code == 201, response.text
        assert response.json()["provider"] == "gemini"
        assert response.json()["redirect_uris"] == uris
        assert fake_session.add.call_args.args[0].redirect_uris == uris
        assert _warnings(fake_logger, "dcr_redirect_uris_dropped") == []

    def test_rejection_description_names_every_gemini_host(self, client):
        from api.routes.oauth import _PROVIDER_EXACT_REDIRECTS

        response, _, _ = self._register(
            client,
            {"client_name": "Gemini", "redirect_uris": ["https://attacker.example/cb"]},
        )

        assert response.status_code == 400, response.text
        description = response.json()["error_description"]
        hosts, prefix = _PROVIDER_EXACT_REDIRECTS["gemini"]
        assert hosts == (_GEMINI_HOST, _GEMINI_SANDBOX_HOST, _GEMINI_TEST_HOST)
        for host in hosts:
            assert f"https://{host}" in description
        assert prefix in description

    def test_gemini_with_claude_stores_the_first(self, client):
        uris = [_GEMINI_URI, "https://claude.ai/api/mcp/auth_callback"]
        response, _, _ = self._register(client, {"client_name": "Gemini", "redirect_uris": uris})

        assert response.status_code == 201, response.text
        assert response.json()["provider"] == "gemini"

    def test_claude_list_unchanged(self, client):
        uris = ["https://claude.ai/api/mcp/auth_callback"]
        response, _, _ = self._register(client, {"client_name": "Claude", "redirect_uris": uris})

        assert response.status_code == 201, response.text
        assert response.json()["provider"] == "claude"

    @pytest.mark.parametrize(
        "redirect_uris",
        [
            ["http://localhost:8080/oauth/callback"],
            # A trailing ``/*`` wildcard passes the pattern check, then fails detection.
            [f"https://{_GEMINI_HOST}/r/*"],
            [f"https://evil.googleusercontent.com{_GEMINI_PREFIX}x"],
        ],
    )
    def test_refused_registrations(self, client, redirect_uris: list[str]):
        response, fake_session, _ = self._register(
            client, {"client_name": "Gemini", "redirect_uris": redirect_uris}
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_client_metadata"
        fake_session.add.assert_not_called()


# --- #2410: Meta Muse ---------------------------------------------------------

_MUSE_URI = "https://agent.meta.ai/api/hatch/oauth/callback"


class TestDetectDcrProviderMuse:
    """One fixed callback, matched as the whole URI, character for character."""

    @pytest.mark.parametrize("client_name", ["Meta Muse", "Muse", "Any Client"])
    def test_accepted(self, client_name: str):
        assert detect_dcr_provider(_MUSE_URI, client_name) == "muse"

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            # muse.ai is not Meta's callback host (and names an unrelated service).
            "https://muse.ai/api/hatch/oauth/callback",
            "https://www.muse.ai/api/hatch/oauth/callback",
            # Other meta.ai hosts and look-alikes.
            "https://meta.ai/api/hatch/oauth/callback",
            "https://evil.agent.meta.ai/api/hatch/oauth/callback",
            "https://agent.meta.ai.evil.example/api/hatch/oauth/callback",
            # Scheme, case, trailing dot, port, userinfo.
            "http://agent.meta.ai/api/hatch/oauth/callback",
            "HTTPS://agent.meta.ai/api/hatch/oauth/callback",
            "https://AGENT.META.AI/api/hatch/oauth/callback",
            "https://agent.meta.ai./api/hatch/oauth/callback",
            "https://agent.meta.ai:443/api/hatch/oauth/callback",
            "https://user@agent.meta.ai/api/hatch/oauth/callback",
            # Path variants: the whole path is fixed, no prefix match.
            "https://agent.meta.ai/api/hatch/oauth/callback/",
            "https://agent.meta.ai/api/hatch/oauth/callback/x",
            "https://agent.meta.ai/api/hatch/oauth/callbackx",
            "https://agent.meta.ai/api/hatch/oauth/",
            "https://agent.meta.ai/api/hatch/oauth/%63allback",
            "https://agent.meta.ai/API/hatch/oauth/callback",
            # Query, params, fragment.
            "https://agent.meta.ai/api/hatch/oauth/callback?next=https://evil.example",
            "https://agent.meta.ai/api/hatch/oauth/callback#x",
            "https://agent.meta.ai/api/hatch/oauth/callback;x",
        ],
    )
    def test_refused(self, redirect_uri: str):
        assert detect_dcr_provider(redirect_uri, "Meta Muse") == "custom"

    def test_muse_is_not_a_loopback_keyword(self):
        # A loopback registration that merely says "Muse" stays refused.
        assert detect_dcr_provider("http://localhost:8080/cb", "Muse") == "custom"


class TestDcrEndpointMuse:
    """``POST /api/v1/oauth/register`` with Muse's callback (#2410)."""

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            yield c

    def _register(self, client, body: dict):
        rate_limit, fake_session, fake_encryptor = _patch_dcr_dependencies()
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
            patch("api.routes.oauth.logger") as fake_logger,
        ):
            response = client.post("/api/v1/oauth/register", json=body)
        return response, fake_session, fake_logger

    def test_public_pkce_client_registers(self, client):
        response, fake_session, _ = self._register(
            client,
            {
                "client_name": "Meta Muse",
                "redirect_uris": [_MUSE_URI],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["provider"] == "muse"
        assert body["redirect_uris"] == [_MUSE_URI]
        fake_session.add.assert_called_once()
        stored = fake_session.add.call_args.args[0]
        assert stored.provider == "muse"
        assert stored.redirect_uris == [_MUSE_URI]
        assert stored.token_endpoint_auth_method == "none"

    def test_muse_ai_host_is_refused(self, client):
        response, fake_session, _ = self._register(
            client,
            {
                "client_name": "Meta Muse",
                "redirect_uris": ["https://muse.ai/api/hatch/oauth/callback"],
            },
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_client_metadata"
        fake_session.add.assert_not_called()

    def test_rejection_description_names_the_muse_callback(self, client):
        response, _, _ = self._register(
            client,
            {"client_name": "Meta Muse", "redirect_uris": ["https://attacker.example/cb"]},
        )

        assert response.status_code == 400, response.text
        assert _MUSE_URI in response.json()["error_description"]


class TestDcrPatternFollowups:
    @pytest.mark.parametrize(
        "invalid",
        [
            "ftp://claude.ai/callback",
            "https://claude.ai/cb/*/bad",
            "https://*.claude.ai/callback",
            "https://user:password@[invalid/callback",
        ],
    )
    @pytest.mark.parametrize("mixed", [False, True])
    def test_pattern_invalid_entries_are_dropped(self, invalid, mixed):
        allowed = "https://claude.ai/api/mcp/auth_callback"
        rate_limit, fake_session, fake_encryptor = _patch_dcr_dependencies()
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
        ):
            response = TestClient(app).post(
                "/api/v1/oauth/register",
                json={
                    "client_name": "Claude",
                    "redirect_uris": [invalid, allowed] if mixed else [invalid],
                },
            )
        assert response.status_code == (201 if mixed else 400), response.text
        if mixed:
            assert response.json()["redirect_uris"] == [allowed]
            assert fake_session.add.call_args.args[0].redirect_uris == [allowed]
        else:
            assert response.json()["error"] == "invalid_client_metadata"
            fake_session.add.assert_not_called()

    def test_redirect_list_is_bounded_at_100(self):
        from pydantic import ValidationError

        from api.routes.oauth import DynamicClientRegistrationRequest

        uri = "https://claude.ai/api/mcp/auth_callback"
        assert len(DynamicClientRegistrationRequest(redirect_uris=[uri] * 100).redirect_uris) == 100
        with pytest.raises(ValidationError):
            DynamicClientRegistrationRequest(redirect_uris=[uri] * 101)

    @pytest.mark.parametrize("schema", ["OAuth2ClientCreateRequest", "OAuth2ClientUpdateRequest"])
    def test_manual_registration_keeps_strict_patterns(self, schema):
        from pydantic import ValidationError

        from api.routes import oauth

        with pytest.raises(ValidationError):
            getattr(oauth, schema)(client_name="manual", redirect_uris=["ftp://claude.ai/callback"])
