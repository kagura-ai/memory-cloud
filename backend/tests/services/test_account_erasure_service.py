"""Unit tests for AccountErasureService (Issue #360, GDPR right-to-erasure).

These tests target the state-machine + guard logic in the service layer.
The cross-store deletion pipeline (`_execute`) requires real Qdrant +
Redis + Postgres containers and is exercised separately via
``make test-integration`` with the migrations applied; the unit tests
here use mocks for the data-layer collaborators so they stay fast and
deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
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
def restore_details():
    """#2244: the Postgres sweep puts back the details the subject wrote on
    other authors' memories — queries (and a vector-store patch) the mocked
    sessions in this module do not script. The real step is covered in
    ``tests/integration/test_erasure_details_db.py``."""
    from services.erasure_details import RestoreReport, StripCounts, SubjectDetailsErasure

    # #2268: restore and strip run in one step (``erase_subject_details``);
    # the walk itself is covered in ``tests/services/test_erasure_details.py``
    with (
        patch(
            "services.account_erasure_service.erase_subject_details",
            new=AsyncMock(return_value=SubjectDetailsErasure(RestoreReport(), StripCounts(0, 0))),
        ) as mock,
        patch("services.erasure_details._updated_rows", new=AsyncMock(return_value=[])),
        patch(
            "services.account_erasure_service.erased_pseudonyms",
            new=AsyncMock(return_value=frozenset()),
        ),
    ):
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
    db.execute = AsyncMock(return_value=MagicMock())  # a result, with sync methods
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


def _lost_cas() -> MagicMock:
    """The cancel's compare-and-set result when it matched nothing: no
    ``RETURNING`` row (#2304)."""
    result = MagicMock(rowcount=0)
    result.first.return_value = None
    return result


def _cancel_update_params(svc: AccountErasureService) -> dict:
    """The params of the cancel's compare-and-set UPDATE (#2282 review)."""
    from sqlalchemy.sql.dml import Update

    (stmt,) = [c.args[0] for c in svc.db.execute.await_args_list if isinstance(c.args[0], Update)]
    assert "erasure_requests.status IN (__[POSTCOMPILE_status_1])" in str(stmt)
    return stmt.compile().params


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

        assert result is request
        params = _cancel_update_params(svc)
        assert params["id_1"] == request.id
        assert params["status_1"] == [STATUS_PENDING, STATUS_COOLING_OFF]
        assert params["status"] == STATUS_CANCELLED
        assert params["cancelled_at"] is not None
        svc.db.commit.assert_awaited_once()
        svc.db.refresh.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_a_request_claimed_meanwhile_is_not_overwritten(self):
        """#2282 review: the sweep claimed the row between the read and the
        cancel's write; the compare-and-set matches nothing and the cancel
        answers 409 with what the row is now."""
        from models.erasure import STATUS_IN_PROGRESS

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
        claimed = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_IN_PROGRESS,
        )
        claimed.id = request.id
        now_row = MagicMock()
        now_row.scalar_one_or_none.return_value = claimed
        svc.db.execute = AsyncMock(side_effect=[_lost_cas(), now_row])

        with pytest.raises(ErasureAlreadyInProgressError) as caught:
            await svc.cancel_self_service(user_id="u-1")

        assert caught.value.status_code == 409
        assert caught.value.details == {"existing_status": STATUS_IN_PROGRESS}
        svc.db.refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_admin_request_cannot_be_cancelled_by_its_subject(self):
        """#2282: an admin force-erase waiting for a vector-store retry is
        ``cooling_off`` too; the erased user must not call it off."""
        svc = _service()
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="admin-1",
            is_self_service=False,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            status=STATUS_COOLING_OFF,
        )
        request.id = uuid4()
        svc._find_active_request = AsyncMock(return_value=request)

        with pytest.raises(ErasureRequestNotFoundError):
            await svc.cancel_self_service(user_id="u-1")

        assert request.status == STATUS_COOLING_OFF
        assert request.cancelled_at is None
        svc.db.commit.assert_not_awaited()

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
        assert result is pending
        assert _cancel_update_params(svc)["status"] == STATUS_CANCELLED


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

        counts = (await svc._delete_postgres(target)).counts

        assert counts["context_read_attributions"] == 3
        swept_models = [call.args[0] for call in svc._count_and_delete.await_args_list]
        assert ContextReadAttribution in swept_models

    @pytest.mark.asyncio
    async def test_restore_never_puts_back_an_earlier_erased_members_value(self, restore_details):
        # #2243 review S1: the live erasure passes every completed erasure's
        # pseudonym, as the reprocess command does (one shared helper).
        erased = frozenset({"e" * 64})
        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()

        with patch(
            "services.account_erasure_service.erased_pseudonyms",
            new=AsyncMock(return_value=erased),
        ) as loaded:
            await svc._delete_postgres(_user())

        loaded.assert_awaited_once_with(svc.db)
        assert restore_details.await_args.kwargs["erased_actors"] == erased

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

        counts = (await svc._delete_postgres(_user())).counts

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

        counts = (await svc._delete_postgres(target)).counts

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
        async def _by_column(model, column, user_id):
            if model is WorkerAppIdentity and column is WorkerAppIdentity.created_by:
                return 1
            if model is WorkerAppIdentity and column is WorkerAppIdentity.updated_by:
                return 2
            return 0

        svc._pseudonymize_field = AsyncMock(side_effect=_by_column)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = (await svc._delete_postgres(target)).counts

        assert counts["worker_app_identities_pseudonymized"] == 3
        calls = [
            c for c in svc._pseudonymize_field.await_args_list if c.args[0] is WorkerAppIdentity
        ]
        assert len(calls) == 2
        assert any(c.args[1] is WorkerAppIdentity.created_by for c in calls)
        assert any(c.args[1] is WorkerAppIdentity.updated_by for c in calls)
        assert all(c.args[2] == target.user_id for c in calls)


class TestRestoreEditorDetailsWiring:
    """#2244: the restore runs first among the memory steps, by the raw sub,
    with the pseudonym as the new rows' actor, and its counts reach the
    erasure summary."""

    @pytest.mark.asyncio
    async def test_restore_runs_before_the_revision_scrub_and_is_counted(
        self, restore_details, hand_over
    ):
        from services.erasure_details import RestoreReport, StripCounts, SubjectDetailsErasure

        order: list[str] = []
        restore_details.side_effect = lambda *a, **k: (
            order.append("restore"),
            SubjectDetailsErasure(
                RestoreReport(restored=2, unavailable=1, embedding_requeued=3), StripCounts(3, 1)
            ),
        )[1]
        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()

        async def _provenance(_user_id):
            order.append("provenance")
            return {"memory_sessions_scrubbed": 0, "session_rollbacks_pseudonymized": 0}

        async def _revisions(_user_id, stripped):
            order.append("revisions")
            assert stripped == StripCounts(3, 1)  # #2268: from the same step
            return {"memory_revisions_pseudonymized": 4, "revision_details_stripped": 3}

        async def _lock(_db, _user_id, *, deleted_by=False):
            order.append("lock")
            assert deleted_by  # #2263: the memories the subject deleted too
            return 0

        async def _tombstones(_user_id):
            order.append("tombstones")
            return 2

        svc._erase_session_provenance = AsyncMock(side_effect=_provenance)
        svc._erase_memory_revisions = AsyncMock(side_effect=_revisions)
        svc._delete_subject_tombstones = AsyncMock(side_effect=_tombstones)

        async def _execute(statement, *args, **kwargs):
            sql = " ".join(str(statement).split())
            if sql.startswith("SELECT pg_advisory_xact_lock"):
                order.append("link_lock")
            if sql.startswith("SELECT contexts_1.id FROM contexts AS contexts_1"):
                # #2278: one phase over the contexts the subject created or deleted
                assert sql.endswith(
                    "WHERE contexts_1.created_by = :created_by_1 "
                    "OR contexts_1.deleted_by = :deleted_by_1 ORDER BY contexts_1.id FOR UPDATE"
                )
                assert statement.compile().params == {
                    "created_by_1": "raw-sub",
                    "deleted_by_1": "raw-sub",
                }
                order.append("contexts_lock")
            return MagicMock()

        svc.db.execute = AsyncMock(side_effect=_execute)
        hand_over.side_effect = lambda *a: (
            order.append("hand_over"),
            {"contexts_handed_over": 0, "identity_links_removed": 0},
        )[1]
        target = _user(user_id="raw-sub")

        with (
            patch("services.account_erasure_service._audit_salt", return_value="salt"),
            patch(
                "services.account_erasure_service.lock_subject_memories",
                new=AsyncMock(side_effect=_lock),
            ),
        ):
            counts = (await svc._delete_postgres(target)).counts
            pseudonym = svc._pseudonym("raw-sub")

        # #2263: the subject's tombstones go right after the memory lock, so
        # no revision statement below meets their rows (the FK cascade takes
        # their revisions)
        # #2263, #2278: the contexts the subject created or deleted are locked
        # first, after the link lock and before the hand-over's memory DELETE —
        # the order ``restore_deleted_context`` takes (context, then memories)
        assert order == [
            "link_lock",
            "contexts_lock",
            "hand_over",
            "lock",
            "tombstones",
            "restore",
            "provenance",
            "revisions",
        ]
        svc._delete_subject_tombstones.assert_awaited_once_with("raw-sub")
        assert counts["memory_tombstones_deleted"] == 2
        restore_details.assert_awaited_once_with(
            svc.db, "raw-sub", actor=pseudonym, erased_actors=frozenset()
        )
        assert pseudonym != "raw-sub"
        assert counts["details_restored"] == 2
        assert counts["details_restored_unavailable"] == 1
        assert counts["details_embedding_requeued"] == 3
        # #2266: the payload counts come after the commit (``_execute``)
        assert not [key for key in counts if key.startswith("details_payload")]
        assert counts["memory_revisions_pseudonymized"] == 4
        assert counts["revision_details_stripped"] == 3


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

        counts = (await svc._delete_postgres(target)).counts

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

        async def _by_column(model, column, user_id):
            return expected.get((model, column.key), 0)

        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(side_effect=_by_column)
        svc._erase_secret_access_log = AsyncMock(return_value=0)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = (await svc._delete_postgres(target)).counts

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
            counts = (await svc._delete_postgres(target)).counts

        revoke.assert_awaited_once_with(svc.db, target.user_id, delete_tokens=True)
        assert counts["oauth_authorization_codes"] == 1
        assert counts["oauth_device_codes"] == 2
        assert counts["oauth_tokens"] == 3
        deleted_models = {c.args[0] for c in svc._count_and_delete.await_args_list}
        assert ContextMember in deleted_models
        assert LLMCallLog in deleted_models
        # #2129: the per-grant settings rows carry the raw sub too.
        from models.auth import OAuthGrantSetting

        assert OAuthGrantSetting in deleted_models

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
        # #2286: a completed run never keeps a marker the sweep may have set
        assert "failure_reason" in params and params["failure_reason"] is None

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

    @pytest.mark.parametrize("rowcount", [1, 0])
    @pytest.mark.asyncio
    async def test_finalize_is_not_guarded_by_the_runs_claim(self, rowcount):
        """#2302: the completion follows the Postgres commit, which outranks a
        newer claim of the row: the UPDATE matches the id alone (no
        ``started_at`` / ``status``). A 0-row match (the row was deleted) is
        logged with the request id only and does not raise."""
        svc = _service()
        captured: dict = {}

        async def _exec(stmt):
            captured["stmt"] = stmt
            return SimpleNamespace(rowcount=rowcount)

        svc.db.execute = AsyncMock(side_effect=_exec)
        svc.db.commit = AsyncMock()
        request = SimpleNamespace(id=uuid4(), user_id="erased-sub", initiated_by="erased-sub")

        with patch("services.account_erasure_service.logger") as log:
            await svc._finalize(request, {"users": 1})

        where = str(captured["stmt"].whereclause)
        assert "started_at" not in where and "status" not in where
        assert "erasure_requests.id" in where
        names = [c.args[0] for c in log.warning.call_args_list]
        if rowcount:
            assert "erasure_finalize_request_missing" not in names
        else:
            (call,) = [
                c
                for c in log.warning.call_args_list
                if c.args[0] == "erasure_finalize_request_missing"
            ]
            assert call.kwargs == {"request_id": str(request.id)}
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("self_service", [True, False])
    @pytest.mark.parametrize("initiated_by", ["u-1", "admin-1"])
    async def test_write_audit_log_follows_the_request_row_rule(self, initiated_by, self_service):
        """#2345: the ``account_erasure`` row's ``initiated_by`` is the
        pseudonym iff it is the subject's sub — whatever ``is_self_service``
        says — and the row carries no IP / user agent even when the request
        had both."""
        from models.auth import AuditLog
        from services.erasure_details import erasure_pseudonym

        svc = _service()
        target = _user()
        request = SimpleNamespace(
            id=uuid4(),
            user_id=target.user_id,
            initiated_by=initiated_by,
            is_self_service=self_service,
            reason_code=None,
        )
        request.ip_address, request.user_agent = "203.0.113.9", "agent"

        await svc._write_audit_log(request, target, {"users": 1})

        row = svc.db.add.call_args.args[0]
        assert isinstance(row, AuditLog)
        pseudonym = erasure_pseudonym(target.user_id)
        assert row.user_id == pseudonym
        expected = pseudonym if initiated_by == target.user_id else initiated_by
        assert row.user_metadata["initiated_by"] == expected
        assert row.user_metadata["is_self_service"] is self_service
        assert row.ip_address is None and row.user_agent is None
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_calls_secret_access_log_erasure(self):
        svc = _service()
        svc._count_and_delete = AsyncMock(return_value=0)
        svc._pseudonymize_field = AsyncMock(return_value=0)
        svc._erase_secret_access_log = AsyncMock(return_value=5)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()
        target = _user()

        counts = (await svc._delete_postgres(target)).counts

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
    from services.account_erasure_service import PostgresErasure
    from services.erasure_details import RestoreReport

    svc._delete_postgres = AsyncMock(
        return_value=PostgresErasure({}, RestoreReport(), datetime(2026, 10, 1, 12, 0))
    )
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
    async def test_confirm_refusal_survives_a_redis_failure(self, owned_contracts):
        """The token delete is best-effort: the client still gets BILLING-005."""
        from utils.exceptions import BillingContractActiveError

        svc = _service()
        target = _user(auth_method="oauth")
        svc._load_user_or_404 = AsyncMock(return_value=target)
        svc._check_no_blocking_workspace_transfers = AsyncMock()
        owned_contracts.return_value = [_contract_workspace()]
        token = "raw-token-abc"
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash=_sha256_hex(target.email),
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            redis_client = MagicMock()
            redis_client.get = AsyncMock(return_value=str(request.id))
            redis_client.delete = AsyncMock(side_effect=RuntimeError("redis down"))
            mock_redis.return_value = redis_client

            with pytest.raises(BillingContractActiveError):
                await svc.confirm_self_service(user_id="u-1", token=token)

        assert request.status == STATUS_CANCELLED

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


