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
from utils.exceptions import (
    AdminProtectionError,
    AuthorizationError,
    MemoryCloudException,
    RateLimitError,
    RedisError,
)

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
        assert body.get("url") is None  # inert by default (PAYMENT_PUBLIC_BASE_URL unset)
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


# ============================================================================
# Rate limiting (#1104)
# ============================================================================


class TestHandoffRateLimitHelper:
    """Unit tests for the per-(owner, workspace) mint rate-limit helper."""

    @pytest.mark.asyncio
    async def test_under_limit_passes_and_pins_bucket_shape(self):
        # Pin the Redis call shape: the bucket MUST be keyed per (user, workspace)
        # with a self-expiring 60s window. A regression to a global/static key
        # (shared-bucket DoS) or a dropped TTL (never-expiring lockout) must fail here.
        ic = AsyncMock(return_value=3)
        with patch.object(route_mod, "incrby_counter", ic):
            await route_mod._check_handoff_rate_limit("u", WS_A, 10)  # no raise
        ic.assert_awaited_once_with(f"billing_handoff:u:{WS_A}:minute", amount=1, ttl=60)

    @pytest.mark.asyncio
    async def test_bucket_keys_are_isolated_per_user_and_workspace(self):
        # Distinct (user, workspace) tuples MUST map to distinct buckets, so one
        # owner (or one workspace) cannot exhaust another's quota.
        keys: list[str] = []
        ic = AsyncMock(return_value=1)
        ic.side_effect = lambda key, **_: keys.append(key) or 1
        with patch.object(route_mod, "incrby_counter", ic):
            await route_mod._check_handoff_rate_limit("alice", WS_A, 10)
            await route_mod._check_handoff_rate_limit("bob", WS_A, 10)
            await route_mod._check_handoff_rate_limit("alice", WS_B, 10)
        assert len(set(keys)) == 3, keys

    @pytest.mark.asyncio
    async def test_at_limit_passes(self):
        with patch.object(route_mod, "incrby_counter", AsyncMock(return_value=10)):
            await route_mod._check_handoff_rate_limit("u", WS_A, 10)  # no raise

    @pytest.mark.asyncio
    async def test_over_limit_raises_429(self):
        with patch.object(route_mod, "incrby_counter", AsyncMock(return_value=11)):
            with pytest.raises(RateLimitError) as exc:
                await route_mod._check_handoff_rate_limit("u", WS_A, 10)
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_zero_limit_rejects(self):
        # limit<=0 disables minting (fail-safe), mirroring the sibling buckets.
        with pytest.raises(RateLimitError):
            await route_mod._check_handoff_rate_limit("u", WS_A, 0)

    @pytest.mark.asyncio
    async def test_fail_open_on_redis_error(self):
        # Redis down → log and allow (consistent with the public_search buckets);
        # the owner gate + short-TTL token remain the primary controls. Use the
        # production error type (incrby_counter wraps failures in RedisError) so a
        # future narrowing of the catch that excludes RedisError is caught here.
        with patch.object(
            route_mod, "incrby_counter", AsyncMock(side_effect=RedisError("redis down"))
        ):
            await route_mod._check_handoff_rate_limit("u", WS_A, 10)  # no raise


class TestHandoffRateLimitRoute:
    def test_over_limit_returns_429(self, session_client):
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "BillingHandoffSigner") as signer_cls,
            patch.object(route_mod, "incrby_counter", AsyncMock(return_value=9999)),
        ):
            signer_cls.return_value.mint.return_value = _fake_minted(WS_A)
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})
        assert resp.status_code == 429, resp.text
        # Mint must NOT have run once the rate limit tripped.
        signer_cls.return_value.mint.assert_not_called()

    def test_rate_limit_runs_after_owner_gate(self, session_client):
        # A non-owner is 403'd by the owner gate BEFORE the rate-limit bucket is
        # touched, so a non-owner cannot exhaust the owner's quota.
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(
            side_effect=AuthorizationError(reason="role_too_low")
        )
        ic = AsyncMock(return_value=1)
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "incrby_counter", ic),
        ):
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})
        assert resp.status_code == 403
        ic.assert_not_called()

    def test_fail_open_lets_mint_proceed(self, session_client):
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "BillingHandoffSigner") as signer_cls,
            patch.object(
                route_mod, "incrby_counter", AsyncMock(side_effect=RedisError("redis down"))
            ),
        ):
            signer_cls.return_value.mint.return_value = _fake_minted(WS_A)
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})
        assert resp.status_code == 200, resp.text
        # Fail-open means the request reaches mint — assert it actually ran.
        signer_cls.return_value.mint.assert_called_once()


# ============================================================================
# Ready-to-use handoff URL (#1118) — opt-in, additive
# ============================================================================

# Single source of truth for the fake JWT (mirrors _fake_minted's token).
_FAKE_TOKEN = _fake_minted(uuid4()).token


