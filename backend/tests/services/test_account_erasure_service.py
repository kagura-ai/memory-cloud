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

import bcrypt
import pytest

from auth.workspace_roles import WorkspaceRole
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


@pytest.fixture(autouse=True)
def hand_over():
    """#1784: the Postgres sweep first passes the subject's private contexts
    to a linked account — queries the mocked sessions in this module do not
    script. The real step is covered in
    ``tests/integration/test_identity_links_db.py``."""
    with patch(
        "services.identity_link_service.hand_over_private_contexts",
        new=AsyncMock(return_value={"contexts_handed_over": 0, "identity_links_removed": 0}),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def owned_contracts():
    """#1940: no owned workspace under a billing contract unless a test sets
    ``owned_contracts.return_value``. The query itself is covered in
    ``tests/services/test_billing_contract.py``."""
    with patch(
        "services.account_erasure_service.owned_workspaces_under_contract",
        new=AsyncMock(return_value=[]),
    ) as mock:
        yield mock


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
    async def test_oauth_settings_failure_rolls_back_and_cleans_redis(self):
        """OAuth path: a failure during ``confirm_url`` construction
        (e.g. ``get_settings()`` raises, frontend_url access fails) AFTER
        Redis SETEX must still trigger rollback + Redis token delete.

        This guards against the regression where these lines lived
        outside the protected ``try`` and a bare exception would leave
        an open transaction + orphan Redis token (Loop 4 review).
        """
        svc = _service()
        target = _user(auth_method="oauth")
        self._wire_typical_path(svc, target)

        with (
            patch("services.account_erasure_service.get_redis_client") as mock_redis,
            patch(
                "services.account_erasure_service.get_settings",
                side_effect=RuntimeError("simulated settings failure"),
            ),
        ):
            mock_redis.return_value = MagicMock(setex=AsyncMock(), delete=AsyncMock())
            with pytest.raises(EmailDispatchError) as exc_info:
                await svc.request_self_service_erasure(user_id="u-1")

        assert exc_info.value.status_code == 503
        # Email send was NEVER attempted (failure happened before).
        svc.email_service.send_erasure_confirmation.assert_not_awaited()
        # But the protective handler still ran — rollback and Redis delete.
        svc.db.rollback.assert_awaited_once()
        svc.db.commit.assert_not_awaited()
        mock_redis.return_value.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_oauth_send_timeout_raises_dispatch_error(self):
        """OAuth path: a stalled email provider exceeding the
        ``CONFIRMATION_EMAIL_TIMEOUT_SECONDS`` bound (``asyncio.wait_for``)
        must trigger the same rollback + Redis cleanup + ``EmailDispatchError``
        as a thrown SDK exception. This protects the DB connection pool
        from long-stalled requests during provider outages.

        The simulation raises ``asyncio.TimeoutError`` directly from the
        AsyncMock — semantically equivalent to ``wait_for`` exhausting
        the timeout, since both surface the same exception type to the
        caller's ``except Exception`` handler.
        """
        svc = _service()
        svc.email_service.send_erasure_confirmation = AsyncMock(
            side_effect=TimeoutError("simulated provider stall")
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
        svc.db.rollback.assert_awaited_once()
        svc.db.commit.assert_not_awaited()
        mock_redis.return_value.delete.assert_awaited_once()
        # Verify the structured log captures error_type="TimeoutError" so
        # ops can distinguish stall-induced rollbacks from generic SDK errors.
        haystack_parts: list[str] = []
        for call in mock_logger.method_calls:
            _name, args, kwargs = call
            haystack_parts.extend(str(a) for a in args)
            for k, v in kwargs.items():
                haystack_parts.append(str(k))
                haystack_parts.append(str(v))
        haystack = " ".join(haystack_parts)
        assert "TimeoutError" in haystack

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
        rows = [SimpleNamespace(workspace_id=ws_id, user_id="u-1", role=WorkspaceRole.OWNER)]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        out = await svc._handle_owned_workspaces("u-1", [ws])
        assert out["sole_owner_workspaces"] == 1
        assert out["transferred"] == []

    @pytest.mark.asyncio
    async def test_other_admin_triggers_auto_transfer(self):
        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1", ownership_epoch=5)
        new_admin = SimpleNamespace(workspace_id=ws_id, user_id="u-2", role=WorkspaceRole.ADMIN)
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role=WorkspaceRole.OWNER),
            new_admin,
        ]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        # #1102: the auto-transfer takes the SHARED workspace-row lock (returning
        # the same locked row) so it serializes with transfer_ownership.
        with patch(
            "services.account_erasure_service.lock_workspace_for_update",
            AsyncMock(return_value=ws),
        ) as lock:
            out = await svc._handle_owned_workspaces("u-1", [ws])
        lock.assert_awaited_once()
        assert ws.owner_user_id == "u-2"
        assert new_admin.role == "owner"
        assert len(out["transferred"]) == 1
        # #1102: the erasure auto-transfer bumps ownership_epoch so the #1100
        # consumer invalidates credentials bound to the erased previous owner.
        assert ws.ownership_epoch == 6

    @pytest.mark.asyncio
    async def test_auto_transfer_skips_when_ownership_moved_under_lock(self):
        # #1102: if a concurrent transfer moved ownership away from the erased user
        # before we acquired the lock, the locked row no longer names them as owner
        # → skip the auto-transfer rather than clobber the new owner.
        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1", ownership_epoch=5)
        # The row as re-read UNDER the lock: ownership already moved to someone else.
        locked = SimpleNamespace(id=ws_id, owner_user_id="someone-else", ownership_epoch=9)
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role=WorkspaceRole.OWNER),
            SimpleNamespace(workspace_id=ws_id, user_id="u-2", role=WorkspaceRole.ADMIN),
        ]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        with patch(
            "services.account_erasure_service.lock_workspace_for_update",
            AsyncMock(return_value=locked),
        ):
            out = await svc._handle_owned_workspaces("u-1", [ws])

        assert out["transferred"] == []
        assert locked.owner_user_id == "someone-else"  # untouched
        assert locked.ownership_epoch == 9  # not bumped

    @pytest.mark.asyncio
    async def test_soft_deleted_workspace_skipped_not_failed(self):
        # #1102: if a workspace was soft-deleted concurrently, the shared lock
        # raises NotFoundException — that benign race must be skipped, NOT allowed
        # to fail the whole GDPR erasure request.
        from utils.exceptions import NotFoundException

        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1", ownership_epoch=5)
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role=WorkspaceRole.OWNER),
            SimpleNamespace(workspace_id=ws_id, user_id="u-2", role=WorkspaceRole.ADMIN),
        ]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        with patch(
            "services.account_erasure_service.lock_workspace_for_update",
            AsyncMock(side_effect=NotFoundException("Workspace")),
        ):
            out = await svc._handle_owned_workspaces("u-1", [ws])

        assert out["transferred"] == []  # skipped, no exception bubbled

    @pytest.mark.asyncio
    async def test_members_without_admin_blocks_with_typed_error(self):
        from utils.exceptions import WorkspaceTransferRequiredError

        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1")
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role=WorkspaceRole.OWNER),
            SimpleNamespace(workspace_id=ws_id, user_id="u-2", role=WorkspaceRole.MEMBER),
            SimpleNamespace(workspace_id=ws_id, user_id="u-3", role=WorkspaceRole.VIEWER),
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