class TestFailureAfterThePostgresCommit:
    """#2268: a failure after ``_delete_postgres`` committed cannot be
    retried (the user row is gone) and the subject is erased in Postgres: the
    request row is marked with ``FAILURE_AFTER_POSTGRES_COMMIT`` and stores
    the pseudonym like a ``complete`` one."""

    @staticmethod
    def _failed_values(svc: AccountErasureService) -> dict:
        return svc.db.execute.await_args.args[0].compile().params

    # #2287: the audit-log pseudonymize is part of the Postgres step, not a
    # step after its commit (``test_the_audit_log_pseudonymization_*`` below)
    @pytest.mark.parametrize("failing", ["_write_audit_log", "_finalize"])
    @pytest.mark.parametrize("self_service", [True, False])
    @pytest.mark.asyncio
    async def test_the_row_is_pseudonymized_and_marked(self, failing, self_service):
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        svc = _service()
        _stub_execute_steps(svc)
        setattr(svc, failing, AsyncMock(side_effect=RuntimeError("boom")))
        request = _execute_request(self_service=self_service)
        request.ip_address, request.user_agent = "203.0.113.9", "agent"

        with (
            patch("services.account_erasure_service.delete_user_points", new=AsyncMock()),
            patch("services.account_erasure_service._audit_salt", return_value="salt"),
        ):
            with pytest.raises(RuntimeError):
                await svc._execute(request, _user())
            pseudonym = svc._pseudonym("u-1")

        params = self._failed_values(svc)
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"].startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        # the step and the class only: an exception text can name the account
        assert params["failure_reason"].endswith(": RuntimeError")
        assert "boom" not in params["failure_reason"]
        assert params["user_id"] == pseudonym != "u-1"
        assert (params["ip_address"], params["user_agent"]) == (None, None)
        if self_service:
            assert params["initiated_by"] == pseudonym
        else:
            assert "initiated_by" not in params  # the admin's id stays
        # the summary keeps the Postgres counts and the scrub time
        summary = params["deleted_data_summary"]
        assert summary["postgres"]["memory_scrub_at"] == "2026-10-01T12:00:00Z"

    @pytest.mark.asyncio
    async def test_a_failure_before_the_commit_keeps_the_row_as_it_was(self):
        """Nothing was erased in Postgres: the user can retry, so the row keeps
        its ids and gets no marker."""
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT

        svc = _service()
        _stub_execute_steps(svc)
        svc._delete_postgres = AsyncMock(side_effect=RuntimeError("deadlock"))

        with patch("services.account_erasure_service.delete_user_points", new=AsyncMock()):
            with pytest.raises(RuntimeError):
                await svc._execute(_execute_request(self_service=True), _user())

        params = self._failed_values(svc)
        assert params["failure_reason"] == "deadlock"
        assert not params["failure_reason"].startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        assert "user_id" not in params and "initiated_by" not in params


def _delete_postgres_service() -> AccountErasureService:
    """``_delete_postgres`` with its sweeps stubbed and ``db.execute`` mocked."""
    svc = _service()
    svc._count_and_delete = AsyncMock(return_value=0)
    svc._pseudonymize_field = AsyncMock(return_value=0)
    svc.db.delete = AsyncMock()
    svc.db.commit = AsyncMock()
    return svc


class TestConnectorKeysReownedInThePostgresStep:
    """#2305: the subject's live connector write keys go to their workspace's
    owner before the per-user ``api_keys`` delete, so the connector keeps
    working."""

    @pytest.mark.asyncio
    async def test_the_update_hands_live_connector_keys_to_the_owner(self):
        from sqlalchemy.dialects import postgresql

        svc = _service()
        captured: list[Any] = []
        svc.db.execute = AsyncMock(
            side_effect=lambda stmt, *a, **k: captured.append(stmt) or MagicMock(rowcount=2)
        )

        assert await svc._reown_connector_keys("raw-sub") == 2

        (stmt,) = captured
        sql = " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())
        assert sql.startswith(
            "UPDATE api_keys SET user_id=workspaces.owner_user_id FROM workspaces WHERE "
        )
        # the id-ordered lock over the subject's live connector rows
        assert (
            "api_keys.id IN (SELECT api_keys_1.id FROM api_keys AS api_keys_1 "
            "WHERE api_keys_1.user_id = %(user_id_1)s::VARCHAR "
            "AND api_keys_1.connector_id IS NOT NULL AND api_keys_1.revoked_at IS NULL "
            "ORDER BY api_keys_1.id FOR UPDATE)"
        ) in sql
        assert "api_keys.user_id = %(user_id_2)s::VARCHAR" in sql
        assert "api_keys.connector_id IS NOT NULL" in sql
        assert "api_keys.revoked_at IS NULL" in sql
        assert "api_keys.workspace_id = workspaces.id" in sql
        assert "workspaces.owner_user_id != %(owner_user_id_1)s::VARCHAR" in sql
        assert stmt.compile().params == {
            "user_id_1": "raw-sub",
            "user_id_2": "raw-sub",
            "owner_user_id_1": "raw-sub",
        }

    @pytest.mark.asyncio
    async def test_it_runs_before_the_api_keys_delete_and_is_counted(self):
        from models.auth import APIKey

        svc = _delete_postgres_service()
        order: list[Any] = []

        async def _reown(user_id: str) -> int:
            order.append(("reown", user_id))
            return 4

        async def _delete(model: Any, where_clause: Any) -> int:
            order.append(model)
            return 1

        svc._reown_connector_keys = AsyncMock(side_effect=_reown)
        svc._count_and_delete = AsyncMock(side_effect=_delete)

        counts = (await svc._delete_postgres(_user())).counts

        assert order.index(("reown", "u-1")) < order.index(APIKey)
        assert order.count(("reown", "u-1")) == 1
        assert counts["connector_api_keys_reowned"] == 4
        assert counts["api_keys"] == 1


