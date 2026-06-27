"""RBAC + wiring tests for POST /api/v1/billing/handoff (#1093).

The handoff endpoint mints a short-lived Ed25519 token that lets the external
billing service trust an authenticated workspace **owner** without re-implementing
user auth. The security contract under test:

- **session-auth only** — a Bearer credential (API key / OAuth) is rejected 403
  by ``require_session_auth`` (exercised with the real dependency).
- **owner-only** — admin/member get 403 via ``PermissionService.check_workspace_owner``.
- **explicit-target-workspace binding** — the owner check runs against the
  *request body* ``workspace_id``, never the caller's ``current_workspace_id``
  (the #389 multi-workspace cross-tenant trap).
- **fail-closed** — an unconfigured signing key surfaces 503 (BILLING-002), even
  for an owner.

The router is mounted on a fresh app with overridden deps so the suite needs no
DB/redis. A local ``MemoryCloudException`` handler mirrors the production
status-code mapping without importing the umap-heavy ``api.main``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.routes import billing_handoff as route_mod
from auth.billing_handoff import BillingHandoffNotConfigured, MintedHandoffToken
from auth.dependencies import require_session_auth
from db.base import get_db
from utils.datetime import utcnow
from utils.exceptions import AdminProtectionError, AuthorizationError, MemoryCloudException

WS_A = uuid4()  # the caller's "current" workspace
WS_B = uuid4()  # a different workspace the caller does NOT own


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(route_mod.router, prefix="/api/v1")

    async def _mc_handler(_request, exc: MemoryCloudException) -> JSONResponse:
        # Mirror production memory_cloud_exception_handler (api/main.py): the wire
        # body is {"error", "message", "details"} — NOT "error_code" — with
        # details stripped to {} for deny-class exceptions (CWE-639). Asserting
        # against this shape verifies the body clients actually receive.
        details = {} if isinstance(exc, (AuthorizationError, AdminProtectionError)) else exc.details
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.error_code, "message": exc.message, "details": details},
        )

    app.add_exception_handler(MemoryCloudException, _mc_handler)
    return app


def _session_user(user_id: str = "owner-1") -> dict:
    return {
        "user_id": user_id,
        "email": f"{user_id}@test.com",
        "current_workspace_id": WS_A,
    }


def _fake_minted(workspace_id, ownership_epoch: int = 0) -> MintedHandoffToken:
    issued = utcnow()
    return MintedHandoffToken(
        token="eyJhbGciOiJFZERTQSJ9.fake.sig",
        jti="jti-test-1",
        kid="kid-1",
        issued_at=issued,
        expires_at=issued + timedelta(seconds=120),
        workspace_id=str(workspace_id),
        user_id="owner-1",
        ownership_epoch=ownership_epoch,
    )


def _db_with_epoch(epoch: int = 0) -> MagicMock:
    """A db stand-in whose single ``execute(...).scalar_one()`` yields ``epoch`` —
    the route reads the workspace's live ownership_epoch this way (#1100)."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one.return_value = epoch
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture
def session_client():
    """Client where require_session_auth yields an owner session user."""
    app = _build_app()

    async def _mock_session():
        return _session_user()

    async def _mock_db():
        yield _db_with_epoch(0)

    app.dependency_overrides[require_session_auth] = _mock_session
    app.dependency_overrides[get_db] = _mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ============================================================================
# Owner happy path
# ============================================================================


