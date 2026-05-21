"""Property test pinning the canonical OAuth scope set across all four
sources (three metadata endpoints + DCR fallback). Set equality only —
other metadata fields are covered by ``tests/smoke/test_well_known.py``.
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
from auth.mcp_scopes import (  # noqa: E402
    ALL_ADVERTISED_SCOPES,
    DCR_DEFAULT_SCOPE,
    DCR_DEFAULT_SCOPES,
)


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
    ``DCR_DEFAULT_SCOPES`` — the advertised set minus ``memory:admin``.

    #608 (D1) narrows the DCR fallback so a client that omits ``scope`` no
    longer receives ``memory:admin`` by default. ``memory:admin`` remains
    advertised in ``scopes_supported`` and explicitly requestable via the
    ``scope`` parameter; only the silent auto-grant is removed.
    """

    def _patched_dcr_session(self):
        """Same shape as ``test_oauth_dcr._patch_dcr_dependencies``, inlined
        intentionally — a drift guard must not couple to fixtures owned by
        other test files, or its contract becomes hostage to unrelated test
        churn.
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
            patch("api.routes.oauth.increment_counter", rate_limit),
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
        assert issued_scopes == set(DCR_DEFAULT_SCOPES), (
            f"DCR issued {sorted(issued_scopes)}, "
            f"expected {sorted(DCR_DEFAULT_SCOPES)} — fallback default "
            "drifted from auth.mcp_scopes.DCR_DEFAULT_SCOPES."
        )
        assert "memory:admin" not in issued_scopes, (
            "DCR fallback re-introduced memory:admin into the default — "
            "this breaks the narrowing-first ordering required by #608 D1 "
            "(a later require_scope deployment would silently grant admin "
            "to every already-issued DCR token)."
        )

    def test_dcr_explicit_admin_request_preserves_admin(self, client: TestClient) -> None:
        """When a client explicitly requests ``memory:admin`` at DCR
        registration, the issued client retains admin in its registered
        scope. The narrowing in #608 (D1) is to the FALLBACK default only,
        not to the explicit-grant path.

        This pins the R1 policy from gate1 review: admin remains an
        opt-in capability, not a removed one. The e09 migration mirrors
        this on the data side (custom-scope rows are preserved).
        """
        rate_limit, fake_session, fake_encryptor = self._patched_dcr_session()
        with (
            patch("api.routes.oauth.increment_counter", rate_limit),
            patch("api.routes.oauth.get_sync_session", return_value=fake_session),
            patch("utils.encryption.get_encryptor", return_value=fake_encryptor),
        ):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": "Claude Code",
                    "redirect_uris": ["http://localhost:8080/callback"],
                    "token_endpoint_auth_method": "none",
                    "scope": "memory:read memory:write memory:admin",
                },
            )

        assert response.status_code == 201, (
            f"DCR registration with explicit admin failed: {response.status_code} {response.text}"
        )
        body = response.json()
        issued_scopes = set(body["scope"].split())
        assert "memory:admin" in issued_scopes, (
            f"DCR dropped explicitly-requested memory:admin from scope "
            f"{sorted(issued_scopes)} — the narrowing in #608 D1 should "
            "apply ONLY to the fallback default, not to explicit grants."
        )

    def test_dcr_default_scopes_excludes_only_memory_admin(self) -> None:
        """Pin the narrowing contract: ``DCR_DEFAULT_SCOPES`` is exactly
        ``ALL_ADVERTISED_SCOPES`` minus ``memory:admin``. No other scope is
        dropped from the default. If a future change accidentally narrows
        the default further (e.g. removes ``memory:delete`` here instead of
        in the #608 D4 migration), this test fails loud.
        """
        assert set(DCR_DEFAULT_SCOPES) == set(ALL_ADVERTISED_SCOPES) - {"memory:admin"}

    def test_dcr_default_scope_constant_matches_dcr_set(self) -> None:
        """Independent of any HTTP round-trip: the constant DCR_DEFAULT_SCOPE
        is exactly DCR_DEFAULT_SCOPES as a space-separated string. Pins both
        the contents AND the encoding (space-separated, RFC 6749 §3.3).
        """
        assert set(DCR_DEFAULT_SCOPE.split()) == set(DCR_DEFAULT_SCOPES)
        assert DCR_DEFAULT_SCOPE == " ".join(DCR_DEFAULT_SCOPES)


class TestNarrowedClientScopeRequestRespectsIntersection:
    """Server-side authorization behavior when a DCR-narrowed client requests
    ``memory:admin``.

    A DCR client registered under the narrowed default (post-#608 D1) holds
    ``DCR_DEFAULT_SCOPES`` — no admin. If that client later requests
    ``memory:admin`` at the authorization step (which a legacy SDK may do
    if it builds the authz URL from ``scopes_supported`` instead of the
    DCR-registered scope), the server-side ``OAuth2Client.get_allowed_scope``
    method returns the intersection of requested-and-registered, so the
    issued token gets the narrowed scope. The MCP authorization spec
    (SEP-835) explicitly endorses this narrowing as least-privilege
    behavior: "Authorization Servers MAY issue access tokens with narrower
    scopes."

    This test pins that contract at the model layer. An SEP-835-compliant
    SDK will accept the narrowed token. A pre-SEP-835 strict-drift SDK may
    invalidate the token; the rollback path is ``alembic downgrade
    e08_592_oauth_scope_canonicalize`` plus reverting `mcp_scopes.py`.
    """

    def test_narrowed_client_requesting_admin_gets_intersection(self) -> None:
        from models.auth import OAuth2Client  # noqa: PLC0415

        narrowed_client = OAuth2Client(
            client_id="test-narrowed-client",
            client_secret_hash="dummy",
            client_name="Test Client (DCR default)",
            redirect_uris=["http://localhost:8080/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            scope=DCR_DEFAULT_SCOPE,
            token_endpoint_auth_method="none",
        )

        granted = set(
            narrowed_client.get_allowed_scope(
                "memory:read memory:write memory:admin offline_access"
            ).split()
        )

        assert granted == {"memory:read", "memory:write", "offline_access"}, (
            f"Expected intersection to exclude memory:admin, got {sorted(granted)}. "
            "OAuth2Client.get_allowed_scope must return requested ∩ registered — "
            "a DCR-narrowed client (no admin in registered scope) requesting "
            "admin must receive a token without admin (SEP-835 least-privilege)."
        )
        assert "memory:admin" not in granted

    def test_explicit_admin_client_requesting_admin_gets_admin(self) -> None:
        """Counterpart to the narrowed case: a client whose registered scope
        includes ``memory:admin`` (explicit-grant via R1 policy) DOES receive
        admin when requesting it. Confirms the intersection logic is
        symmetric — narrowing applies only when the client wasn't granted
        admin in the first place.
        """
        from models.auth import OAuth2Client  # noqa: PLC0415

        explicit_admin_client = OAuth2Client(
            client_id="test-explicit-admin",
            client_secret_hash="dummy",
            client_name="Admin Worker",
            redirect_uris=["http://localhost:8080/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            scope="memory:read memory:write memory:admin",
            token_endpoint_auth_method="none",
        )

        granted = set(explicit_admin_client.get_allowed_scope("memory:read memory:admin").split())

        assert granted == {"memory:read", "memory:admin"}
        assert "memory:admin" in granted


class TestMcpAuthChallengeIncludesResourceMetadata:
    """RFC 9728 §5.1: ``WWW-Authenticate`` on a 401 from a protected resource
    SHOULD include a ``resource_metadata`` attribute pointing at the
    protected-resource metadata document. Smoke test only — end-to-end
    behavior with a real Claude Code client is verified manually.
    """

    def test_mcp_401_includes_resource_metadata_attr(self, client: TestClient) -> None:
        response = client.get("/mcp/")
        # /mcp/ must require authentication and emit the RFC 6750 + RFC 9728
        # challenge. If the transport ever stops returning 401 here (auth
        # short-circuit, accidental public access), failing loud is what we
        # want — silently skipping would defeat the purpose of pinning the
        # challenge contract.
        assert response.status_code == 401, (
            f"expected 401 for unauthenticated GET /mcp/, got "
            f"{response.status_code}: {response.text[:200]}"
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