class TestAuditLogPseudonymizationInThePostgresStep:
    """#2287: the subject's audit rows are pseudonymized inside
    ``_delete_postgres``'s transaction — atomic with the erasure, so no
    failure after the commit can leave the raw sub and email on them."""

    @pytest.mark.asyncio
    async def test_the_update_runs_before_the_commit_by_the_raw_sub(self):
        from sqlalchemy import Select
        from sqlalchemy.sql.dml import Update

        svc = _delete_postgres_service()
        order: list[Any] = []
        svc.db.execute = AsyncMock(
            side_effect=lambda stmt, *a, **k: order.append(stmt) or MagicMock(rowcount=3)
        )
        svc.db.delete = AsyncMock(side_effect=lambda *a: order.append("delete"))
        svc.db.commit = AsyncMock(side_effect=lambda: order.append("commit"))

        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            counts = (await svc._delete_postgres(_user())).counts
            pseudonym = svc._pseudonym("u-1")

        audit = [
            i
            for i, stmt in enumerate(order)
            if isinstance(stmt, Update) and str(stmt.table) == "audit_logs"
        ]
        # #2298: the own rows, then the mentions, after one lock over both
        assert len(audit) == 2 and audit[1] == audit[0] + 1
        lock = order[audit[0] - 1]
        assert isinstance(lock, Select)
        assert "audit_logs" in str(lock) and str(lock).endswith("FOR UPDATE")
        # the mentions are the last statement before the user delete, and
        # everything is before the one commit
        assert order[audit[1] + 1] == "delete"
        assert order.index("commit") > audit[1] and order.count("commit") == 1
        params = order[audit[0]].compile().params
        # WHERE by the raw sub, never by the pseudonym (that would hash the hash)
        assert {v for k, v in params.items() if k.startswith("user_id_")} == {"u-1"}
        assert params["user_id"] == pseudonym
        mention = str(order[audit[1]].compile())
        assert "audit_logs.user_email IN" in mention and "audit_logs.resource IN" in mention
        assert counts["audit_logs_pseudonymized"] == 3
        assert counts["audit_mentions_pseudonymized"] == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failing", ["own", "mentions"])
    async def test_a_failing_audit_statement_rolls_the_step_back(self, failing):
        """#2298: a failure in either audit UPDATE goes through the handler
        that keeps the request's ids (nothing was committed)."""
        from sqlalchemy.sql.dml import Update

        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT

        svc = _delete_postgres_service()
        svc._handle_owned_workspaces = AsyncMock(return_value={})
        seen: list[str] = []

        async def _exec(stmt, *a, **k):
            if isinstance(stmt, Update) and str(stmt.table) == "audit_logs":
                kind = "mentions" if "audit_logs.resource IN" in str(stmt) else "own"
                seen.append(kind)
                if kind == failing:
                    raise RuntimeError("audit down")
            return MagicMock(rowcount=1)

        svc.db.execute = AsyncMock(side_effect=_exec)
        with (
            patch("services.account_erasure_service.delete_user_points", new=AsyncMock()),
            patch("services.account_erasure_service.erased_pseudonyms", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError):
                await svc._execute(_execute_request(self_service=True), _user())

        assert seen[-1] == failing
        svc.db.delete.assert_not_awaited()
        svc.db.commit.assert_awaited_once()  # the failure row only
        params = svc.db.execute.await_args.args[0].compile().params
        assert params["failure_reason"] == "audit down"
        assert not params["failure_reason"].startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        assert "user_id" not in params and "initiated_by" not in params

    @pytest.mark.asyncio
    async def test_a_failure_rolls_the_postgres_step_back(self):
        """Raised inside the Postgres step: nothing was committed, so the
        request keeps its ids and gets no marker — the user can retry."""
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT

        svc = _delete_postgres_service()
        svc._handle_owned_workspaces = AsyncMock(return_value={})
        svc._pseudonymize_audit_logs = AsyncMock(side_effect=RuntimeError("audit down"))

        with (
            patch("services.account_erasure_service.delete_user_points", new=AsyncMock()),
            patch("services.account_erasure_service.erased_pseudonyms", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError):
                await svc._execute(_execute_request(self_service=True), _user())

        svc.db.delete.assert_not_awaited()
        svc.db.commit.assert_awaited_once()  # the failure row only
        params = svc.db.execute.await_args.args[0].compile().params
        assert params["failure_reason"] == "audit down"
        assert not params["failure_reason"].startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        assert "user_id" not in params and "initiated_by" not in params


@pytest.mark.asyncio
async def test_a_failure_after_the_commit_logs_no_account_id_or_exception_text():
    """#2268 review: after the Postgres commit the logs carry the request id,
    the step and the exception class — never the account id or the text."""
    svc = _service()
    _stub_execute_steps(svc)
    svc._finalize = AsyncMock(side_effect=RuntimeError("Key (user_id)=(u-1) violates"))

    with (
        patch("services.account_erasure_service.delete_user_points", new=AsyncMock()),
        patch("services.account_erasure_service.logger") as log,
    ):
        with pytest.raises(RuntimeError):
            await svc._execute(_execute_request(self_service=True), _user())

    calls = [*log.error.call_args_list, *log.warning.call_args_list, *log.info.call_args_list]
    assert any(c.args and c.args[0] == "erasure_failed_after_postgres_commit" for c in calls)
    assert not any(c.args and c.args[0] == "erasure_execute_failed" for c in calls)
    for call in calls:
        assert "u-1" not in repr(call), call
        assert call.kwargs.get("exc_info") is None


@pytest.mark.asyncio
async def test_the_sweep_logs_no_account_id_for_a_failed_request():
    """#2268 review: the sweep's own line after ``_execute`` raised names the
    request and the exception class only."""
    svc = _service()
    request = _execute_request(self_service=True)
    claimed = MagicMock()
    claimed.scalars.return_value.all.return_value = [request]
    fresh = MagicMock()
    fresh.scalar_one_or_none.return_value = request
    svc.db.execute = AsyncMock(side_effect=[MagicMock(rowcount=0), claimed, fresh])
    svc._load_user_or_none = AsyncMock(return_value=_user())
    svc._execute = AsyncMock(side_effect=RuntimeError("Key (user_id)=(u-1) violates"))

    with patch("services.account_erasure_service.logger") as log:
        assert await svc.sweep_pending_erasures() == 0

    (call,) = [c for c in log.error.call_args_list if c.args[0] == "erasure_sweep_execute_failed"]
    assert call.kwargs == {"request_id": str(request.id), "error_class": "RuntimeError"}


# ---------------------------------------------------------------------------
# #2286: a run interrupted after the Postgres commit
# ---------------------------------------------------------------------------

_SCRUB_AT = datetime(2026, 10, 1, 12, 0)


def _sweep_with_absent_user(
    request: ErasureRequest,
) -> tuple[AccountErasureService, list[Any]]:
    """The sweep claims ``request``; its user row is gone."""
    svc = _service()
    claimed = MagicMock()
    claimed.scalars.return_value.all.return_value = [request]
    fresh = MagicMock()
    fresh.scalar_one_or_none.return_value = request
    results = iter([MagicMock(rowcount=0), claimed, fresh])
    statements: list[Any] = []

    def execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(stmt)
        return next(results, MagicMock(rowcount=1))

    svc.db.execute = AsyncMock(side_effect=execute)
    svc._load_user_or_none = AsyncMock(return_value=None)
    svc._execute = AsyncMock()
    svc._clear_redis = AsyncMock(return_value={"sessions": 1, "co_act": 0})
    # #2302: the payload re-drive after an interrupted close (tested below)
    svc._resync_interrupted_payloads = AsyncMock()
    return svc, statements


def _close_values(statements: list[Any]) -> dict[str, Any]:
    """The params of the one UPDATE that closed the request."""
    from sqlalchemy.sql.dml import Update

    (closing,) = [s for s in statements[3:] if isinstance(s, Update)]
    assert str(closing.table) == "erasure_requests"
    return closing.compile().params


class TestSweepClosesARequestWithoutAUserRow:
    """#2286: a claimed request whose user row is gone is never re-run. The
    ``postgres`` summary (written in the Postgres step's transaction) tells
    a run interrupted after that commit from one that never started; both
    end pseudonymized."""

    @pytest.mark.parametrize("self_service", [True, False])
    @pytest.mark.asyncio
    async def test_an_interrupted_run_is_failed_marked_and_pseudonymized(self, self_service):
        from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        request = _execute_request(self_service=self_service)
        request.ip_address, request.user_agent = "203.0.113.9", "agent"
        postgres = {"users": 1, "memory_scrub_at": "2026-10-01T12:00:00Z"}
        request.deleted_data_summary = {"postgres": postgres}
        svc, statements = _sweep_with_absent_user(request)

        with (
            patch("services.account_erasure_service._audit_salt", return_value="salt"),
            patch("services.account_erasure_service.logger") as log,
        ):
            assert await svc.sweep_pending_erasures() == 0
            pseudonym = svc._pseudonym("u-1")

        svc._execute.assert_not_awaited()
        svc._clear_redis.assert_awaited_once_with("u-1")
        params = _close_values(statements)
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"] == INTERRUPTED_AFTER_POSTGRES_COMMIT
        assert params["user_id"] == pseudonym != "u-1"
        assert (params["ip_address"], params["user_agent"]) == (None, None)
        if self_service:
            assert params["initiated_by"] == pseudonym
        else:
            assert "initiated_by" not in params  # the admin's id stays
        assert "completed_at" not in params
        # the Postgres counts and the scrub time stay; the Redis counts join
        assert params["deleted_data_summary"] == {
            "postgres": postgres,
            "redis": {"sessions": 1, "co_act": 0},
        }
        # guarded: a row moved since the claim is left alone
        assert params["status_1"] == "in_progress"
        assert params["id_1"] == request.id
        (call,) = [
            c for c in log.warning.call_args_list if c.args[0] == "erasure_sweep_user_row_absent"
        ]
        assert call.kwargs == {"request_id": str(request.id), "ran_in_postgres": True}
        for c in [*log.warning.call_args_list, *log.error.call_args_list]:
            assert "u-1" not in repr(c), c

    @pytest.mark.parametrize("summary", [None, {"qdrant": {"kagura_memories": 0}}])
    @pytest.mark.asyncio
    async def test_a_request_that_never_ran_is_complete_noted_and_pseudonymized(self, summary):
        from models.erasure import STATUS_COMPLETE

        request = _execute_request(self_service=True)
        request.ip_address, request.user_agent = "203.0.113.9", "agent"
        request.deleted_data_summary = summary
        svc, statements = _sweep_with_absent_user(request)

        with (
            patch("services.account_erasure_service._audit_salt", return_value="salt"),
            patch("services.account_erasure_service.logger") as log,
        ):
            await svc.sweep_pending_erasures()
            pseudonym = svc._pseudonym("u-1")

        svc._clear_redis.assert_not_awaited()
        params = _close_values(statements)
        assert params["status"] == STATUS_COMPLETE
        assert params["completed_at"] is not None
        # flagged: ``erased_pseudonyms`` takes the stored pseudonym as is
        assert params["deleted_data_summary"] == {
            "note": "user_row_already_absent",
            "pseudonymized": True,
        }
        assert "failure_reason" not in params
        assert params["user_id"] == params["initiated_by"] == pseudonym
        assert (params["ip_address"], params["user_agent"]) == (None, None)
        (call,) = [
            c for c in log.warning.call_args_list if c.args[0] == "erasure_sweep_user_row_absent"
        ]
        assert call.kwargs == {"request_id": str(request.id), "ran_in_postgres": False}

    @pytest.mark.asyncio
    async def test_a_redis_failure_does_not_stop_the_close(self):
        from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT

        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"postgres": {"users": 1}}
        svc, statements = _sweep_with_absent_user(request)
        svc._clear_redis = AsyncMock(side_effect=RuntimeError("redis down for u-1"))

        with patch("services.account_erasure_service.logger") as log:
            await svc.sweep_pending_erasures()

        params = _close_values(statements)
        assert params["failure_reason"] == INTERRUPTED_AFTER_POSTGRES_COMMIT
        assert params["deleted_data_summary"] == {"postgres": {"users": 1}}
        svc.db.commit.assert_awaited()
        (call,) = [
            c
            for c in log.warning.call_args_list
            if c.args[0] == "erasure_sweep_redis_cleanup_failed"
        ]
        assert call.kwargs == {"request_id": str(request.id), "error_class": "RuntimeError"}
        for c in [*log.warning.call_args_list, *log.error.call_args_list]:
            assert "u-1" not in repr(c), c

    @pytest.mark.asyncio
    async def test_a_row_moved_since_the_claim_is_logged_as_moved(self):
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"postgres": {"users": 1}}
        svc, statements = _sweep_with_absent_user(request)
        executed = svc.db.execute.side_effect

        def moved(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            result = executed(stmt, *args, **kwargs)
            if len(statements) > 3:
                result.rowcount = 0  # the guarded UPDATE matched nothing
            return result

        svc.db.execute = AsyncMock(side_effect=moved)

        with patch("services.account_erasure_service.logger") as log:
            await svc.sweep_pending_erasures()

        names = [c.args[0] for c in [*log.warning.call_args_list, *log.info.call_args_list]]
        assert "erasure_sweep_request_moved" in names
        assert "erasure_sweep_user_row_absent" not in names

    @pytest.mark.parametrize("ran", [False, True])
    @pytest.mark.asyncio
    async def test_the_never_ran_close_requires_no_postgres_in_sql(self, ran):
        """Review of #2302: the sweep read the row without ``postgres``; a run
        alive past the cutoff may commit its Postgres step before the close.
        The ``complete`` close is then guarded by ``NOT (summary ? 'postgres')``
        (0 rows: moved; the next sweep closes it as interrupted); the
        interrupted close needs no such guard."""
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"postgres": {"users": 1}} if ran else None
        svc, statements = _sweep_with_absent_user(request)

        await svc.sweep_pending_erasures()

        from sqlalchemy.sql.dml import Update

        (close,) = [s for s in statements[3:] if isinstance(s, Update)]
        params = close.compile().params
        assert ("postgres" in params.values()) is (not ran), params
        assert ("deleted_data_summary ?" in str(close.whereclause)) is (not ran)

    @pytest.mark.asyncio
    async def test_a_failing_close_does_not_stop_the_batch(self):
        gone, live = _execute_request(self_service=True), _execute_request(self_service=True)
        live.user_id = live.initiated_by = "u-2"
        svc = _service()
        claimed = MagicMock()
        claimed.scalars.return_value.all.return_value = [gone, live]
        fresh = [MagicMock(), MagicMock()]
        fresh[0].scalar_one_or_none.return_value = gone
        fresh[1].scalar_one_or_none.return_value = live
        svc.db.execute = AsyncMock(side_effect=[MagicMock(rowcount=0), claimed, *fresh])
        svc._load_user_or_none = AsyncMock(side_effect=[None, _user(user_id="u-2")])
        svc._close_request_without_user = AsyncMock(side_effect=RuntimeError("u-1 lost"))
        svc._execute = AsyncMock()

        with patch("services.account_erasure_service.logger") as log:
            assert await svc.sweep_pending_erasures() == 1

        svc._execute.assert_awaited_once()
        svc.db.rollback.assert_awaited()
        (call,) = [c for c in log.error.call_args_list if c.args[0] == "erasure_sweep_close_failed"]
        assert call.kwargs == {"request_id": str(gone.id), "error_class": "RuntimeError"}