class TestHandoffReadyToUseUrl:
    """The opt-in ``url`` convenience: when ``payment_public_base_url`` is set the
    response ADDS a ready-to-use ``{base}/enter?t={token}`` redirect; unset keeps
    the decoupled raw-token contract (#1098) with ``url=None``."""

    def _post_with_base(self, session_client, base_url: str):
        """POST as an owner with ``payment_public_base_url=base_url``.

        Returns ``(response, build_spy)``. ``build_spy`` WRAPS the real
        ``_build_handoff_url`` so a test can prove the route actually invoked it
        (so ``url is None`` reflects a real builder call on the empty base, not
        merely the response field default). ``incrby_counter`` is stubbed so the
        rate-limit takes its normal under-limit path, not the Redis fail-open one.
        """
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
        # Patch get_settings so both the rate-limit read and the new base-url read
        # come from one stand-in (the route reads settings once).
        settings = MagicMock()
        settings.payment_public_base_url = base_url
        settings.billing_handoff_rate_limit_per_minute = 10
        build_spy = MagicMock(wraps=route_mod._build_handoff_url)
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "BillingHandoffSigner") as signer_cls,
            patch.object(route_mod, "get_settings", return_value=settings),
            patch.object(route_mod, "incrby_counter", AsyncMock(return_value=1)),
            patch.object(route_mod, "_build_handoff_url", build_spy),
        ):
            signer_cls.return_value.mint.return_value = _fake_minted(WS_A)
            resp = session_client.post("/api/v1/billing/handoff", json={"workspace_id": str(WS_A)})
        return resp, build_spy

    def test_url_is_null_when_base_unset(self, session_client):
        resp, build_spy = self._post_with_base(session_client, "")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["url"] is None
        # Non-vacuous: the route actually called the builder with the empty base,
        # so url is None because the builder returned None — not because the arg
        # was dropped or the field merely defaults to None.
        build_spy.assert_called_once_with("", _FAKE_TOKEN)
        # The raw-token contract is intact regardless of the opt-in field.
        assert body["token"] == _FAKE_TOKEN

    def test_url_built_when_base_set(self, session_client):
        resp, _ = self._post_with_base(session_client, "https://billing.example.com")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["url"] == f"https://billing.example.com/enter?t={_FAKE_TOKEN}"
        # Additive: the raw token is still present and unchanged (shape not replaced).
        assert body["token"] == _FAKE_TOKEN
        assert body["kid"] == "kid-1"

    def test_url_normalizes_trailing_slash_on_base(self, session_client):
        resp, _ = self._post_with_base(session_client, "https://billing.example.com/")
        assert resp.status_code == 200, resp.text
        # No double slash before /enter.
        assert resp.json()["url"] == f"https://billing.example.com/enter?t={_FAKE_TOKEN}"


class TestBuildHandoffUrlHelper:
    """Unit tests for the ``{base}/enter?t={token}`` URL builder."""

    def test_returns_none_for_empty_or_blank_base(self):
        assert route_mod._build_handoff_url("", "tok") is None
        assert route_mod._build_handoff_url("   ", "tok") is None

    def test_returns_none_for_slash_only_base(self):
        # A slash-only base must collapse to None, not a relative "/enter?t=..."
        # that a browser resolves against the API host (rstrip runs BEFORE guard).
        assert route_mod._build_handoff_url("/", "tok") is None
        assert route_mod._build_handoff_url("//", "tok") is None
        assert route_mod._build_handoff_url("  /  ", "tok") is None

    def test_builds_enter_url(self):
        assert (
            route_mod._build_handoff_url("https://billing.example.com", "tok")
            == "https://billing.example.com/enter?t=tok"
        )

    def test_normalizes_trailing_slash(self):
        assert (
            route_mod._build_handoff_url("https://billing.example.com/", "tok")
            == "https://billing.example.com/enter?t=tok"
        )


class TestValidateHandoffBaseUrl:
    """Unit tests for the startup validator on ``payment_public_base_url`` (#1118)."""

    def test_empty_is_allowed_and_normalized(self):
        from config.settings import _validate_handoff_base_url

        assert _validate_handoff_base_url("") == ""
        assert _validate_handoff_base_url("   ") == ""

    def test_https_origin_passes(self):
        from config.settings import _validate_handoff_base_url

        assert (
            _validate_handoff_base_url("https://billing.example.com")
            == "https://billing.example.com"
        )

    def test_http_localhost_allowed_for_dev(self):
        from config.settings import _validate_handoff_base_url

        assert _validate_handoff_base_url("http://localhost:9000") == "http://localhost:9000"

    @pytest.mark.parametrize(
        "bad",
        [
            "http://billing.example.com",  # plaintext non-local → token-over-HTTP leak
            "billing.example.com",  # no scheme → relative URL
            "javascript:alert(1)",  # non-http(s) scheme
            "https://billing.example.com/v2",  # path component → breaks /enter contract
        ],
    )
    def test_rejects_malformed_base(self, bad):
        from config.settings import _validate_handoff_base_url

        with pytest.raises(ValueError):
            _validate_handoff_base_url(bad)