def test_startup_registers_the_session_manager_beside_the_store(monkeypatch):
    """The service reads the manager from ``auth.session``, not a route module (#1809)."""
    from api.routes import auth as auth_module
    from auth import session as session_module
    from services import account_erasure_service

    # Snapshot the globals startup writes; monkeypatch restores them.
    monkeypatch.setattr(session_module, "_active_session_manager", None)
    monkeypatch.setattr(auth_module, "_session_manager", auth_module._session_manager)
    monkeypatch.setattr(auth_module, "_oauth2_manager", auth_module._oauth2_manager)

    sentinel = object()
    auth_module.initialize_auth_routes(None, sentinel)  # type: ignore[arg-type]
    assert session_module.get_session_manager() is sentinel
    assert account_erasure_service.get_session_manager() is sentinel


class TestDeletePostgresSweep:
    """#1228: the per-user Postgres sweep must include the diagnostic
    context_read_attributions table — attribution rows where the erased
    user read a context in ANOTHER user's workspace survive the
    workspace-cascade deletion, so the user_id sweep is the only
    mechanism removing them on GDPR erasure."""

    @pytest.mark.asyncio
    async def test_sweep_covers_context_read_attributions(self):
        from models.auth import ContextReadAttribution

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=3)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = await svc._delete_postgres(target)

        assert counts["context_read_attributions"] == 3
        swept_models = [call.args[0] for call in svc._count_and_delete.await_args_list]
        assert ContextReadAttribution in swept_models

    @pytest.mark.asyncio
    async def test_sweep_covers_known_devices(self):
        # #1769: the FK cascades, but the sweep deletes and counts them so the
        # erasure summary lists the browsers that were forgotten.
        from models.auth import UserKnownDevice

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=2)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()

        counts = await svc._delete_postgres(_user())

        assert counts["user_known_devices"] == 2
        swept_models = [call.args[0] for call in svc._count_and_delete.await_args_list]
        assert UserKnownDevice in swept_models

    @pytest.mark.asyncio
    async def test_sweep_pseudonymizes_surviving_agents(self):
        """#1274 (RFC-0002 P0-1): agents.owner_user_id must be pseudonymized
        for erased subjects whose registry rows survive (agents in co-owned,
        transferred workspaces — sole-owner workspaces cascade the rows away
        before this step)."""
        from models.agent import Agent

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=2)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = await svc._delete_postgres(target)

        assert counts["agents_pseudonymized"] == 2
        pseudonymized = [call.args[0] for call in svc._pseudonymize_field.await_args_list]
        assert Agent in pseudonymized
        agent_call = next(
            call for call in svc._pseudonymize_field.await_args_list if call.args[0] is Agent
        )
        assert agent_call.args[1] is Agent.owner_user_id
        assert agent_call.args[2] == target.user_id

    @pytest.mark.asyncio
    async def test_sweep_pseudonymizes_worker_app_identity_actor_columns(self):
        """#1358: worker_app_identities are GLOBAL control-plane rows (no
        workspace FK, so no cascade ever removes them) — created_by /
        updated_by hold the operator's raw OAuth sub and must be
        pseudonymized on erasure (plan_changes/agents legal-retention
        posture: row survives, personal link breaks)."""
        from models.worker_app import WorkerAppIdentity

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)

        # Distinct per-column values pin the SUM semantics exactly — a
        # refactor that doubles one sweep (2 * created_by) would still
        # produce 2 under a uniform return_value.
        async def _by_column(model, column, user_id, extra_values=None):
            if model is WorkerAppIdentity and column is WorkerAppIdentity.created_by:
                return 1
            if model is WorkerAppIdentity and column is WorkerAppIdentity.updated_by:
                return 2
            return 0

        svc._pseudonymize_field = AsyncMock(side_effect=_by_column)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = await svc._delete_postgres(target)

        assert counts["worker_app_identities_pseudonymized"] == 3
        calls = [
            c for c in svc._pseudonymize_field.await_args_list if c.args[0] is WorkerAppIdentity
        ]
        assert len(calls) == 2
        assert any(c.args[1] is WorkerAppIdentity.created_by for c in calls)
        assert any(c.args[1] is WorkerAppIdentity.updated_by for c in calls)
        assert all(c.args[2] == target.user_id for c in calls)