class TestTheSweepReDrivesAnInterruptedRunsPayloads:
    """#2302: right after closing a row as interrupted, the sweep patches the
    vector payloads of the memories the run's restore put back (read from
    the restore's revision rows) and records the counts on the row."""

    @pytest.mark.asyncio
    async def test_the_sweep_re_drives_only_an_interrupted_close(self):
        request = _execute_request(self_service=True)
        postgres = {"users": 1, "memory_scrub_at": "2026-10-01T12:00:00Z"}
        request.deleted_data_summary = {"postgres": postgres}
        svc, _ = _sweep_with_absent_user(request)

        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            await svc.sweep_pending_erasures()
            pseudonym = svc._pseudonym("u-1")

        svc._resync_interrupted_payloads.assert_awaited_once_with(
            request.id, pseudonym, {"postgres": postgres}
        )

    @pytest.mark.parametrize("summary", [None, {"qdrant": {"kagura_memories": 0}}])
    @pytest.mark.asyncio
    async def test_a_never_ran_note_row_is_not_re_driven(self, summary):
        request = _execute_request(self_service=True)
        request.deleted_data_summary = summary
        svc, statements = _sweep_with_absent_user(request)

        await svc.sweep_pending_erasures()

        svc._resync_interrupted_payloads.assert_not_awaited()
        _close_values(statements)  # the close is the only UPDATE

    @pytest.mark.asyncio
    async def test_a_moved_row_is_not_re_driven(self):
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"postgres": {"users": 1}}
        svc, statements = _sweep_with_absent_user(request)
        executed = svc.db.execute.side_effect

        def moved(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            result = executed(stmt, *args, **kwargs)
            if len(statements) > 3:
                result.rowcount = 0
            return result

        svc.db.execute = AsyncMock(side_effect=moved)
        await svc.sweep_pending_erasures()

        svc._resync_interrupted_payloads.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_re_drive_is_logged_and_the_batch_goes_on(self):
        gone, live = _execute_request(self_service=True), _execute_request(self_service=True)
        gone.deleted_data_summary = {"postgres": {"users": 1}}
        live.user_id = live.initiated_by = "u-2"
        svc = _service()
        claimed = MagicMock()
        claimed.scalars.return_value.all.return_value = [gone, live]
        fresh = [MagicMock(), MagicMock()]
        fresh[0].scalar_one_or_none.return_value = gone
        fresh[1].scalar_one_or_none.return_value = live
        svc.db.execute = AsyncMock(side_effect=[MagicMock(rowcount=0), claimed, *fresh])
        svc._load_user_or_none = AsyncMock(side_effect=[None, _user(user_id="u-2")])
        svc._close_request_without_user = AsyncMock(return_value="interrupted")
        svc._resync_interrupted_payloads = AsyncMock(side_effect=RuntimeError("u-1 down"))
        svc._execute = AsyncMock()

        with patch("services.account_erasure_service.logger") as log:
            assert await svc.sweep_pending_erasures() == 1

        svc._execute.assert_awaited_once()
        svc.db.rollback.assert_awaited()
        # its own event: the close committed, the row is terminal
        names = [c.args[0] for c in log.error.call_args_list]
        assert "erasure_sweep_close_failed" not in names
        (call,) = [
            c
            for c in log.error.call_args_list
            if c.args[0] == "erasure_sweep_payload_resync_failed"
        ]
        assert call.kwargs == {"request_id": str(gone.id), "error_class": "RuntimeError"}

    @staticmethod
    def _resync_service(rowcount: int) -> tuple[AccountErasureService, list[Any]]:
        svc = _service()
        statements: list[Any] = []

        def execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(stmt)
            return MagicMock(rowcount=rowcount)

        svc.db.execute = AsyncMock(side_effect=execute)
        return svc, statements

    @pytest.mark.parametrize("rowcount", [1, 0])
    @pytest.mark.asyncio
    async def test_the_restored_set_is_patched_and_the_counts_recorded(self, rowcount):
        from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT, STATUS_FAILED
        from services.erasure_details import PayloadSync

        request_id = uuid4()
        memory_id = uuid4()
        postgres = {"users": 1, "memory_scrub_at": "2026-10-01T12:00:00Z"}
        svc, statements = self._resync_service(rowcount)
        syncs = [PayloadSync(memory_id, 3)]

        async def sync(db: Any, given: Any, report: Any) -> None:
            assert given == syncs
            report.payload_synced = 1

        with (
            patch(
                "services.account_erasure_service.restored_payload_syncs",
                new=AsyncMock(return_value=syncs),
            ) as derive,
            patch(
                "services.account_erasure_service.sync_restored_payloads",
                new=AsyncMock(side_effect=sync),
            ) as patched,
            patch("services.account_erasure_service.logger") as log,
        ):
            await svc._resync_interrupted_payloads(request_id, "p" * 64, {"postgres": postgres})

        derive.assert_awaited_once_with(svc.db, "p" * 64, since=datetime(2026, 10, 1, 12, 0))
        patched.assert_awaited_once()
        (update_stmt,) = statements
        params = update_stmt.compile().params
        assert params["id_1"] == request_id
        assert params["status_1"] == STATUS_FAILED
        # any ``after_postgres_commit:`` marker (review of #2302)
        assert "erasure_requests.failure_reason LIKE" in str(update_stmt.whereclause)
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT

        marker = next(v for k, v in params.items() if k.startswith("failure_reason"))
        assert marker == FAILURE_AFTER_POSTGRES_COMMIT.replace("_", "/_")  # autoescaped
        assert INTERRUPTED_AFTER_POSTGRES_COMMIT.startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        (keys,) = [v for v in params.values() if isinstance(v, dict) and "postgres" in v]
        assert keys["postgres"]["users"] == 1
        assert keys["postgres"]["memory_scrub_at"] == "2026-10-01T12:00:00Z"
        assert keys["postgres"]["details_payload_synced"] == 1
        assert keys["postgres"]["details_payload_requeue_failed"] == 0
        assert len([k for k in keys["postgres"] if k.startswith("details_payload_")]) == 7
        svc.db.commit.assert_awaited()
        calls = [*log.info.call_args_list, *log.warning.call_args_list]
        names = [c.args[0] for c in calls]
        if rowcount:
            (call,) = [c for c in calls if c.args[0] == "erasure_sweep_payloads_resynced"]
            assert call.kwargs["request_id"] == str(request_id)
            assert call.kwargs["memories"] == 1
            assert call.kwargs["details_payload_synced"] == 1
            assert "erasure_sweep_request_moved" not in names
        else:
            assert "erasure_sweep_request_moved" in names
            assert "erasure_sweep_payloads_resynced" not in names
        for c in calls:
            assert "p" * 64 not in repr(c)

    @pytest.mark.parametrize("scrub", [None, "not a time"])
    @pytest.mark.asyncio
    async def test_no_usable_scrub_time_reads_without_a_bound(self, scrub):
        """Review of #2302: no bound reads every restore of the pseudonym
        (never patches nothing and records it as done)."""
        svc, statements = self._resync_service(1)
        postgres: dict[str, Any] = {"users": 1}
        if scrub is not None:
            postgres["memory_scrub_at"] = scrub
        with (
            patch(
                "services.account_erasure_service.restored_payload_syncs",
                new=AsyncMock(return_value=[]),
            ) as derive,
            patch("services.account_erasure_service.sync_restored_payloads", new=AsyncMock()),
        ):
            await svc._resync_interrupted_payloads(uuid4(), "p" * 64, {"postgres": postgres})

        derive.assert_awaited_once_with(svc.db, "p" * 64, since=None)
        (update_stmt,) = statements
        params = update_stmt.compile().params
        (keys,) = [v for v in params.values() if isinstance(v, dict) and "postgres" in v]
        assert keys["postgres"]["details_payload_synced"] == 0


class TestTheSweepNeverRunsAgainstAReRegisteredSub:
    """#2302: a claimed row whose Postgres step committed is closed as
    interrupted even when a user row with its sub exists — that is an
    account registered again since, and is never run against."""

    @pytest.mark.parametrize("self_service", [True, False])
    @pytest.mark.asyncio
    async def test_the_new_account_is_not_erased_and_its_redis_is_kept(self, self_service):
        from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        request = _execute_request(self_service=self_service)
        request.ip_address, request.user_agent = "203.0.113.9", "agent"
        postgres = {"users": 1, "memory_scrub_at": "2026-10-01T12:00:00Z"}
        request.deleted_data_summary = {"postgres": postgres}
        svc, statements = _sweep_with_absent_user(request)
        svc._load_user_or_none = AsyncMock(return_value=_user())

        with (
            patch("services.account_erasure_service._audit_salt", return_value="salt"),
            patch("services.account_erasure_service.logger") as log,
        ):
            assert await svc.sweep_pending_erasures() == 0
            pseudonym = svc._pseudonym("u-1")

        svc._execute.assert_not_awaited()
        svc._clear_redis.assert_not_awaited()
        params = _close_values(statements)
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"] == INTERRUPTED_AFTER_POSTGRES_COMMIT
        assert params["user_id"] == pseudonym
        if self_service:
            assert params["initiated_by"] == pseudonym
        else:
            assert "initiated_by" not in params
        assert params["deleted_data_summary"] == {"postgres": postgres}  # no redis key
        svc._resync_interrupted_payloads.assert_awaited_once()
        (call,) = [
            c
            for c in log.warning.call_args_list
            if c.args[0] == "erasure_sweep_subject_reregistered"
        ]
        assert call.kwargs == {"request_id": str(request.id)}
        names = [c.args[0] for c in log.warning.call_args_list]
        assert "erasure_sweep_user_row_absent" not in names
        for c in [*log.warning.call_args_list, *log.error.call_args_list]:
            assert "u-1" not in repr(c), c

    @pytest.mark.parametrize("summary", [None, {"retry": {"attempts": 1}}])
    @pytest.mark.asyncio
    async def test_a_stale_run_that_never_reached_postgres_runs_again(self, summary):
        request = _execute_request(self_service=True)
        request.deleted_data_summary = summary
        svc, statements = _sweep_with_absent_user(request)
        target = _user()
        svc._load_user_or_none = AsyncMock(return_value=target)

        assert await svc.sweep_pending_erasures() == 1

        svc._execute.assert_awaited_once_with(request, target)
        svc._resync_interrupted_payloads.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_close_refuses_a_live_user_on_a_row_that_never_ran(self):
        request = _execute_request(self_service=True)
        svc = _service()
        with pytest.raises(ValueError):
            await svc._close_request_without_user(request, user_row_exists=True)
        svc.db.execute.assert_not_awaited()


class TestThePostgresStepRecordsItselfOnTheRequest:
    """#2286: ``_delete_postgres`` writes the ``postgres`` summary on the
    request row inside its transaction, so a crash after the commit cannot
    lose the fact that it ran."""

    @staticmethod
    def _recording_service() -> tuple[AccountErasureService, list[Any]]:
        svc = _delete_postgres_service()
        order: list[Any] = []
        svc.db.execute = AsyncMock(
            side_effect=lambda stmt, *a, **k: order.append(stmt) or MagicMock(rowcount=2)
        )
        svc.db.delete = AsyncMock(side_effect=lambda *a: order.append("delete"))
        svc.db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
        return svc, order

    @staticmethod
    def _request_updates(order: list[Any]) -> list[int]:
        from sqlalchemy.sql.dml import Update

        return [
            i
            for i, stmt in enumerate(order)
            if isinstance(stmt, Update) and str(stmt.table) == "erasure_requests"
        ]

    @pytest.mark.asyncio
    async def test_the_summary_is_the_last_write_before_the_commit(self):
        from sqlalchemy.sql.dml import Update

        svc, order = self._recording_service()
        request_id = uuid4()

        with patch("services.account_erasure_service.utcnow", return_value=_SCRUB_AT):
            erased = await svc._delete_postgres(_user(), request_id=request_id)

        (i,) = self._request_updates(order)
        # after the audit-log statement (its count is in the summary), then
        # the user delete and the one commit
        audit = next(
            j
            for j, stmt in enumerate(order)
            if isinstance(stmt, Update) and str(stmt.table) == "audit_logs"
        )
        assert audit < i
        assert order[i + 1 :] == ["delete", "commit"]
        params = order[i].compile().params
        assert params["id_1"] == request_id  # scoped to the one request row
        # merged into the row's summary (``coalesce(nullif(summary, 'null'),
        # '{}') || {...}``), so a key written before this step survives and a
        # legacy JSON-null summary merges like SQL NULL (#2026)
        assert (
            "SET deleted_data_summary=(coalesce(nullif(erasure_requests.deleted_data_summary, "
            "CAST(:param_1 AS JSONB)), :param_2) || :param_3)"
        ) in str(order[i])
        assert params["param_1"] == "null"
        assert {} in params.values()
        (merged,) = [v for v in params.values() if isinstance(v, dict) and "postgres" in v]
        postgres = merged["postgres"]
        assert postgres == {**erased.counts, "memory_scrub_at": "2026-10-01T12:00:00Z"}
        assert postgres["users"] == 1 and postgres["audit_logs_pseudonymized"] == 2

    @pytest.mark.asyncio
    async def test_without_a_request_no_request_row_is_written(self):
        svc, order = self._recording_service()

        await svc._delete_postgres(_user())

        assert self._request_updates(order) == []
        assert order[-2:] == ["delete", "commit"]

    @pytest.mark.asyncio
    async def test_execute_passes_its_request(self):
        svc = _service()
        _stub_execute_steps(svc)
        request = _execute_request(self_service=True)

        with patch("services.account_erasure_service.delete_user_points", new=AsyncMock()):
            await svc._execute(request, _user())

        assert svc._delete_postgres.await_args.kwargs == {"request_id": request.id}


@pytest.mark.parametrize("initiated_by", ["u-1", "admin-1"])
def test_pseudonymized_request_values(initiated_by):
    """#2286, #2288: the one values dict every writer that pseudonymizes a
    request row uses — the pseudonym is ``erasure_pseudonym``'s, and an
    admin's ``initiated_by`` stays out of the values."""
    from services.erasure_details import erasure_pseudonym, pseudonymized_request_values

    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        values = pseudonymized_request_values("u-1", initiated_by)
        pseudonym = erasure_pseudonym("u-1")

    expected = {"user_id": pseudonym, "ip_address": None, "user_agent": None}
    if initiated_by == "u-1":
        expected["initiated_by"] = pseudonym
    assert values == expected


def test_pseudonymized_request_values_unread_row_decides_in_sql():
    """#2288: without the row's ``initiated_by`` (the re-run's repair) the
    column decides by the same rule: the subject's own sub becomes the
    pseudonym, any other value stays."""
    from sqlalchemy import update
    from sqlalchemy.dialects import postgresql

    from services.erasure_details import erasure_pseudonym, pseudonymized_request_values

    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        values = pseudonymized_request_values("u-1", None)
        pseudonym = erasure_pseudonym("u-1")

    assert {k: v for k, v in values.items() if k != "initiated_by"} == {
        "user_id": pseudonym,
        "ip_address": None,
        "user_agent": None,
    }
    compiled = (
        update(ErasureRequest)
        .values(**values)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert (
        f"initiated_by=coalesce(nullif(erasure_requests.initiated_by, 'u-1'), '{pseudonym}')"
        in str(compiled)
    )


# ---------------------------------------------------------------------------
# #2282: a vector-store failure at the first step reschedules the request
# ---------------------------------------------------------------------------


def _recording_execute(svc: AccountErasureService, *, rowcount: int = 1) -> list[Any]:
    """Record every statement and the rollbacks/commits, in order."""
    order: list[Any] = []
    svc.db.execute = AsyncMock(
        side_effect=lambda stmt, *a, **k: order.append(stmt) or MagicMock(rowcount=rowcount)
    )
    svc.db.rollback = AsyncMock(side_effect=lambda: order.append("rollback"))
    svc.db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    return order


def _request_update(order: list[Any]) -> Any:
    """The one UPDATE of ``erasure_requests`` the handler wrote."""
    from sqlalchemy.sql.dml import Update

    (stmt,) = [s for s in order if isinstance(s, Update) and str(s.table) == "erasure_requests"]
    return stmt


class TestAVectorStoreOutageReschedulesTheRequest:
    """#2282: a ``QdrantError`` from ``delete_user_points`` (step 1) leaves
    nothing erased, so the request goes back to ``cooling_off`` with a later
    ``scheduled_for`` and ``summary.retry`` — up to
    ``VECTOR_STORE_MAX_ATTEMPTS`` attempts, then ``failed``."""

    @pytest.mark.parametrize("self_service", [True, False])
    @pytest.mark.asyncio
    async def test_the_first_failure_reschedules(self, self_service):
        from models.erasure import STATUS_IN_PROGRESS
        from services.account_erasure_service import VECTOR_STORE_MAX_ATTEMPTS
        from utils.datetime import utcnow
        from utils.exceptions import ErasureRetryScheduledError, QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        order = _recording_execute(svc)
        request = _execute_request(self_service=self_service)
        request.ip_address, request.user_agent = "203.0.113.9", "agent"
        request.started_at = datetime(2026, 10, 1, 12, 0)  # this run's claim

        before = utcnow()
        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=QdrantError("connection refused")),
        ):
            with pytest.raises(ErasureRetryScheduledError) as caught:
                await svc._execute(request, _user())
        after = utcnow()

        # nothing past step 1 ran
        svc._handle_owned_workspaces.assert_not_awaited()
        svc._delete_postgres.assert_not_awaited()
        svc._clear_redis.assert_not_awaited()
        svc._write_audit_log.assert_not_awaited()
        svc._finalize.assert_not_awaited()
        svc.email_service.send_erasure_complete.assert_not_awaited()

        stmt = _request_update(order)
        # before it only reads (the owned-workspace lookup); then rolled
        # back, the one UPDATE, its commit
        from sqlalchemy.sql.selectable import Select

        assert order[-3:] == ["rollback", stmt, "commit"]
        assert all(isinstance(s, Select) for s in order[:-3])
        params = stmt.compile().params
        assert params["id_1"] == request.id
        # guarded: a row moved meanwhile (not in_progress) is left alone
        assert params["status_1"] == STATUS_IN_PROGRESS
        assert "erasure_requests.status = :status_1" in str(stmt)
        # and by this run's claim: a later sweep's re-claim is not overwritten
        assert params["started_at_1"] == datetime(2026, 10, 1, 12, 0)
        assert "erasure_requests.started_at = :started_at_1" in str(stmt)
        assert params["status"] == STATUS_COOLING_OFF
        assert before + timedelta(minutes=15) <= params["scheduled_for"]
        assert params["scheduled_for"] <= after + timedelta(minutes=15)
        retry = params["deleted_data_summary"]["retry"]
        assert params["deleted_data_summary"] == {"retry": retry}
        assert {k: v for k, v in retry.items() if k != "last_attempt_at"} == {
            "attempts": 1,
            "step": "vector_store",
            "error_class": "QdrantError",
        }
        assert retry["last_attempt_at"].endswith("Z")
        # the row's own ids are not touched (nothing was erased), and no
        # failure is recorded
        assert set(params) == {
            "status",
            "scheduled_for",
            "deleted_data_summary",
            "id_1",
            "status_1",
            "started_at_1",
        }
        assert "u-1" not in repr(params["deleted_data_summary"])

        exc = caught.value
        assert exc.status_code == 503
        assert exc.error_code == "ERASURE-007"
        assert exc.details == {
            "request_id": str(request.id),
            "attempt": 1,
            "max_attempts": VECTOR_STORE_MAX_ATTEMPTS,
            "scheduled_for": exc.details["scheduled_for"],
            "error_class": "QdrantError",
        }
        assert exc.scheduled_for == params["scheduled_for"]
        assert isinstance(exc.__cause__, QdrantError)

    @pytest.mark.parametrize("previous", [1, 2])
    @pytest.mark.asyncio
    async def test_a_later_failure_counts_on(self, previous):
        from utils.exceptions import ErasureRetryScheduledError, QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        order = _recording_execute(svc)
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"retry": {"attempts": previous, "step": "vector_store"}}

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=QdrantError("timeout")),
        ):
            with pytest.raises(ErasureRetryScheduledError) as caught:
                await svc._execute(request, _user())

        assert caught.value.attempt == previous + 1
        params = _request_update(order).compile().params
        assert params["status"] == STATUS_COOLING_OFF
        assert params["deleted_data_summary"]["retry"]["attempts"] == previous + 1

    @pytest.mark.asyncio
    async def test_the_last_attempt_fails_the_request(self):
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT, STATUS_FAILED
        from utils.exceptions import QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        order = _recording_execute(svc)
        # #2304: the re-queue after the give-up has its own tests below
        svc._requeue_after_vector_store_failure = AsyncMock()
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"retry": {"attempts": 3, "step": "vector_store"}}

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=QdrantError("connection refused")),
        ):
            with pytest.raises(QdrantError):
                await svc._execute(request, _user())

        svc._handle_owned_workspaces.assert_not_awaited()
        svc._delete_postgres.assert_not_awaited()
        params = _request_update(order).compile().params
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"] == (
            "vector_store_unavailable after 4 attempts: Qdrant service error: connection refused"
        )
        assert not params["failure_reason"].startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        # nothing was erased: the row keeps its ids, and no later run
        assert "user_id" not in params and "initiated_by" not in params
        assert "scheduled_for" not in params
        # the history is kept on the failed row
        assert params["deleted_data_summary"]["retry"]["attempts"] == 4
        assert params["deleted_data_summary"]["retry"]["error_class"] == "QdrantError"

    @pytest.mark.parametrize("error", [RuntimeError("bad config"), ValueError("bad filter")])
    @pytest.mark.asyncio
    async def test_any_other_error_at_step_one_stays_terminal(self, error):
        from models.erasure import STATUS_FAILED

        svc = _service()
        _stub_execute_steps(svc)
        order = _recording_execute(svc)

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=error),
        ):
            with pytest.raises(type(error)):
                await svc._execute(_execute_request(self_service=True), _user())

        params = _request_update(order).compile().params
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"] == str(error)
        assert params["deleted_data_summary"] is None
        assert "scheduled_for" not in params

    @pytest.mark.parametrize("failing", ["_handle_owned_workspaces", "_delete_postgres"])
    @pytest.mark.asyncio
    async def test_a_vector_store_error_at_another_step_stays_terminal(self, failing):
        """Only step 1 is retryable: a ``QdrantError`` from a later step (the
        Postgres step's details restore calls the vector store, #2244) fails
        the request as before."""
        from models.erasure import STATUS_FAILED
        from utils.exceptions import QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        setattr(svc, failing, AsyncMock(side_effect=QdrantError("down")))
        order = _recording_execute(svc)

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={"kagura_memories": 0}),
        ):
            with pytest.raises(QdrantError):
                await svc._execute(_execute_request(self_service=True), _user())

        params = _request_update(order).compile().params
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"] == "Qdrant service error: down"
        assert "scheduled_for" not in params
        assert "user_id" not in params

    @pytest.mark.asyncio
    async def test_a_failure_before_the_commit_keeps_the_retry_history(self):
        """The failure handlers write ``summary``, which carries the earlier
        attempts' block from the start."""
        from models.erasure import STATUS_FAILED

        svc = _service()
        _stub_execute_steps(svc)
        svc._delete_postgres = AsyncMock(side_effect=RuntimeError("deadlock"))
        order = _recording_execute(svc)
        request = _execute_request(self_service=True)
        previous = {"attempts": 2, "step": "vector_store", "error_class": "QdrantError"}
        request.deleted_data_summary = {"retry": previous}

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={"kagura_memories": 0}),
        ):
            with pytest.raises(RuntimeError):
                await svc._execute(request, _user())

        params = _request_update(order).compile().params
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"] == "deadlock"
        summary = params["deleted_data_summary"]
        assert summary["retry"] == previous
        assert "postgres" not in summary

    @pytest.mark.asyncio
    async def test_the_failure_handler_is_guarded_by_the_runs_claim(self):
        """#2306: like the reschedule, the generic handler's UPDATE matches
        only the row this run claimed (its ``started_at``)."""
        from models.erasure import STATUS_FAILED

        svc = _service()
        _stub_execute_steps(svc)
        svc._delete_postgres = AsyncMock(side_effect=RuntimeError("deadlock"))
        order = _recording_execute(svc)
        request = _execute_request(self_service=True)
        request.started_at = datetime(2026, 10, 1, 12, 0)  # this run's claim

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={"kagura_memories": 0}),
        ):
            with pytest.raises(RuntimeError, match="deadlock"):
                await svc._execute(request, _user())

        stmt = _request_update(order)
        assert order[-2:] == [stmt, "commit"]
        params = stmt.compile().params
        assert params["status"] == STATUS_FAILED
        assert params["id_1"] == request.id
        assert params["started_at_1"] == datetime(2026, 10, 1, 12, 0)
        assert "erasure_requests.started_at = :started_at_1" in str(stmt)

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("deadlock"),
            "billing",
            "transfer",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_stale_runs_failure_handler_only_logs(self, error):
        """#2306: the row was claimed again by a later sweep; the handler's
        UPDATE (generic, billing-contract or workspace-transfer) matches
        nothing, it logs ``erasure_failure_handler_stale_run`` and the run's
        own error still reaches the caller."""
        from utils.exceptions import BillingContractActiveError, WorkspaceTransferRequiredError

        if error == "billing":
            error = BillingContractActiveError(["ws-1"])
        elif error == "transfer":
            error = WorkspaceTransferRequiredError("ws-1", 2)
        svc = _service()
        _stub_execute_steps(svc)
        svc._handle_owned_workspaces = AsyncMock(side_effect=error)
        order = _recording_execute(svc, rowcount=0)
        request = _execute_request(self_service=True)
        request.started_at = datetime(2026, 10, 1, 12, 0)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(return_value={"kagura_memories": 0}),
            ),
            patch("services.account_erasure_service.sync_restored_payloads", new=AsyncMock()),
            patch("services.account_erasure_service.logger") as log,
        ):
            with pytest.raises(type(error)):
                await svc._execute(request, _user())

        stmt = _request_update(order)  # the one guarded UPDATE, nothing after it
        assert order[-2:] == [stmt, "commit"]
        assert stmt.compile().params["started_at_1"] == datetime(2026, 10, 1, 12, 0)
        # #2302 review: and still running — an older run's ``_finalize``
        # (by id alone) may have completed the row meanwhile
        assert "erasure_requests.status = :status_1" in str(stmt.whereclause)
        assert stmt.compile().params["status_1"] == "in_progress"
        assert "erasure_requests.started_at = :started_at_1" in str(stmt)
        stale = [
            c
            for c in log.warning.call_args_list
            if c.args[0] == "erasure_failure_handler_stale_run"
        ]
        assert len(stale) == 1
        assert stale[0].kwargs == {"request_id": str(request.id)}

    @pytest.mark.asyncio
    async def test_a_failure_after_the_postgres_commit_is_written_whatever_the_claim(self):
        """#2306: once this run erased the subject in Postgres, its handler
        writes by id alone — the row must be pseudonymized and carry the
        marker even if a later sweep claimed it again meanwhile."""
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        svc = _service()
        _stub_execute_steps(svc)
        svc._clear_redis = AsyncMock(side_effect=RuntimeError("redis down"))
        order = _recording_execute(svc)
        request = _execute_request(self_service=True)
        request.started_at = datetime(2026, 10, 1, 12, 0)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(return_value={"kagura_memories": 0}),
            ),
            patch("services.account_erasure_service.sync_restored_payloads", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="redis down"):
                await svc._execute(request, _user())

        stmt = _request_update(order)
        params = stmt.compile().params
        assert "started_at" not in str(stmt.whereclause)
        assert params["id_1"] == request.id
        assert params["status"] == STATUS_FAILED
        assert params["failure_reason"].startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        assert params["user_id"] != "u-1"

    @pytest.mark.asyncio
    async def test_the_attempt_that_succeeds_keeps_the_history(self):
        svc = _service()
        _stub_execute_steps(svc)
        request = _execute_request(self_service=True)
        previous = {"attempts": 2, "step": "vector_store", "error_class": "QdrantError"}
        request.deleted_data_summary = {"retry": previous}

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={"kagura_memories": 3}),
        ):
            await svc._execute(request, _user())

        summary = svc._finalize.await_args.args[1]
        assert summary["retry"] == previous
        assert summary["qdrant"] == {"kagura_memories": 3}
        assert "postgres" in summary

    @pytest.mark.asyncio
    async def test_a_row_moved_meanwhile_answers_the_vector_store_error(self):
        """Nothing was rescheduled, so the caller is not told the request will
        run again: it gets the vector store's own error."""
        from utils.exceptions import QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        _recording_execute(svc, rowcount=0)
        request = _execute_request(self_service=True)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=QdrantError("down")),
            ),
            patch("services.account_erasure_service.logger") as log,
        ):
            with pytest.raises(QdrantError) as caught:
                await svc._execute(request, _user())

        assert str(caught.value) == "Qdrant service error: down"
        events = [c.args[0] for c in log.warning.call_args_list]
        assert "erasure_retry_request_moved" in events
        assert "erasure_retry_scheduled" not in events

    @pytest.mark.asyncio
    async def test_the_log_line_names_the_request_not_the_account(self):
        from utils.exceptions import ErasureRetryScheduledError, QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        _recording_execute(svc)
        request = _execute_request(self_service=True)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=QdrantError("down for u-1")),
            ),
            patch("services.account_erasure_service.logger") as log,
        ):
            with pytest.raises(ErasureRetryScheduledError):
                await svc._execute(request, _user())

        (call,) = [c for c in log.warning.call_args_list if c.args[0] == "erasure_retry_scheduled"]
        assert call.kwargs["request_id"] == str(request.id)
        assert call.kwargs["attempt"] == 1
        assert call.kwargs["error_class"] == "QdrantError"
        assert "u-1" not in repr(call)
        assert log.error.call_args_list == []  # not a failure (yet)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (None, (None, 0)),
        ({}, (None, 0)),
        ({"retry": "junk"}, (None, 0)),
        ({"retry": {"attempts": 2}}, ({"attempts": 2}, 2)),
        ({"retry": {"attempts": "2"}}, ({"attempts": "2"}, 0)),
        ({"retry": {"attempts": True}}, ({"attempts": True}, 0)),
        ({"retry": {"attempts": -1}}, ({"attempts": -1}, 0)),
        ({"retry": {}}, ({}, 0)),
    ],
)
def test_previous_retry(summary, expected):
    """#2282: an unreadable count starts the budget over rather than failing."""
    from services.account_erasure_service import _previous_retry

    assert _previous_retry(summary) == expected