class TestHandoffOwnerHappyPath:
    def test_owner_gets_token(self, session_client):
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "BillingHandoffSigner") as signer_cls,
        ):
            signer_cls.return_value.mint.return_value = _fake_minted(WS_A)
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token"] == "eyJhbGciOiJFZERTQSJ9.fake.sig"
        assert body["kid"] == "kid-1"
        assert body["jti"] == "jti-test-1"
        assert body["token_type"] == "billing_handoff"
        assert body["expires_at"].endswith("Z")  # TZAwareBaseModel serialization
        # The signer must be invoked with the body workspace and the session user.
        mint_call = signer_cls.return_value.mint.call_args
        assert mint_call.kwargs["workspace_id"] == WS_A
        assert mint_call.kwargs["user_id"] == "owner-1"

    def test_owner_check_uses_body_workspace_not_session_current(self, session_client):
        """The #389 trap guard: owner is verified against the body workspace_id."""
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "BillingHandoffSigner") as signer_cls,
        ):
            signer_cls.return_value.mint.return_value = _fake_minted(WS_B)
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_B)})

        assert resp.status_code == 200, resp.text
        # The owner gate must have been called with WS_B (the body), not WS_A (session).
        called_args = perm.check_workspace_owner.await_args
        assert WS_B in called_args.args or WS_B in called_args.kwargs.values()
        assert WS_A not in called_args.args
        # AND the token itself must be minted for WS_B, not the session's WS_A —
        # a correct owner-check that then minted for current_workspace would still
        # re-open the cross-tenant trap, so assert the mint binding too.
        mint_call = signer_cls.return_value.mint.call_args
        assert mint_call.kwargs["workspace_id"] == WS_B
        assert mint_call.kwargs["user_id"] == "owner-1"

    def test_route_stamps_workspace_ownership_epoch_into_token(self, session_client):
        """#1100: the route reads the workspace's live ownership_epoch and passes it
        to mint, so the token carries the generation it was minted under."""

        async def _db_epoch_5():
            yield _db_with_epoch(5)

        session_client.app.dependency_overrides[get_db] = _db_epoch_5

        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "BillingHandoffSigner") as signer_cls,
        ):
            signer_cls.return_value.mint.return_value = _fake_minted(WS_A, ownership_epoch=5)
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})

        assert resp.status_code == 200, resp.text
        mint_call = signer_cls.return_value.mint.call_args
        assert mint_call.kwargs["ownership_epoch"] == 5


# ============================================================================
# Owner-only denial
# ============================================================================


class TestHandoffOwnerOnly:
    @pytest.mark.parametrize("denied_role", ["admin", "member", "viewer"])
    def test_non_owner_gets_403(self, session_client, denied_role):
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(
            side_effect=AuthorizationError(reason="role_too_low")
        )
        with patch.object(route_mod, "PermissionService", return_value=perm):
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})
        assert resp.status_code == 403, f"{denied_role}: {resp.text}"

    def test_owner_of_other_workspace_cannot_mint_for_unowned_target(self, session_client):
        # Caller owns WS_A but requests a handoff for WS_B → owner check raises → 403.
        perm = MagicMock()

        async def _owner_only_of_a(_user_id, workspace_id):
            if workspace_id != WS_A:
                raise AuthorizationError(reason="not_a_member")
            return MagicMock()

        perm.check_workspace_owner = AsyncMock(side_effect=_owner_only_of_a)
        with patch.object(route_mod, "PermissionService", return_value=perm):
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_B)})
        assert resp.status_code == 403, resp.text


# ============================================================================
# Session-only enforcement (real dependency)
# ============================================================================


class TestHandoffSessionOnly:
    # require_session_auth rejects EVERY Bearer credential — both a kagura_* API
    # key and an opaque/OAuth device-flow token (distinguished only for the
    # token_kind log field). Both must 403 before any owner check or minting.
    @pytest.mark.parametrize(
        "bearer",
        ["kagura_live_testkey", "opaque-oauth-access-token-abc123"],
        ids=["api_key", "oauth_bearer"],
    )
    def test_any_bearer_credential_is_rejected_403(self, bearer):
        # Use the REAL require_session_auth (not overridden).
        app = _build_app()

        async def _mock_db():
            yield MagicMock()

        app.dependency_overrides[get_db] = _mock_db
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/billing/handoff",
                json={"workspace_id": str(WS_A)},
                headers={"Authorization": f"Bearer {bearer}"},
            )
        assert resp.status_code == 403, resp.text


# ============================================================================
# Fail-closed when unconfigured
# ============================================================================


class TestHandoffFailClosed:
    def test_unconfigured_signing_key_returns_503_even_for_owner(self, session_client):
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "BillingHandoffSigner") as signer_cls,
        ):
            signer_cls.return_value.mint.side_effect = BillingHandoffNotConfigured()
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})
        assert resp.status_code == 503, resp.text
        # Production envelope keys on "error" (not "error_code") and carries
        # "message"/"details" — assert the shape clients actually receive.
        body = resp.json()
        assert body["error"] == "BILLING-002"
        assert set(body) >= {"error", "message", "details"}


# ============================================================================
# Input validation
# ============================================================================


class TestHandoffValidation:
    def test_missing_workspace_id_is_422(self, session_client):
        resp = session_client.post("/api/v1/billing/handoff", json={})
        assert resp.status_code == 422

    def test_non_uuid_workspace_id_is_422(self, session_client):
        resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": "not-a-uuid"})
        assert resp.status_code == 422