class TestMemoryAccessEventsErasure:
    """#1278: the erased subject's memory_access_events rows are pseudonymized
    + scrubbed in ONE carve-out UPDATE (user_id pseudonym, session_id/run_id
    NULL, event_metadata redacted)."""

    @pytest.mark.asyncio
    async def test_erasure_pseudonymizes_and_scrubs(self):

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        captured = {}

        async def _exec(stmt):
            captured["stmt"] = stmt
            return SimpleNamespace(rowcount=4)

        svc.db.execute = AsyncMock(side_effect=_exec)
        target = _user()

        count = await svc._erase_memory_access_events(target.user_id)
        assert count == 4
        # A single UPDATE on memory_access_events touching only carve-out cols.
        compiled = str(captured["stmt"]).lower()
        assert "update memory_access_events" in compiled
        params = captured["stmt"].compile().params
        assert params["user_id"] != target.user_id  # pseudonymized
        assert params["session_id"] is None
        assert params["run_id"] is None


class TestErasureResiduals1365:
    """#1365: the four raw-sub residual families left by #1358."""

    @pytest.mark.asyncio
    async def test_sweep_pseudonymizes_config_overrides_updated_by(self):
        from models.config import ConfigOverride

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc._erase_secret_access_log = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = await svc._delete_postgres(target)

        assert "config_overrides_pseudonymized" in counts
        calls = [c for c in svc._pseudonymize_field.await_args_list if c.args[0] is ConfigOverride]
        assert len(calls) == 1
        assert calls[0].args[1] is ConfigOverride.updated_by
        assert calls[0].args[2] == target.user_id

    @pytest.mark.asyncio
    async def test_sweep_pseudonymizes_all_authorship_columns(self):
        """All 16 surviving-row authorship columns are swept, and the
        count is their sum (distinct per-column values pin the SUM).

        The last four (identity_id, ContextMember/WorkspaceMember.invited_by,
        WorkspaceInvitation.accepted_by) were added by the v0.54..v0.57 review
        (F7/F8/F9) — surviving-row identity links the earlier sweep missed."""
        from models.auth import (
            Context,
            ContextMember,
            ExternalAPIKey,
            WorkspaceInvitation,
            WorkspaceMember,
        )
        from models.file_objects import FileObject
        from models.resource import (
            Resource,
            ResourceToken,
            WorkspaceAddon,
            WorkspaceConnector,
        )
        from models.secrets import (
            RecipientPubkey,
            Secret,
            SecretGrant,
            SecretVersion,
        )

        expected = {
            (WorkspaceConnector, "created_by"): 1,
            (Resource, "created_by"): 2,
            (ResourceToken, "created_by"): 3,
            (WorkspaceAddon, "created_by"): 4,
            (FileObject, "created_by"): 5,
            (Secret, "created_by"): 6,
            (SecretVersion, "created_by"): 7,
            (RecipientPubkey, "created_by"): 8,
            (RecipientPubkey, "attested_by"): 9,
            (SecretGrant, "granted_by"): 10,
            (Context, "created_by"): 11,
            (ExternalAPIKey, "updated_by"): 12,
            (RecipientPubkey, "identity_id"): 13,
            (ContextMember, "invited_by"): 14,
            (WorkspaceMember, "invited_by"): 15,
            (WorkspaceInvitation, "accepted_by"): 16,
        }

        async def _by_column(model, column, user_id, extra_values=None):
            return expected.get((model, column.key), 0)

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(side_effect=_by_column)
        svc._erase_secret_access_log = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = await svc._delete_postgres(target)

        assert counts["authorship_columns_pseudonymized"] == sum(expected.values())
        swept = {
            (c.args[0], c.args[1].key)
            for c in svc._pseudonymize_field.await_args_list
            if (c.args[0], c.args[1].key) in expected
        }
        assert swept == set(expected)

    @pytest.mark.asyncio
    async def test_sweep_deletes_new_no_cascade_per_user_tables(self):
        """v0.54..v0.57 review sweep: per-user rows with no user-scoped cascade
        (or dead ACL grants) must be deleted so the raw sub can't outlive
        erasure — context_members (F8), oauth_device_codes, llm_call_logs."""
        from unittest.mock import patch

        from models.auth import ContextMember
        from models.llm_call_log import LLMCallLog
        from services.oauth_grant_revocation import RevokedGrants

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc._erase_secret_access_log = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        # #1770: the OAuth codes and device codes go through the shared
        # revoker (users → codes → device codes → tokens), whose counts land
        # in the summary.
        revoke = AsyncMock(
            return_value=RevokedGrants(authorization_codes=1, device_codes=2, tokens=3)
        )
        with patch("services.account_erasure_service.revoke_oauth_grants", revoke):
            counts = await svc._delete_postgres(target)

        revoke.assert_awaited_once_with(svc.db, target.user_id, delete_tokens=True)
        assert counts["oauth_authorization_codes"] == 1
        assert counts["oauth_device_codes"] == 2
        assert counts["oauth_tokens"] == 3
        deleted_models = {c.args[0] for c in svc._count_and_delete.await_args_list}
        assert ContextMember in deleted_models
        assert LLMCallLog in deleted_models

    @pytest.mark.asyncio
    async def test_sweep_pseudonymizes_surviving_graph_and_analysis(self):
        """v0.54..v0.57 review sweep: neural edges, analysis runs, retrieval
        feedback, and sleep reports that outlive the sole-owner cascade in
        co-owned/transferred workspaces must have the subject link pseudonymized
        (same deterministic pseudonym as the scrubbed memories)."""
        from models.analysis import MemoryAnalysis
        from models.memory import NeuralMemoryEdge
        from models.retrieval_feedback import RetrievalFeedback
        from models.sleep import SleepReport

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc._erase_secret_access_log = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        await svc._delete_postgres(target)

        pseudonymized = {
            (c.args[0], c.args[1].key) for c in svc._pseudonymize_field.await_args_list
        }
        assert (NeuralMemoryEdge, "user_id") in pseudonymized
        assert (MemoryAnalysis, "triggered_by") in pseudonymized
        assert (RetrievalFeedback, "user_id") in pseudonymized
        assert (SleepReport, "user_id") in pseudonymized

    @pytest.mark.asyncio
    async def test_finalize_pseudonymizes_request_self_row(self):
        """F10: on completion the retained erasure_requests row's own user_id is
        pseudonymized and ip/user_agent nulled; self-service also pseudonymizes
        initiated_by (the subject's own sub). The raw sub must not survive."""
        svc = _service()
        captured: dict = {}

        async def _exec(stmt):
            captured["stmt"] = stmt
            return SimpleNamespace(rowcount=1)

        svc.db.execute = AsyncMock(side_effect=_exec)
        svc.db.commit = AsyncMock()
        request = SimpleNamespace(id=uuid4(), user_id="oauth-sub-123", initiated_by="oauth-sub-123")

        await svc._finalize(request, {"users": 1})

        params = captured["stmt"].compile().params
        assert params["user_id"] != "oauth-sub-123"  # pseudonymized
        assert params["initiated_by"] != "oauth-sub-123"  # self-service pseudonymized
        assert params["ip_address"] is None
        assert params["user_agent"] is None

    @pytest.mark.asyncio
    async def test_finalize_keeps_admin_initiator_on_force_erase(self):
        """F10: on an admin force-erase, initiated_by is the acting admin's sub
        (legitimate accountability evidence) and must be kept, while the erased
        subject's own user_id is still pseudonymized."""
        svc = _service()
        captured: dict = {}

        async def _exec(stmt):
            captured["stmt"] = stmt
            return SimpleNamespace(rowcount=1)

        svc.db.execute = AsyncMock(side_effect=_exec)
        svc.db.commit = AsyncMock()
        request = SimpleNamespace(id=uuid4(), user_id="erased-sub", initiated_by="admin-sub")

        await svc._finalize(request, {"users": 1})

        params = captured["stmt"].compile().params
        assert params["user_id"] != "erased-sub"  # pseudonymized
        assert "initiated_by" not in params  # admin sub kept out of the UPDATE

    @pytest.mark.asyncio
    async def test_sweep_calls_secret_access_log_erasure(self):
        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc._erase_secret_access_log = AsyncMock(return_value=5)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = await svc._delete_postgres(target)

        assert counts["secret_access_log_pseudonymized"] == 5
        svc._erase_secret_access_log.assert_awaited_once_with(target.user_id)

    @pytest.mark.asyncio
    async def test_erase_secret_access_log_touches_only_carveout_columns(self):
        """Two UPDATEs (actor / recipient), each touching ONLY its own
        carve-out identity column — anything else would be rejected by
        the e72 append-only trigger in production."""
        svc = _service()
        captured = []

        async def _exec(stmt):
            captured.append(stmt)
            return SimpleNamespace(rowcount=2)

        svc.db.execute = AsyncMock(side_effect=_exec)
        target = _user()

        count = await svc._erase_secret_access_log(target.user_id)

        assert count == 4
        assert len(captured) == 2
        for stmt in captured:
            compiled = str(stmt).lower()
            assert "update secret_access_log" in compiled
        actor_params = captured[0].compile().params
        assert actor_params["actor_user_id"] != target.user_id  # pseudonymized
        recipient_params = captured[1].compile().params
        assert recipient_params["recipient_identity"] != target.user_id