@pytest.mark.asyncio
async def test_the_sweep_logs_a_rescheduled_request_and_goes_on():
    """#2282: ``_execute`` rescheduled the first request; the sweep logs it
    (request id, no account id), does not count it, and runs the next one."""
    from utils.datetime import utcnow
    from utils.exceptions import ErasureRetryScheduledError

    svc = _service()
    first, second = _execute_request(self_service=True), _execute_request(self_service=True)
    second.user_id = second.initiated_by = "u-2"
    claimed = MagicMock()
    claimed.scalars.return_value.all.return_value = [first, second]
    fresh = [MagicMock(), MagicMock()]
    fresh[0].scalar_one_or_none.return_value = first
    fresh[1].scalar_one_or_none.return_value = second
    svc.db.execute = AsyncMock(side_effect=[MagicMock(rowcount=0), claimed, *fresh])
    svc._load_user_or_none = AsyncMock(side_effect=[_user(), _user(user_id="u-2")])
    retry = ErasureRetryScheduledError(
        request_id=str(first.id),
        attempt=2,
        max_attempts=4,
        scheduled_for=utcnow() + timedelta(minutes=15),
        error_class="QdrantError",
    )
    svc._execute = AsyncMock(side_effect=[retry, None])

    with patch("services.account_erasure_service.logger") as log:
        assert await svc.sweep_pending_erasures() == 1

    assert svc._execute.await_count == 2
    (call,) = [
        c for c in log.warning.call_args_list if c.args[0] == "erasure_sweep_retry_scheduled"
    ]
    assert call.kwargs["request_id"] == str(first.id)
    assert call.kwargs["attempt"] == 2
    assert "u-1" not in repr(call)
    assert not [c for c in log.error.call_args_list if c.args[0] == "erasure_sweep_execute_failed"]


