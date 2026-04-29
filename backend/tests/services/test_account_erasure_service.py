"""Unit tests for AccountErasureService (Issue #360, GDPR right-to-erasure).

These tests target the state-machine + guard logic in the service layer.
The cross-store deletion pipeline (`_execute`) requires real Qdrant +
Redis + Postgres containers and is exercised separately via
``make test-integration`` with the migrations applied; the unit tests
here use mocks for the data-layer collaborators so they stay fast and
deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.erasure import (
    REASON_SELF_SERVICE,
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_CANCELLED,
    STATUS_COOLING_OFF,
    STATUS_PENDING,
    ErasureRequest,
)
from services.account_erasure_service import (
    COOLING_OFF_PERIOD,
    AccountErasureService,
    _sha256_hex,
)
from utils.exceptions import (
    EmailDispatchError,
    ErasureAlreadyInProgressError,
    ErasureForbiddenError,
    ErasureRequestNotFoundError,
    ErasureTokenInvalidError,
    InitialAdminCannotBeErasedError,
    NotFoundException,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(
    *,
    user_id: str = "u-1",
    email: str = "alice@example.com",
    is_initial_admin: bool = False,
    auth_method: str = "oauth",
    password_hash: str | None = None,
    role: str = "user",
) -> SimpleNamespace:
    """Light User stand-in. AccountErasureService only reads attributes."""
    return SimpleNamespace(
        user_id=user_id,
        email=email,
        is_initial_admin=is_initial_admin,
        auth_method=auth_method,
        password_hash=password_hash,
        role=role,
    )


def _service() -> AccountErasureService:
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()
    email = AsyncMock()
    email.send_erasure_receipt = AsyncMock(return_value=True)
    email.send_erasure_cooling_off_started = AsyncMock(return_value=True)
    email.send_erasure_complete = AsyncMock(return_value=True)
    # send_erasure_confirmation defaults to success — Issue #469's OAuth path
    # exercises this; tests that need failure modes override per-test.
    email.send_erasure_confirmation = AsyncMock(return_value=True)
    return AccountErasureService(db, email_service=email)


# ---------------------------------------------------------------------------
# request_self_service_erasure
# ---------------------------------------------------------------------------


class TestRequestSelfServiceErasure:
    @staticmethod
    def _wire_typical_path(svc: AccountErasureService, target: SimpleNamespace) -> list:
        """Common stub harness shared by the request-creation tests.

        Returns the ``added_rows`` capture list so callers can inspect the
        ``ErasureRequest`` row built by the service.

        Critical detail: ``db.flush`` is wired to populate ``request.id`` —
        in real SQLAlchemy the server-default UUID is materialized at flush
        time, and the service relies on that for ``redis.setex`` value and
        the ``request_id`` passed to ``send_erasure_confirmation`` BEFORE
        ``db.commit()/refresh()`` runs. Without simulating this, a
        regression that uses ``request.id`` against an unflushed row would
        silently pass tests by writing ``"None"`` to Redis and the email
        body. The ``db.refresh`` callback is kept as well — it represents
        the in-prod path where committed-state attributes (``status``, etc.)
        are reconciled from the DB.
        """
        svc._load_user_or_404 = AsyncMock(return_value=target)
        svc._find_active_request = AsyncMock(return_value=None)

        added_rows: list[ErasureRequest] = []
        svc.db.add = lambda row: added_rows.append(row)

        async def _flush_populates_id() -> None:
            for row in added_rows:
                if getattr(row, "id", None) is None:
                    row.id = uuid4()

        async def _refresh(row: ErasureRequest) -> None:
            if getattr(row, "id", None) is None:
                row.id = uuid4()

        svc.db.flush = AsyncMock(side_effect=_flush_populates_id)
        svc.db.refresh = AsyncMock(side_effect=_refresh)
        return added_rows

    @pytest.mark.asyncio
    async def test_password_user_returns_raw_token_in_response(self):
        """Password-auth users get the raw confirm_token in the return value
        (the response body remains their canonical delivery channel — they
        re-enter their password as the second factor at confirm time)."""
        svc = _service()
        target = _user(auth_method="password", password_hash="hashed")
        added_rows = self._wire_typical_path(svc, target)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(setex=AsyncMock(), delete=AsyncMock())
            request, response_token = await svc.request_self_service_erasure(user_id="u-1")

        assert response_token is not None, "password users must receive raw token"
        assert len(added_rows) == 1
        row = added_rows[0]
        assert row.status == STATUS_PENDING
        assert row.is_self_service is True
        assert row.reason_code == REASON_SELF_SERVICE
        assert row.confirm_token_hash == _sha256_hex(response_token)
        assert row.user_email_hash == _sha256_hex(target.email)
        assert request is row
        # No confirmation email for password users — the response IS the channel.
        svc.email_service.send_erasure_confirmation.assert_not_awaited()
        # Receipt is fire-and-forget post-commit (existing pattern).
        svc.email_service.send_erasure_receipt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_oauth_user_returns_none_and_sends_confirmation_email(self):
        """OAuth users get confirm_token=None in the response and the raw
        token is delivered via send_erasure_confirmation. This is the
        defining behavior of Issue #469."""
        svc = _service()
        target = _user(auth_method="oauth")
        added_rows = self._wire_typical_path(svc, target)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(setex=AsyncMock(), delete=AsyncMock())
            request, response_token = await svc.request_self_service_erasure(user_id="u-1")

        assert response_token is None, "OAuth users must NOT receive token in response"
        assert len(added_rows) == 1
        row = added_rows[0]
        # The raw token still exists internally — recovered from the email
        # call so we can verify the row hash matches what was sent.
        svc.email_service.send_erasure_confirmation.assert_awaited_once()
        call_kwargs = svc.email_service.send_erasure_confirmation.await_args.kwargs
        sent_raw_token = call_kwargs["confirm_token"]
        assert sent_raw_token  # non-empty
        assert row.confirm_token_hash == _sha256_hex(sent_raw_token)
        assert call_kwargs["to_email"] == target.email
        # confirm_url embeds the raw token via query parameter (the Resend
        # implementation keeps the token in the URL only, not the body).
        assert sent_raw_token in call_kwargs["confirm_url"]
        assert call_kwargs["confirm_url"].startswith("http")  # built from frontend_url
        # Success path must NOT delete the Redis token — the key is what the
        # downstream confirm endpoint validates against.
        mock_redis.return_value.delete.assert_not_awaited()
        # Regression guard: ``request_id`` passed to the email send must be
        # the real UUID populated at flush time, NOT "None". A regression
        # that uses ``request.id`` before flush would silently write "None"
        # into the email body and Redis SETEX value. The ``_wire_typical_path``
        # harness simulates server-default population on flush.
        assert call_kwargs["request_id"] != "None"
        assert call_kwargs["request_id"]  # non-empty
        # Same guard on the Redis SETEX call: third arg is the request id.
        setex_call = mock_redis.return_value.setex.await_args
        assert setex_call.args[2] != "None"
        assert setex_call.args[2]  # non-empty

    @pytest.mark.asyncio
    async def test_oauth_send_returns_false_raises_dispatch_error(self):
        """OAuth path: when the email service returns False (Protocol-honoring
        failure signal), the service must rollback and raise EmailDispatchError
        so the route layer can return 503. Without this the user would have
        a committed pending row but no token in any channel — wedged."""
        svc = _service()
        svc.email_service.send_erasure_confirmation = AsyncMock(return_value=False)
        target = _user(auth_method="oauth")
        self._wire_typical_path(svc, target)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(setex=AsyncMock(), delete=AsyncMock())
            with pytest.raises(EmailDispatchError) as exc_info:
                await svc.request_self_service_erasure(user_id="u-1")

        assert exc_info.value.status_code == 503
        svc.db.rollback.assert_awaited_once()
        # commit must NOT have run — the row should be rolled back atomically.
        svc.db.commit.assert_not_awaited()
        # Receipt must not have fired either (it's post-commit).
        svc.email_service.send_erasure_receipt.assert_not_awaited()
        # Redis token must be explicitly deleted on rollback — this narrows
        # the orphan-token window from the 1h TTL to "immediate" so a
        # partially-delivered email cannot drive a confirm against the
        # rolled-back row.
        mock_redis.return_value.delete.assert_awaited_once()
        deleted_key = mock_redis.return_value.delete.await_args.args[0]
        assert deleted_key.startswith("erasure_token:")

    @pytest.mark.asyncio
    async def test_oauth_send_raises_redacts_log_and_rolls_back(self):
        """OAuth path: a misbehaving email backend that raises (against the
        Protocol contract) must still trigger rollback + EmailDispatchError,
        AND the log entry must NOT contain str(exc) — SDK exception messages
        can echo the request body which embeds confirm_url + raw token.

        This is defense-in-depth per OWASP A09 (Security Logging Failures).
        """
        raw_token_should_not_leak = "test-token-leaked-in-sdk-error-message"
        svc = _service()
        svc.email_service.send_erasure_confirmation = AsyncMock(
            side_effect=RuntimeError(
                f"resend SDK 500: request body included token={raw_token_should_not_leak}"
            )
        )
        target = _user(auth_method="oauth")
        self._wire_typical_path(svc, target)

        with (
            patch("services.account_erasure_service.get_redis_client") as mock_redis,
            patch("services.account_erasure_service.logger") as mock_logger,
        ):
            mock_redis.return_value = MagicMock(setex=AsyncMock(), delete=AsyncMock())
            with pytest.raises(EmailDispatchError) as exc_info:
                await svc.request_self_service_erasure(user_id="u-1")

        assert exc_info.value.status_code == 503
        # __cause__ / __context__ must be suppressed (raise ... from None) so
        # the original SDK message — which can echo the token — does not
        # propagate up the exception chain.
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True

        # Recover the actual token + confirm_url that the service generated
        # internally. ``call_args`` is populated even when ``side_effect``
        # raises — Mock records the call before evaluating side_effect.
        # This is the genuine secret material; ``raw_token_should_not_leak``
        # above is only the string we forced into the SDK exception.
        send_call = svc.email_service.send_erasure_confirmation.call_args
        actual_raw_token = send_call.kwargs["confirm_token"]
        actual_confirm_url = send_call.kwargs["confirm_url"]
        assert actual_raw_token, "test setup error: no token was passed to send"
        assert actual_raw_token in actual_confirm_url, (
            "test setup error: confirm_url should embed the raw token"
        )

        # Verify log redaction across every logger call made during the failure.
        haystack_parts: list[str] = []
        for call in mock_logger.method_calls:
            _name, args, kwargs = call
            haystack_parts.extend(str(a) for a in args)
            for k, v in kwargs.items():
                haystack_parts.append(str(k))
                haystack_parts.append(str(v))
        haystack = " ".join(haystack_parts)

        # 1. Sentinel in the SDK exception's str(exc) must not leak.
        assert raw_token_should_not_leak not in haystack, (
            "SDK exception message leaked into log; service must not surface str(exc)"
        )
        # 2. The actually-generated token must not leak (catches a future
        #    regression where the service starts logging confirm_url, the
        #    raw token, or other request metadata in the failure branch).
        assert actual_raw_token not in haystack, (
            "actual generated token leaked into log; "
            "redaction must hold for the real secret material, "
            "not just the sentinel injected via the SDK exception"
        )
        # 3. confirm_url leak (which embeds the token as a query parameter).
        assert actual_confirm_url not in haystack, (
            "confirm_url leaked into log; URL embeds the raw token as a query param"
        )
        # 4. error_type IS expected to be present (structured metadata).
        assert "error_type" in haystack
        assert "RuntimeError" in haystack

        svc.db.rollback.assert_awaited_once()
        svc.db.commit.assert_not_awaited()
        # Redis token must be explicitly deleted on the exception rollback
        # path — same defense as the False-return path. The key passed must
        # match the actual generated token recovered above.
        mock_redis.return_value.delete.assert_awaited_once()
        deleted_key = mock_redis.return_value.delete.await_args.args[0]
        assert deleted_key == f"erasure_token:{actual_raw_token}"

    @pytest.mark.asyncio
    async def test_commit_fails_after_oauth_email_send_cleans_up(self):
        """OAuth path: if ``db.commit()`` fails AFTER the confirmation email
        has already been sent (rare — DB blip during commit), the service
        must still rollback, best-effort delete the Redis token, log
        ``erasure_request_commit_failed_after_side_effects``, and re-raise.

        The user has the confirm email already; if they click, the confirm
        endpoint sees no row → ``ErasureRequestNotFoundError`` (404) — same
        UX as Redis TTL self-clean, but explicit cleanup means the failure
        is observable in logs.
        """
        svc = _service()
        svc.db.commit = AsyncMock(side_effect=RuntimeError("simulated commit blip"))
        target = _user(auth_method="oauth")
        self._wire_typical_path(svc, target)

        with (
            patch("services.account_erasure_service.get_redis_client") as mock_redis,
            patch("services.account_erasure_service.logger") as mock_logger,
        ):
            mock_redis.return_value = MagicMock(setex=AsyncMock(), delete=AsyncMock())
            with pytest.raises(RuntimeError, match="simulated commit blip"):
                await svc.request_self_service_erasure(user_id="u-1")

        # Confirmation email DID fire (commit failed AFTER it).
        svc.email_service.send_erasure_confirmation.assert_awaited_once()
        # Receipt did NOT fire (it's after the failed commit).
        svc.email_service.send_erasure_receipt.assert_not_awaited()
        # Both rollback and Redis delete were attempted.
        svc.db.rollback.assert_awaited_once()
        mock_redis.return_value.delete.assert_awaited_once()
        # Structured log was emitted with the right event name and metadata.
        log_event_logged = any(
            call.args and call.args[0] == "erasure_request_commit_failed_after_side_effects"
            for call in mock_logger.error.call_args_list
        )
        assert log_event_logged, (
            "expected structured log 'erasure_request_commit_failed_after_side_effects'"
        )

    @pytest.mark.asyncio
    async def test_blocks_initial_admin(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user(is_initial_admin=True))
        svc._find_active_request = AsyncMock(return_value=None)

        with pytest.raises(InitialAdminCannotBeErasedError):
            await svc.request_self_service_erasure(user_id="u-1")

    @pytest.mark.asyncio
    async def test_blocks_when_active_request_exists(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())
        existing = SimpleNamespace(status=STATUS_COOLING_OFF)
        svc._find_active_request = AsyncMock(return_value=existing)

        with pytest.raises(ErasureAlreadyInProgressError):
            await svc.request_self_service_erasure(user_id="u-1")

    @pytest.mark.asyncio
    async def test_unknown_user_raises_not_found(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(side_effect=NotFoundException("User", "u-1"))

        with pytest.raises(NotFoundException):
            await svc.request_self_service_erasure(user_id="u-1")


# ---------------------------------------------------------------------------
# confirm_self_service
# ---------------------------------------------------------------------------


class TestConfirmSelfService:
    @pytest.mark.asyncio
    async def test_oauth_user_confirms_with_token_only(self):
        svc = _service()
        target = _user(auth_method="oauth")
        svc._load_user_or_404 = AsyncMock(return_value=target)
        # User owns no workspaces — workspace pre-check is a no-op.
        svc._check_no_blocking_workspace_transfers = AsyncMock()

        token = "raw-token-abc"
        request_id = uuid4()
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash=_sha256_hex(target.email),
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = request_id
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            redis_client = MagicMock()
            redis_client.get = AsyncMock(return_value=str(request_id))
            redis_client.delete = AsyncMock()
            mock_redis.return_value = redis_client

            await svc.confirm_self_service(user_id="u-1", token=token)

        assert request.status == STATUS_COOLING_OFF
        assert request.scheduled_for is not None
        # Cooling-off window equals the configured policy.
        assert (request.scheduled_for - request.confirmed_at) == COOLING_OFF_PERIOD
        redis_client.delete.assert_awaited_once()
        svc.email_service.send_erasure_cooling_off_started.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_missing_in_redis_raises_invalid(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(get=AsyncMock(return_value=None))
            with pytest.raises(ErasureTokenInvalidError):
                await svc.confirm_self_service(user_id="u-1", token="x")

    @pytest.mark.asyncio
    async def test_password_user_requires_password(self):
        svc = _service()
        target = _user(auth_method="password", password_hash="hashed")
        svc._load_user_or_404 = AsyncMock(return_value=target)

        token = "tok"
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with pytest.raises(ErasureForbiddenError):
                await svc.confirm_self_service(user_id="u-1", token=token, password=None)

    @pytest.mark.asyncio
    async def test_password_user_wrong_password_blocked(self):
        svc = _service()
        target = _user(auth_method="password", password_hash="hashed")
        svc._load_user_or_404 = AsyncMock(return_value=target)

        token = "tok"
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with patch("auth.password.verify_password", return_value=False):
                with pytest.raises(ErasureForbiddenError):
                    await svc.confirm_self_service(user_id="u-1", token=token, password="bad")

    @pytest.mark.asyncio
    async def test_token_for_other_user_rejected(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user(user_id="u-1"))

        token = "tok"
        request = ErasureRequest(
            user_id="u-OTHER",
            user_email_hash="x",
            initiated_by="u-OTHER",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with pytest.raises(ErasureTokenInvalidError):
                await svc.confirm_self_service(user_id="u-1", token=token)

    @pytest.mark.asyncio
    async def test_already_confirmed_request_rejected(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())

        token = "tok"
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_COOLING_OFF,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with pytest.raises(ErasureTokenInvalidError):
                await svc.confirm_self_service(user_id="u-1", token=token)


# ---------------------------------------------------------------------------
# cancel_self_service
# ---------------------------------------------------------------------------


class TestCancelSelfService:
    @pytest.mark.asyncio
    async def test_cooling_off_request_can_be_cancelled(self):
        svc = _service()
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_COOLING_OFF,
        )
        request.id = uuid4()
        svc._find_active_request = AsyncMock(return_value=request)

        result = await svc.cancel_self_service(user_id="u-1")

        assert result.status == STATUS_CANCELLED
        assert result.cancelled_at is not None

    @pytest.mark.asyncio
    async def test_no_active_request_raises_not_found(self):
        svc = _service()
        svc._find_active_request = AsyncMock(return_value=None)

        with pytest.raises(ErasureRequestNotFoundError):
            await svc.cancel_self_service(user_id="u-1")

    @pytest.mark.asyncio
    async def test_pending_request_can_be_cancelled(self):
        """Pending IS cancellable now (Copilot /review iter 2 finding).

        Without this, an unconfirmed pending row whose Redis token TTL
        has elapsed would block all future erasure requests via the
        partial unique index — user permanently wedged. Allow cancel
        to give the user immediate recourse.
        """
        svc = _service()
        pending = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
        )
        pending.id = uuid4()
        svc._find_active_request = AsyncMock(return_value=pending)

        result = await svc.cancel_self_service(user_id="u-1")
        assert result.status == STATUS_CANCELLED
        assert result.cancelled_at is not None


# ---------------------------------------------------------------------------
# admin_force_erase guard rails
# ---------------------------------------------------------------------------


class TestAdminForceErase:
    @pytest.mark.asyncio
    async def test_self_service_reason_rejected(self):
        svc = _service()
        with pytest.raises(ValidationError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code=REASON_SELF_SERVICE,
            )

    @pytest.mark.asyncio
    async def test_unknown_reason_code_rejected(self):
        svc = _service()
        with pytest.raises(ValidationError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code="not_a_real_code",
            )

    @pytest.mark.asyncio
    async def test_reason_detail_length_capped(self):
        svc = _service()
        with pytest.raises(ValidationError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="x" * 1001,
            )

    @pytest.mark.asyncio
    async def test_initial_admin_blocked(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user(is_initial_admin=True, role="admin"))

        with pytest.raises(InitialAdminCannotBeErasedError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            )

    @pytest.mark.asyncio
    async def test_last_admin_blocked(self):
        svc = _service()
        target = _user(role="admin")
        svc._load_user_or_404 = AsyncMock(return_value=target)
        with patch("services.account_erasure_service.SystemAdminService") as MockSvcCls:
            instance = MockSvcCls.return_value
            instance.can_delete_admin = AsyncMock(
                return_value=(False, "Cannot delete the last remaining system administrator")
            )
            with pytest.raises(ErasureForbiddenError):
                await svc.admin_force_erase(
                    target_user_id="u-1",
                    initiator_user_id="admin-2",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                )


# ---------------------------------------------------------------------------
# Workspace ownership transfer logic
# ---------------------------------------------------------------------------


class TestHandleOwnedWorkspaces:
    @staticmethod
    def _bulk_members_result(rows: list[SimpleNamespace]) -> MagicMock:
        """Build a MagicMock that mimics the bulk SELECT WorkspaceMember
        result the service now expects (one round-trip across all workspaces)."""
        result_obj = MagicMock()
        result_obj.scalars.return_value.all.return_value = rows
        return result_obj

    @pytest.mark.asyncio
    async def test_sole_owner_workspace_passes_through(self):
        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1")
        rows = [SimpleNamespace(workspace_id=ws_id, user_id="u-1", role="owner")]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        out = await svc._handle_owned_workspaces("u-1", [ws])
        assert out["sole_owner_workspaces"] == 1
        assert out["transferred"] == []

    @pytest.mark.asyncio
    async def test_other_admin_triggers_auto_transfer(self):
        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1")
        new_admin = SimpleNamespace(workspace_id=ws_id, user_id="u-2", role="admin")
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role="owner"),
            new_admin,
        ]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        out = await svc._handle_owned_workspaces("u-1", [ws])
        assert ws.owner_user_id == "u-2"
        assert new_admin.role == "owner"
        assert len(out["transferred"]) == 1

    @pytest.mark.asyncio
    async def test_members_without_admin_blocks_with_typed_error(self):
        from utils.exceptions import WorkspaceTransferRequiredError

        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1")
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role="owner"),
            SimpleNamespace(workspace_id=ws_id, user_id="u-2", role="member"),
            SimpleNamespace(workspace_id=ws_id, user_id="u-3", role="viewer"),
        ]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        with pytest.raises(WorkspaceTransferRequiredError) as exc_info:
            await svc._handle_owned_workspaces("u-1", [ws])
        # The error carries member_count so the API can surface it.
        assert exc_info.value.details["member_count"] == 2

    @pytest.mark.asyncio
    async def test_no_workspaces_short_circuits(self):
        svc = _service()
        # No bulk-load issued when there are no workspaces — verify execute
        # is never awaited.
        svc.db.execute = AsyncMock()
        out = await svc._handle_owned_workspaces("u-1", [])
        assert out == {"transferred": [], "sole_owner_workspaces": 0}
        svc.db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Helpers (sanity)