class TestConfirmSelfServiceLongPassword:
    """Issue #1707: a > 72-byte password is a refusal, never a bcrypt 5 ValueError."""

    @staticmethod
    def _pending(svc: AccountErasureService, token: str) -> ErasureRequest:
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
        return request

    @pytest.mark.asyncio
    async def test_over_long_wrong_password_is_incorrect_password(self):
        svc = _service()
        stored = bcrypt.hashpw(b"Correct-Horse-1!", bcrypt.gensalt(rounds=4)).decode()
        svc._load_user_or_404 = AsyncMock(
            return_value=_user(auth_method="password", password_hash=stored)
        )
        token = "tok"
        request = self._pending(svc, token)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with pytest.raises(ErasureForbiddenError, match="Incorrect password"):
                await svc.confirm_self_service(user_id="u-1", token=token, password="y" * 100)


class TestErasureFollowsHasPassword1678:
    """#1678: the second factor follows ``password_hash``, not ``auth_method``."""

    @pytest.mark.asyncio
    async def test_oauth_user_with_password_gets_the_token_in_the_response(self):
        svc = _service()
        target = _user(auth_method="oauth", password_hash="hashed")
        TestRequestSelfServiceErasure._wire_typical_path(svc, target)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(setex=AsyncMock(), delete=AsyncMock())
            _, response_token = await svc.request_self_service_erasure(user_id="u-1")

        assert response_token is not None
        svc.email_service.send_erasure_confirmation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oauth_user_with_password_must_reenter_it(self):
        svc = _service()
        target = _user(auth_method="oauth", password_hash="hashed")
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
                get=AsyncMock(return_value=str(request.id)), delete=AsyncMock()
            )
            with pytest.raises(ErasureForbiddenError):
                await svc.confirm_self_service(user_id="u-1", token=token, password=None)