@pytest.mark.asyncio
async def test_admin_force_erase_forwards_the_reschedule():
    """#2282: the admin path runs ``_execute`` inline, so the caller gets the
    503 with the request id and the retry time."""
    from utils.datetime import utcnow
    from utils.exceptions import ErasureRetryScheduledError

    svc = _service()
    svc._load_user_or_404 = AsyncMock(return_value=_user())
    svc._find_active_request = AsyncMock(return_value=None)
    retry = ErasureRetryScheduledError(
        request_id="r-1",
        attempt=1,
        max_attempts=4,
        scheduled_for=utcnow() + timedelta(minutes=15),
        error_class="QdrantError",
    )
    svc._execute = AsyncMock(side_effect=retry)

    with patch("services.account_erasure_service.SystemAdminService") as admin_svc:
        admin_svc.return_value.can_delete_admin = AsyncMock(return_value=(True, None))
        with pytest.raises(ErasureRetryScheduledError) as caught:
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            )

    assert caught.value.status_code == 503
    assert caught.value.details["request_id"] == "r-1"
    svc._execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# #2304: re-queue embeddings after a cancelled or failed vector-store retry
# ---------------------------------------------------------------------------


def _spent_request(attempts: int | None) -> ErasureRequest:
    """A self-service ``cooling_off`` request; ``attempts`` spent at step 1
    (``None``: no ``retry`` block at all)."""
    request = ErasureRequest(
        user_id="u-1",
        user_email_hash="x",
        initiated_by="u-1",
        is_self_service=True,
        reason_code=REASON_SELF_SERVICE,
        status=STATUS_COOLING_OFF,
    )
    request.id = uuid4()
    if attempts is not None:
        request.deleted_data_summary = {
            "retry": {
                "attempts": attempts,
                "step": "vector_store",
                "error_class": "QdrantError",
                "last_attempt_at": "2026-10-01T12:00:00Z",
            }
        }
    return request


