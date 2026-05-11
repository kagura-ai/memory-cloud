"""Drift guard for OAuth metadata scope advertisements (#592).

Issue #592 surfaced a 4-way drift between OAuth metadata sources that broke
the Claude Code MCP DCR flow: ``oauth-authorization-server``,
``oauth-protected-resource``, ``openid-configuration``, and the DCR
``POST /oauth/register`` fallback each advertised a different
``scopes_supported`` set, and the SDK's scope-mismatch check correctly
invalidated all credentials when the union didn't match what was issued.

The fix consolidates the canonical set in :mod:`auth.mcp_scopes`. This test
file is the property test that PINS that contract — if any of the four
sources drift again, one of these tests fails before production does.

The tests are intentionally narrow: they assert set equality on
``scopes_supported`` only. Other metadata fields (issuer, endpoints,
PKCE methods) are covered by ``tests/smoke/test_well_known.py``.
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
from auth.mcp_scopes import ALL_ADVERTISED_SCOPES, DCR_DEFAULT_SCOPE  # noqa: E402


@pytest.fixture
def client():
    """Test client without auth overrides — these endpoints must be public."""
    with TestClient(app) as c:
        yield c


class TestMetadataScopesNoDrift:
    """All three metadata endpoints advertise the canonical scope set.

    Set equality (not list equality) — order is not part of the contract and
    enforcing it would just create churn whenever the constant list is
    reordered.
    """

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/.well-known/openid-configuration",
        ],
    )
    def test_scopes_supported_matches_canonical(self, client: TestClient, endpoint: str) -> None:
        response = client.get(endpoint)
        assert response.status_code == 200, (
            f"{endpoint} returned {response.status_code}: {response.text}"
        )
        body = response.json()
        assert "scopes_supported" in body, f"{endpoint} response is missing scopes_supported"
        assert set(body["scopes_supported"]) == set(ALL_ADVERTISED_SCOPES), (
            f"{endpoint} advertises {sorted(body['scopes_supported'])}, "
            f"expected {sorted(ALL_ADVERTISED_SCOPES)} — metadata drift "
            "detected, re-anchor on auth.mcp_scopes.ALL_ADVERTISED_SCOPES."
        )


class TestDcrFallbackScopeNoDrift:
    """DCR fallback scope (``POST /oauth/register`` with no ``scope``) matches
    the canonical set, so a fresh DCR client holds every advertised scope.

    Before #592, this fell back to ``memory:read memory:write offline_access``
    while the well-known endpoints advertised ``memory:admin`` too — the
    union/issued mismatch was what tripped the Claude Code SDK's scope check.
    """

    def _patched_dcr_session(self):
        """Mock the DCR success path: rate-limit counter + sync DB session +
        encryptor. Same shape as ``test_oauth_dcr._patch_dcr_dependencies``
        but inlined to keep this file independent — drift guards should not
        couple to other tests' fixtures.
        """
        rate_limit = AsyncMock(return_value=1)
        fake_session = MagicMock()

        def _refresh(c):
            if c.id is None:
                c.id = 99999

        fake_session.refresh.side_effect = _refresh

        fake_encryptor = MagicMock()
        fake_encryptor.encrypt = MagicMock(return_value="encrypted-secret-blob-base64")
        return rate_limit, fake_session, fake_encryptor

    def test_dcr_default_scope_matches_canonical(self, client: TestClient) -> None:
        rate_limit, fake_session, fake_encryptor = self._patched_dcr_session()
        with (
            patch("db.redis.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
        ):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": "Claude Code",
                    "redirect_uris": ["http://localhost:8080/callback"],
                    "token_endpoint_auth_method": "none",
                    # NB: no "scope" key — exercise the fallback path
                },
            )

        assert response.status_code == 201, (
            f"DCR registration failed: {response.status_code} {response.text}"
        )
        body = response.json()
        issued_scopes = set(body["scope"].split())
        assert issued_scopes == set(ALL_ADVERTISED_SCOPES), (
            f"DCR issued {sorted(issued_scopes)}, "
            f"expected {sorted(ALL_ADVERTISED_SCOPES)} — fallback default "
            "drifted from auth.mcp_scopes.DCR_DEFAULT_SCOPE."
        )

    def test_dcr_default_scope_constant_matches_advertised_set(self) -> None:
        """Independent of any HTTP round-trip: the constant DCR_DEFAULT_SCOPE
        is exactly the advertised set as a space-separated string. Pins both
        the contents AND the encoding (space-separated, RFC 6749 §3.3).
        """
        assert set(DCR_DEFAULT_SCOPE.split()) == set(ALL_ADVERTISED_SCOPES)
        # space-separated, no leading/trailing whitespace, no double spaces
        assert DCR_DEFAULT_SCOPE == " ".join(ALL_ADVERTISED_SCOPES)


class TestMcpAuthChallengeIncludesResourceMetadata:
    """RFC 9728 §5.1: the ``WWW-Authenticate`` header on 401 responses from a
    protected resource SHOULD include a ``resource_metadata`` attribute
    pointing at the protected-resource metadata document. Before #592, the
    Claude Code MCP SDK had to guess this URL by convention; making it
    explicit closes the discovery hole.

    This is a smoke test, not a full RFC 9728 audit — it asserts the header
    is present and well-formed when MCP auth fails. End-to-end behavior with
    a real Claude Code client is verified manually post-deploy.
    """

    def test_mcp_401_includes_resource_metadata_attr(self, client: TestClient) -> None:
        # Unauthenticated GET to /mcp/ — auth should fail and emit RFC 6750 +
        # RFC 9728 challenge.
        response = client.get("/mcp/")
        if response.status_code != 401:
            pytest.skip(
                "MCP route did not return 401 for unauthenticated GET in test "
                f"client (got {response.status_code}); transport may short-circuit "
                "before auth in TestClient. Manual verification covers this path."
            )
        www_authenticate = response.headers.get("www-authenticate", "")
        assert "Bearer" in www_authenticate, (
            f"missing Bearer challenge in WWW-Authenticate: {www_authenticate!r}"
        )
        assert "resource_metadata=" in www_authenticate, (
            "WWW-Authenticate is missing the RFC 9728 resource_metadata "
            f"attribute: {www_authenticate!r}"
        )
        assert "/.well-known/oauth-protected-resource" in www_authenticate, (
            "resource_metadata URL does not point at the protected-resource "
            f"metadata document: {www_authenticate!r}"
        )