class TestEmailLinkErasureStaysConfirmable1678:
    """#1678: the second factor is decided when the erasure is REQUESTED.

    An emailed link is the proof for an account that had no password then;
    setting a password before clicking it must not break the link. A request
    made with a password (token in the response) still needs the password.
    """

    class _Redis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        async def setex(self, key: str, ttl: int, value: str) -> None:
            self.store[key] = value

        async def get(self, key: str) -> str | None:
            return self.store.get(key)

        async def delete(self, key: str) -> None:
            self.store.pop(key, None)

    async def _request_then_confirm(
        self, *, password_at_request: str | None, password_at_confirm: str | None, **confirm
    ) -> tuple[AccountErasureService, ErasureRequest]:
        svc = _service()
        target = _user(auth_method="oauth", password_hash=password_at_request)
        added = TestRequestSelfServiceErasure._wire_typical_path(svc, target)
        redis = self._Redis()
        with patch("services.account_erasure_service.get_redis_client", return_value=redis):
            _, response_token = await svc.request_self_service_erasure(user_id="u-1")
            request = added[0]
            token = (
                response_token
                or (svc.email_service.send_erasure_confirmation.await_args.kwargs["confirm_token"])
            )
            # Between the request and the click the password state changes.
            target.password_hash = password_at_confirm
            request.status = STATUS_PENDING
            svc._load_request_or_404 = AsyncMock(return_value=request)
            svc._check_no_blocking_workspace_transfers = AsyncMock()
            await svc.confirm_self_service(user_id="u-1", token=token, **confirm)
        return svc, request

    @pytest.mark.asyncio
    async def test_email_link_confirms_after_a_password_was_set(self):
        _, request = await self._request_then_confirm(
            password_at_request=None, password_at_confirm="hashed-since"
        )
        assert request.status == STATUS_COOLING_OFF

    @pytest.mark.asyncio
    async def test_password_request_still_needs_the_password(self):
        with pytest.raises(ErasureForbiddenError):
            await self._request_then_confirm(
                password_at_request="hashed", password_at_confirm="hashed", password=None
            )