def _erasure_request_updates(order: list[Any]) -> list[Any]:
    from sqlalchemy.sql.dml import Update

    return [s for s in order if isinstance(s, Update) and str(s.table) == "erasure_requests"]


def _summary_update_params(stmt: Any) -> dict[str, Any]:
    """The JSONB merged into ``deleted_data_summary`` by the re-queue's
    summary UPDATE, and its guard."""
    from sqlalchemy.dialects import postgresql

    compiled = stmt.compile(dialect=postgresql.dialect())
    params = compiled.params
    merged = [v for v in params.values() if isinstance(v, dict) and "retry" in v]
    assert len(merged) == 1, params
    return {"sql": str(compiled), "params": params, "merged": merged[0]}


def _cas_returns(svc: AccountErasureService, summary: dict[str, Any] | None) -> None:
    """The cancel's compare-and-set returns ``summary`` as the moved row's
    (``RETURNING deleted_data_summary``, #2304); other statements as before."""
    from sqlalchemy.sql.dml import Update

    inner = svc.db.execute

    async def _execute(stmt, *a, **k):
        result = await inner(stmt, *a, **k)
        if isinstance(stmt, Update) and stmt._returning:
            result.first = MagicMock(return_value=SimpleNamespace(deleted_data_summary=summary))
        return result

    svc.db.execute = AsyncMock(side_effect=_execute)


class TestRequeueAfterACancelledRetry:
    @pytest.mark.asyncio
    async def test_a_cancel_after_a_spent_attempt_requeues_after_the_commit(self):
        from sqlalchemy.sql.dml import Update

        svc = _service()
        request = _spent_request(1)
        svc._find_active_request = AsyncMock(return_value=request)
        order = _recording_execute(svc)
        _cas_returns(svc, request.deleted_data_summary)
        svc.db.refresh = AsyncMock(side_effect=lambda row: order.append("refresh"))
        requeue = AsyncMock(side_effect=lambda db, user_id: order.append("requeue") or 3)

        with patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue):
            result = await svc.cancel_self_service(user_id="u-1")

        assert result is request
        requeue.assert_awaited_once_with(svc.db, "u-1")
        cas, summary_update = _erasure_request_updates(order)
        assert order == [cas, "commit", "requeue", summary_update, "commit", "refresh"]
        assert isinstance(cas, Update)
        assert cas.compile().params["status"] == STATUS_CANCELLED
        got = _summary_update_params(summary_update)
        # guarded by id + the terminal status
        assert got["params"]["id_1"] == request.id
        assert got["params"]["status_1"] == STATUS_CANCELLED
        retry = got["merged"]["retry"]
        assert {k: v for k, v in retry.items() if k != "requeued_at"} == {
            "attempts": 1,
            "step": "vector_store",
            "error_class": "QdrantError",
            "last_attempt_at": "2026-10-01T12:00:00Z",
            "embeddings_requeued": 3,
        }
        assert retry["requeued_at"].endswith("Z")
        assert "u-1" not in repr(got["merged"])

    @pytest.mark.parametrize("attempts", [None, 0])
    @pytest.mark.asyncio
    async def test_a_cancel_without_a_spent_attempt_requeues_nothing(self, attempts):
        svc = _service()
        request = _spent_request(attempts)
        svc._find_active_request = AsyncMock(return_value=request)
        order = _recording_execute(svc)
        _cas_returns(svc, request.deleted_data_summary)
        requeue = AsyncMock(return_value=0)

        with patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue):
            await svc.cancel_self_service(user_id="u-1")

        requeue.assert_not_awaited()
        assert len(_erasure_request_updates(order)) == 1

    @pytest.mark.asyncio
    async def test_the_gate_reads_the_row_the_cancel_moved(self):
        """A run that rescheduled the request between the cancel's read and
        its compare-and-set spent an attempt: the CAS's ``RETURNING`` row
        decides, not the earlier read."""
        svc = _service()
        request = _spent_request(None)  # read before the run
        svc._find_active_request = AsyncMock(return_value=request)
        order = _recording_execute(svc)
        moved = _spent_request(1).deleted_data_summary
        _cas_returns(svc, moved)
        requeue = AsyncMock(return_value=2)

        with patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue):
            await svc.cancel_self_service(user_id="u-1")

        requeue.assert_awaited_once_with(svc.db, "u-1")
        cas, summary_update = _erasure_request_updates(order)
        assert "RETURNING erasure_requests.deleted_data_summary" in str(cas)
        retry = _summary_update_params(summary_update)["merged"]["retry"]
        assert retry["attempts"] == 1
        assert retry["embeddings_requeued"] == 2

    @pytest.mark.asyncio
    async def test_a_lost_cancel_requeues_nothing(self):
        svc = _service()
        svc._find_active_request = AsyncMock(return_value=_spent_request(2))
        now_status = MagicMock()
        # #2306: the lost compare-and-set re-reads the row as the caller's
        now_status.scalar_one_or_none.return_value = MagicMock(status="in_progress")
        svc.db.execute = AsyncMock(side_effect=[_lost_cas(), now_status])
        requeue = AsyncMock(return_value=0)

        with patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue):
            with pytest.raises(ErasureAlreadyInProgressError):
                await svc.cancel_self_service(user_id="u-1")

        requeue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_requeue_does_not_fail_the_cancel(self):
        svc = _service()
        request = _spent_request(1)
        svc._find_active_request = AsyncMock(return_value=request)
        order = _recording_execute(svc)
        _cas_returns(svc, request.deleted_data_summary)
        requeue = AsyncMock(side_effect=ConnectionError("Key (user_id)=(u-1) lost"))

        with (
            patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue),
            patch("services.account_erasure_service.logger") as log,
        ):
            result = await svc.cancel_self_service(user_id="u-1")

        assert result is request
        assert order[-1] == "rollback"
        assert len(_erasure_request_updates(order)) == 1  # no summary write
        log.error.assert_called_once_with(
            "erasure_embeddings_requeue_failed",
            request_id=str(request.id),
            error_class="ConnectionError",
        )
        svc.db.refresh.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_an_unrecorded_count_is_logged_not_raised(self):
        svc = _service()
        request = _spent_request(1)
        svc._find_active_request = AsyncMock(return_value=request)
        svc.db.execute = AsyncMock(side_effect=[MagicMock(rowcount=1), MagicMock(rowcount=0)])
        _cas_returns(svc, request.deleted_data_summary)

        with (
            patch(
                "services.account_erasure_service.requeue_subject_embeddings",
                new=AsyncMock(return_value=2),
            ),
            patch("services.account_erasure_service.logger") as log,
        ):
            await svc.cancel_self_service(user_id="u-1")

        log.warning.assert_called_once_with(
            "erasure_embeddings_requeue_unrecorded", request_id=str(request.id), memories=2
        )

    @pytest.mark.asyncio
    async def test_the_requeue_logs_carry_no_account_id(self):
        svc = _service()
        request = _spent_request(1)
        svc._find_active_request = AsyncMock(return_value=request)
        svc.db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
        _cas_returns(svc, request.deleted_data_summary)

        with (
            patch(
                "services.account_erasure_service.requeue_subject_embeddings",
                new=AsyncMock(return_value=4),
            ),
            patch("services.account_erasure_service.logger") as log,
        ):
            await svc.cancel_self_service(user_id="u-1")

        requeued = [
            c for c in log.info.call_args_list if c.args[0] == "erasure_embeddings_requeued"
        ]
        assert len(requeued) == 1
        assert requeued[0].kwargs["memories"] == 4
        assert "u-1" not in repr(requeued)


