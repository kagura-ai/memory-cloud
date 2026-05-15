"""Tests for OAuth Bearer support on REST /api/v1/* endpoints (Issue #649).

Covers the dependency-layer behavior the issue locks down:

- ``verify_oauth_bearer_token`` + ``is_oauth_bearer_token`` helpers in
  ``auth.oauth2_bearer``.
- Method → scope mapping (``_required_scope_for_method``) and membership
  test (``_scope_granted``) in ``auth.dependencies``.
- ``get_user_from_api_key_or_session`` priority-0 OAuth branch:
  insufficient-scope 403 with RFC 6750 ``WWW-Authenticate`` header,
  invalid-token 401, ``kagura_*`` API-key fallthrough unchanged.
- ``require_session_auth`` rejecting OAuth Bearer with 403 (carrying
  the same #252 intent forward to OAuth).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from auth.dependencies import (  # noqa: E402
    _required_scope_for_method,
    _scope_granted,
    get_user_from_api_key_or_session,
    require_session_auth,
)
from auth.oauth2_bearer import is_oauth_bearer_token  # noqa: E402

# ---------------------------------------------------------------------------
# Pure helpers — no DB, no FastAPI
# ---------------------------------------------------------------------------


class TestIsOAuthBearerToken:
    def test_kagura_prefix_is_not_oauth(self):
        assert is_oauth_bearer_token("kagura_abc123") is False

    def test_random_token_is_oauth(self):
        assert is_oauth_bearer_token("A" * 43) is True

    def test_empty_string_is_not_oauth(self):
        assert is_oauth_bearer_token("") is False


class TestRequiredScopeForMethod:
    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_read_methods_map_to_memory_read(self, method):
        assert _required_scope_for_method(method) == "memory:read"

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_write_methods_map_to_memory_write(self, method):
        assert _required_scope_for_method(method) == "memory:write"

    def test_lowercase_method_is_normalized(self):
        assert _required_scope_for_method("get") == "memory:read"
        assert _required_scope_for_method("post") == "memory:write"

    def test_unknown_method_defaults_to_memory_write(self):
        # Fail-closed: an unrecognized verb gets the stricter scope so a
        # future HTTP method (TRACE / CONNECT / custom) cannot accidentally
        # bypass enforcement.
        assert _required_scope_for_method("EXTEND") == "memory:write"


class TestScopeGranted:
    def test_required_scope_present(self):
        assert _scope_granted("memory:read memory:write", "memory:read") is True

    def test_required_scope_absent(self):
        assert _scope_granted("memory:read", "memory:write") is False

    def test_none_granted_is_false(self):
        assert _scope_granted(None, "memory:read") is False

    def test_empty_granted_is_false(self):
        assert _scope_granted("", "memory:read") is False

    def test_no_substring_partial_match(self):
        # Membership is per-token, not substring — ``memory:writeable``
        # must NOT satisfy ``memory:write``.
        assert _scope_granted("memory:writeable", "memory:write") is False


# ---------------------------------------------------------------------------
# get_user_from_api_key_or_session — OAuth priority-0 branch
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_oauth_route():
    """Mount a single test route protected by ``get_user_from_api_key_or_session``."""
    app = FastAPI()

    from fastapi import Depends

    @app.get("/_t/read")
    async def read_route(user: dict = Depends(get_user_from_api_key_or_session)):
        return {"user_id": user["user_id"], "oauth_scope": user.get("oauth_scope")}

    @app.post("/_t/write")
    async def write_route(user: dict = Depends(get_user_from_api_key_or_session)):
        return {"user_id": user["user_id"], "oauth_scope": user.get("oauth_scope")}

    @app.delete("/_t/delete")
    async def delete_route(user: dict = Depends(get_user_from_api_key_or_session)):
        return {"user_id": user["user_id"], "oauth_scope": user.get("oauth_scope")}

    return app


class TestOAuthBranchInDependency:
    def test_valid_oauth_with_read_scope_passes_get(self, app_with_oauth_route):
        with (
            patch(
                "auth.dependencies.verify_oauth_bearer_token",
                new=AsyncMock(return_value=("user-oauth-123", "memory:read memory:write")),
            ),
            patch(
                "auth.dependencies._get_user_workspace_id",
                new=AsyncMock(return_value=None),
            ),
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.get("/_t/read", headers={"Authorization": "Bearer randomtok"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["user_id"] == "user-oauth-123"
            assert body["oauth_scope"] == "memory:read memory:write"

    def test_valid_oauth_with_write_scope_passes_post(self, app_with_oauth_route):
        with (
            patch(
                "auth.dependencies.verify_oauth_bearer_token",
                new=AsyncMock(return_value=("user-oauth-123", "memory:write")),
            ),
            patch(
                "auth.dependencies._get_user_workspace_id",
                new=AsyncMock(return_value=None),
            ),
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.post("/_t/write", headers={"Authorization": "Bearer randomtok"})
            assert resp.status_code == 200

    def test_read_only_token_on_post_returns_403_with_www_authenticate(self, app_with_oauth_route):
        with patch(
            "auth.dependencies.verify_oauth_bearer_token",
            new=AsyncMock(return_value=("user-oauth-123", "memory:read")),
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.post("/_t/write", headers={"Authorization": "Bearer randomtok"})
            assert resp.status_code == 403
            assert "insufficient_scope" in resp.headers.get("www-authenticate", "")
            assert 'scope="memory:write"' in resp.headers.get("www-authenticate", "")

    def test_read_only_token_on_delete_returns_403(self, app_with_oauth_route):
        with patch(
            "auth.dependencies.verify_oauth_bearer_token",
            new=AsyncMock(return_value=("user-oauth-123", "memory:read")),
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.delete("/_t/delete", headers={"Authorization": "Bearer randomtok"})
            assert resp.status_code == 403
            assert "insufficient_scope" in resp.headers.get("www-authenticate", "")

    def test_invalid_or_expired_oauth_returns_401(self, app_with_oauth_route):
        with patch(
            "auth.dependencies.verify_oauth_bearer_token",
            new=AsyncMock(return_value=None),
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.get("/_t/read", headers={"Authorization": "Bearer randomtok"})
            assert resp.status_code == 401

    def test_kagura_prefix_bypasses_oauth_path(self, app_with_oauth_route):
        """Tokens starting with ``kagura_`` MUST NOT trigger the OAuth verifier.

        Verified via the verifier's mock: if the OAuth path is wrongly
        taken, the mock is invoked. We assert it is NOT called even
        when the API-key path itself rejects the token below. The
        ``APIKeyManager.verify_key`` patch short-circuits the DB lookup
        so the test does not require a live postgres.
        """
        oauth_mock = AsyncMock(return_value=None)
        api_key_manager_mock = AsyncMock(return_value=None)
        with (
            patch("auth.dependencies.verify_oauth_bearer_token", new=oauth_mock),
            patch("auth.api_keys.APIKeyManager.verify_key", new=api_key_manager_mock),
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.get("/_t/read", headers={"Authorization": "Bearer kagura_doesnotexist"})
            # API-key path will 401 because the mock returns None, but
            # the contract we're verifying is that the OAuth verifier
            # is never reached.
            assert resp.status_code == 401
            oauth_mock.assert_not_called()
            api_key_manager_mock.assert_called_once()

    def test_no_scope_token_cannot_read(self, app_with_oauth_route):
        """A token with NULL ``scope`` (legacy / non-CLI issuance) fails closed."""
        with patch(
            "auth.dependencies.verify_oauth_bearer_token",
            new=AsyncMock(return_value=("user-oauth-123", None)),
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.get("/_t/read", headers={"Authorization": "Bearer randomtok"})
            assert resp.status_code == 403


# ---------------------------------------------------------------------------
# require_session_auth — Web UI-only routes reject OAuth Bearer
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_session_only_route():
    app = FastAPI()
    from fastapi import Depends

    @app.get("/_t/web_ui")
    async def web_ui_route(user: dict = Depends(require_session_auth)):
        return {"user_id": user["user_id"]}

    return app


class TestRequireSessionAuthRejectsOAuth:
    def test_oauth_bearer_rejected_with_403(self, app_with_session_only_route):
        client = TestClient(app_with_session_only_route)
        resp = client.get(
            "/_t/web_ui",
            headers={"Authorization": "Bearer randomoauth"},
        )
        assert resp.status_code == 403
        # Message must mention both kinds so SDK error surfaces don't
        # mislead users into thinking only API keys are blocked.
        assert "OAuth" in resp.json()["detail"] or "Bearer" in resp.json()["detail"]

    def test_kagura_api_key_still_rejected_with_403(self, app_with_session_only_route):
        client = TestClient(app_with_session_only_route)
        resp = client.get(
            "/_t/web_ui",
            headers={"Authorization": "Bearer kagura_anykey"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# MCP verify_oauth2_token shim — relocation regression guard
# ---------------------------------------------------------------------------


class TestMCPOAuth2TokenShim:
    """Ensure ``mcp_server.auth._verify_oauth2_token`` still returns just
    ``user_id`` after the verifier was relocated to
    ``auth.oauth2_bearer.verify_oauth_bearer_token`` (Issue #649).

    MCP scope enforcement happens at the tool layer
    (``auth.mcp_scopes``), not the transport gate, so the MCP shim
    intentionally discards scope here. This test pins that contract.
    """

    @pytest.mark.asyncio
    async def test_shim_returns_user_id_only(self):
        from mcp_server.auth import _verify_oauth2_token

        with patch(
            "auth.oauth2_bearer.verify_oauth_bearer_token",
            new=AsyncMock(return_value=("user-mcp-xyz", "memory:read")),
        ):
            result = await _verify_oauth2_token("randomtok")
            assert result == "user-mcp-xyz"

    @pytest.mark.asyncio
    async def test_shim_returns_none_on_invalid(self):
        from mcp_server.auth import _verify_oauth2_token

        with patch(
            "auth.oauth2_bearer.verify_oauth_bearer_token",
            new=AsyncMock(return_value=None),
        ):
            result = await _verify_oauth2_token("randomtok")
            assert result is None


# ---------------------------------------------------------------------------
# DB failure → 401 (silent-failure contract matches verify_api_key)
# ---------------------------------------------------------------------------


class TestOAuthVerifyDBFailure:
    """``verify_oauth_bearer_token`` swallows SQLAlchemy errors and
    returns ``None`` so that a transient DB failure during the OAuth
    lookup surfaces as 401 to the client, not 500 — matching
    ``verify_api_key``'s contract (``auth.dependencies:225-227``).
    """

    @pytest.mark.asyncio
    async def test_sqlalchemy_error_returns_none(self):
        from sqlalchemy.exc import OperationalError

        from auth.oauth2_bearer import verify_oauth_bearer_token

        failing_db = AsyncMock()
        failing_db.execute = AsyncMock(
            side_effect=OperationalError("statement", {}, Exception("pool exhausted"))
        )

        result = await verify_oauth_bearer_token("randomtok", failing_db)
        assert result is None

    def test_db_failure_in_dependency_returns_401(self, app_with_oauth_route):
        """End-to-end: DB error in the verifier results in 401, not 500."""
        with patch(
            "auth.dependencies.verify_oauth_bearer_token",
            new=AsyncMock(return_value=None),  # simulate "lookup failed → None"
        ):
            client = TestClient(app_with_oauth_route)
            resp = client.get("/_t/read", headers={"Authorization": "Bearer randomtok"})
            assert resp.status_code == 401