# ---------------------------------------------------------------------------
# Billing contract blocks self-service erasure (Issue #1940)
# ---------------------------------------------------------------------------


def _contract_workspace(*, deleted: bool = False) -> SimpleNamespace:
    from utils.datetime import utcnow

    return SimpleNamespace(
        id=uuid4(),
        plan_name="pro",
        has_active_billing_contract=True,
        deleted_at=utcnow() if deleted else None,
    )


def _execute_request(*, self_service: bool) -> ErasureRequest:
    request = ErasureRequest(
        user_id="u-1",
        user_email_hash=_sha256_hex("alice@example.com"),
        initiated_by="u-1" if self_service else "admin-1",
        is_self_service=self_service,
        reason_code=REASON_SELF_SERVICE if self_service else REASON_USER_REQUEST_VIA_SUPPORT,
        status=STATUS_COOLING_OFF if self_service else "in_progress",
    )
    request.id = uuid4()
    return request


def _stub_execute_steps(svc: AccountErasureService) -> None:
    svc._handle_owned_workspaces = AsyncMock(return_value={})
    svc._delete_postgres = AsyncMock(return_value={})
    svc._pseudonymize_audit_logs = AsyncMock(return_value=0)
    svc._clear_redis = AsyncMock(return_value={})
    svc._write_audit_log = AsyncMock()
    svc._finalize = AsyncMock()