class TestRequeueAfterTheLastAttempt:
    @pytest.mark.asyncio
    async def test_the_last_attempt_requeues_after_the_failure_commit(self):
        from models.erasure import STATUS_FAILED
        from utils.exceptions import QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        order = _recording_execute(svc)
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"retry": {"attempts": 3, "step": "vector_store"}}
        requeue = AsyncMock(side_effect=lambda db, user_id: order.append("requeue") or 7)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=QdrantError("connection refused")),
            ),
            patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue),
        ):
            with pytest.raises(QdrantError):
                await svc._execute(request, _user())

        requeue.assert_awaited_once_with(svc.db, "u-1")
        failure, summary_update = _erasure_request_updates(order)
        assert order[-6:] == ["rollback", failure, "commit", "requeue", summary_update, "commit"]
        assert failure.compile().params["status"] == STATUS_FAILED
        got = _summary_update_params(summary_update)
        assert got["params"]["id_1"] == request.id
        assert got["params"]["status_1"] == STATUS_FAILED
        retry = got["merged"]["retry"]
        assert retry["attempts"] == 4
        assert retry["embeddings_requeued"] == 7

    @pytest.mark.asyncio
    async def test_a_stale_run_requeues_nothing(self):
        """#2306: the failure UPDATE is guarded by this run's claim; when it
        matched nothing (a newer run owns the row) nothing is re-queued."""
        from utils.exceptions import QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        _recording_execute(svc, rowcount=0)
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"retry": {"attempts": 3, "step": "vector_store"}}
        requeue = AsyncMock(return_value=0)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=QdrantError("connection refused")),
            ),
            patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue),
            patch("services.account_erasure_service.logger") as log,
        ):
            with pytest.raises(QdrantError):
                await svc._execute(request, _user())

        requeue.assert_not_awaited()
        log.warning.assert_any_call("erasure_failure_handler_stale_run", request_id=str(request.id))

    @pytest.mark.asyncio
    async def test_a_failed_requeue_keeps_the_vector_store_error(self):
        from utils.exceptions import QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        _recording_execute(svc)
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"retry": {"attempts": 3, "step": "vector_store"}}

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=QdrantError("connection refused")),
            ),
            patch(
                "services.account_erasure_service.requeue_subject_embeddings",
                new=AsyncMock(side_effect=RuntimeError("lock timeout")),
            ),
        ):
            with pytest.raises(QdrantError):
                await svc._execute(request, _user())

    @pytest.mark.asyncio
    async def test_a_rescheduled_attempt_requeues_nothing(self):
        from utils.exceptions import ErasureRetryScheduledError, QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        _recording_execute(svc)
        requeue = AsyncMock(return_value=0)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=QdrantError("down")),
            ),
            patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue),
        ):
            with pytest.raises(ErasureRetryScheduledError):
                await svc._execute(_execute_request(self_service=True), _user())

        requeue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_another_error_at_step_one_requeues_nothing(self):
        svc = _service()
        _stub_execute_steps(svc)
        _recording_execute(svc)
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"retry": {"attempts": 3, "step": "vector_store"}}
        requeue = AsyncMock(return_value=0)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=RuntimeError("bad config")),
            ),
            patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue),
        ):
            with pytest.raises(RuntimeError):
                await svc._execute(request, _user())

        requeue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_vector_store_error_at_a_later_step_requeues_nothing(self):
        from utils.exceptions import QdrantError

        svc = _service()
        _stub_execute_steps(svc)
        svc._delete_postgres = AsyncMock(side_effect=QdrantError("down"))
        _recording_execute(svc)
        request = _execute_request(self_service=True)
        request.deleted_data_summary = {"retry": {"attempts": 3, "step": "vector_store"}}
        requeue = AsyncMock(return_value=0)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(return_value={"kagura_memories": 0}),
            ),
            patch("services.account_erasure_service.requeue_subject_embeddings", new=requeue),
        ):
            with pytest.raises(QdrantError):
                await svc._execute(request, _user())

        requeue.assert_not_awaited()


class TestRequeueSubjectEmbeddings:
    @staticmethod
    def _db(batches: list[list[Any]]) -> tuple[MagicMock, list[Any]]:
        """A session recording statements and commits; each UPDATE returns
        the next batch of ids."""
        from sqlalchemy.sql.dml import Update

        db = MagicMock()
        order: list[Any] = []
        pending = iter(batches)

        def _execute(stmt, *a, **k):
            order.append(stmt)
            result = MagicMock()
            if isinstance(stmt, Update):
                result.scalars.return_value.all.return_value = next(pending)
            return result

        db.execute = AsyncMock(side_effect=_execute)
        db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
        return db, order

    @pytest.mark.asyncio
    async def test_batches_on_a_keyset_until_a_short_one(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.sql.elements import TextClause

        from services.account_erasure_service import (
            EMBEDDING_REQUEUE_BATCH,
            requeue_subject_embeddings,
        )

        assert EMBEDDING_REQUEUE_BATCH == 500
        first_ids = sorted(uuid4() for _ in range(500))
        db, order = self._db([first_ids, [uuid4() for _ in range(3)]])

        assert await requeue_subject_embeddings(db, "u-1") == 503

        timeout1, first, commit1, timeout2, second, commit2 = order
        assert (commit1, commit2) == ("commit", "commit")
        for timeout in (timeout1, timeout2):
            # each batch's transaction bounds its lock wait
            assert isinstance(timeout, TextClause)
            assert str(timeout) == "SET LOCAL lock_timeout = '5s'"
        sqls = []
        for stmt in (first, second):
            assert str(stmt.table) == "memories"
            assert stmt.get_execution_options()["synchronize_session"] is False
            compiled = stmt.compile(dialect=postgresql.dialect())
            sql = " ".join(str(compiled).split())
            params = compiled.params
            assert sql.startswith("UPDATE memories SET embedding_status=")
            assert sql.endswith("RETURNING memories.id")
            assert "memories.user_id = %(user_id_1)s" in sql
            assert "memories.deleted_at IS NULL" in sql
            assert "memories.embedding_status IN (__[POSTCOMPILE_embedding_status_1])" in sql
            # a stale processing row with no budget left stays the sweep's
            assert "NOT (memories.embedding_status = %(embedding_status_2)s" in sql
            assert "memories.embedding_retry_count >=" in sql
            assert "ORDER BY memories.id" in sql
            assert "::INTEGER FOR NO KEY UPDATE" in sql
            assert "resource_id" not in sql  # indexer rows are rebuilt too
            assert params["user_id_1"] == "u-1"
            assert list(params["embedding_status_1"]) == ["success", "processing"]
            assert params["embedding_status_2"] == "processing"
            assert params["embedding_status"] == "pending"
            assert 500 in params.values()
            # the status only: updated_at and the pipeline's fields stay
            assert set(stmt._values) == {stmt.table.c.embedding_status}
            sqls.append((sql, params))
        # the second batch starts after the first's last id
        assert "memories.id >" not in sqls[0][0]
        assert "memories.id > %(id_1)s" in sqls[1][0]
        assert sqls[1][1]["id_1"] == first_ids[-1]

    @pytest.mark.asyncio
    async def test_nothing_to_requeue(self):
        from services.account_erasure_service import requeue_subject_embeddings

        db, order = self._db([[]])

        assert await requeue_subject_embeddings(db, "u-1") == 0
        assert len(order) == 3  # lock timeout, the UPDATE, its commit
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_rollback_does_not_escape(self):
        svc = _service()
        svc.db.rollback = AsyncMock(side_effect=ConnectionError("gone"))
        request_id = uuid4()

        with (
            patch(
                "services.account_erasure_service.requeue_subject_embeddings",
                new=AsyncMock(side_effect=ConnectionError("gone")),
            ),
            patch("services.account_erasure_service.logger") as log,
        ):
            await svc._requeue_after_vector_store_failure(
                request_id, "u-1", {"attempts": 1}, status=STATUS_CANCELLED
            )

        log.error.assert_called_once_with(
            "erasure_embeddings_requeue_failed",
            request_id=str(request_id),
            error_class="ConnectionError",
        )