# ---------------------------------------------------------------------------


def test_sha256_hex_is_stable_and_salt_aware():
    assert _sha256_hex("a") == _sha256_hex("a")
    assert _sha256_hex("a") != _sha256_hex("a", salt="x")
    assert len(_sha256_hex("anything")) == 64


# ---------------------------------------------------------------------------
# AUDIT_PSEUDO_SALT settings sourcing + SessionManager accessor
# ---------------------------------------------------------------------------


def test_audit_salt_reads_from_settings(monkeypatch):
    """`_audit_salt()` must reflect the active Settings.audit_pseudo_salt."""
    from services import account_erasure_service

    fake_settings = SimpleNamespace(audit_pseudo_salt="prod-rotated-salt-2026Q2")
    monkeypatch.setattr(account_erasure_service, "get_settings", lambda: fake_settings)

    assert account_erasure_service._audit_salt() == "prod-rotated-salt-2026Q2"

    pseudo_a = _sha256_hex("u-1", salt=account_erasure_service._audit_salt())
    monkeypatch.setattr(
        account_erasure_service,
        "get_settings",
        lambda: SimpleNamespace(audit_pseudo_salt="different-salt"),
    )
    pseudo_b = _sha256_hex("u-1", salt=account_erasure_service._audit_salt())
    assert pseudo_a != pseudo_b, "rotating the salt must change downstream pseudonyms"


def test_get_session_manager_returns_module_state(monkeypatch):
    """`get_session_manager()` must expose the live `_session_manager` module attribute."""
    from api.routes import auth as auth_module

    sentinel = object()
    monkeypatch.setattr(auth_module, "_session_manager", sentinel)
    assert auth_module.get_session_manager() is sentinel

    monkeypatch.setattr(auth_module, "_session_manager", None)
    assert auth_module.get_session_manager() is None