class TestBillingContractBlocksErasure:
    @pytest.mark.asyncio
    async def test_request_refused_while_owning_contract_workspace(self, owned_contracts):
        from utils.exceptions import BillingContractActiveError

        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())
        svc._find_active_request = AsyncMock(return_value=None)
        ws = _contract_workspace()
        owned_contracts.return_value = [ws]

        with pytest.raises(BillingContractActiveError) as exc:
            await svc.request_self_service_erasure(user_id="u-1")

        assert exc.value.status_code == 409
        assert exc.value.details["workspace_ids"] == [str(ws.id)]
        svc.db.add.assert_not_called()
        svc.email_service.send_erasure_confirmation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_keeps_reporting_an_active_request_first(self, owned_contracts):
        """A scheduled (cooling_off) erasure stays visible as such; the sweep
        is what fails it if the contract is still running then."""
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())
        svc._find_active_request = AsyncMock(
            return_value=SimpleNamespace(status=STATUS_COOLING_OFF)
        )
        owned_contracts.return_value = [_contract_workspace()]

        with pytest.raises(ErasureAlreadyInProgressError):
            await svc.request_self_service_erasure(user_id="u-1")

    @pytest.mark.asyncio
    async def test_confirm_refused_cancels_the_pending_request(self, owned_contracts):
        """Refusing confirm also cancels the request and burns its token, so a
        fresh request works once the contract has ended (no dead pending row)."""
        from utils.exceptions import BillingContractActiveError

        svc = _service()
        target = _user(auth_method="oauth")
        svc._load_user_or_404 = AsyncMock(return_value=target)
        svc._check_no_blocking_workspace_transfers = AsyncMock()
        owned_contracts.return_value = [_contract_workspace()]

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

            with pytest.raises(BillingContractActiveError):
                await svc.confirm_self_service(user_id="u-1", token=token)

        assert request.status == STATUS_CANCELLED
        assert request.cancelled_at is not None
        assert request.scheduled_for is None
        svc.db.commit.assert_awaited_once()
        redis_client.delete.assert_awaited_once()
        svc.email_service.send_erasure_cooling_off_started.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_execution_fails_request_when_contract_started_in_cooling_off(self):
        from models.erasure import STATUS_FAILED
        from utils.exceptions import BillingContractActiveError

        svc = _service()
        svc._list_owned_workspaces = AsyncMock(return_value=[_contract_workspace()])
        request = _execute_request(self_service=True)

        with patch(
            "services.account_erasure_service.delete_user_points", new=AsyncMock()
        ) as qdrant_delete:
            with pytest.raises(BillingContractActiveError):
                await svc._execute(request, _user())

        qdrant_delete.assert_not_awaited()
        failed_update = svc.db.execute.await_args.args[0]
        params = failed_update.compile().params
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"].startswith("billing_contract_active")

    @pytest.mark.asyncio
    async def test_sweep_ignores_a_soft_deleted_contract_workspace(self):
        """Only live workspaces block; a deleted one is hard-deleted with the
        account (billing's read of it already answers 404 → cancel)."""
        svc = _service()
        svc._list_owned_workspaces = AsyncMock(return_value=[_contract_workspace(deleted=True)])
        _stub_execute_steps(svc)

        with patch("services.account_erasure_service.delete_user_points", new=AsyncMock()):
            await svc._execute(_execute_request(self_service=True), _user())

        svc._finalize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_admin_force_erase_proceeds_and_logs_after_completion(self):
        svc = _service()
        live, deleted = _contract_workspace(), _contract_workspace(deleted=True)
        svc._list_owned_workspaces = AsyncMock(return_value=[live, deleted])
        _stub_execute_steps(svc)
        order: list[str] = []
        svc._finalize = AsyncMock(side_effect=lambda *a: order.append("finalize"))

        with (
            patch("services.account_erasure_service.delete_user_points", new=AsyncMock()),
            patch("services.account_erasure_service.logger") as mock_logger,
        ):
            mock_logger.warning.side_effect = lambda *a, **k: order.append(a[0])
            await svc._execute(_execute_request(self_service=False), _user())

        override = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "erasure_admin_override_billing_contract"
        ]
        assert len(override) == 1
        # Soft-deleted ones are hard-deleted too, so they are part of the log.
        assert override[0].kwargs["workspace_ids"] == [str(live.id), str(deleted.id)]
        assert order.index("finalize") < order.index("erasure_admin_override_billing_contract")
