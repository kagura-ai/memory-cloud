"""Route-level tests for /api/v1/me/account/erasure endpoints (Issue #469).

Covers the response-shape contract for ``POST /me/account/erasure-request``:

| auth_method | email send result | response confirm_token | HTTP status |
|---|---|---|---|
| password    | n/a               | raw                    | 201 |
| oauth       | success           | null                   | 201 |
| oauth       | raises            | n/a (rolled back)      | 503 |
| password    | regression check  | raw (unchanged)        | 201 |

The service-layer behaviour (when to issue the token, when to dispatch
email, when to rollback, how logs are redacted on failure) is covered by
``tests/services/test_account_erasure_service.py``. These route-layer
tests verify only that the handler wires the service return into the
response schema correctly and that ``EmailDispatchError`` propagates
with ``status_code=503`` (mapped by the global
``memory_cloud_exception_handler`` in api/main.py — verified separately).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from api.routes import me_account
from api.routes.me_account import (
    ErasureRequestCreateResponse,
    create_erasure_request,
)
from utils.exceptions import EmailDispatchError


def _request() -> SimpleNamespace:
    """Minimal Request stand-in. Handler reads ``.client.host`` and
    ``.headers.get("user-agent")``."""
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"user-agent": "pytest"},
    )


def _user_session(*, user_id: str = "u-1") -> dict:
    """Minimal SessionUser dict — the handler only reads ``user_id``.

    The full session payload is richer (``sub``, ``email``, ``name``,
    ``role``) but those fields are unused by ``create_erasure_request``;
    keeping the stub minimal makes test intent obvious.
    """
    return {"user_id": user_id}


def _record() -> SimpleNamespace:
    """Stand-in for the ``ErasureRequest`` ORM row returned by the service."""
    return SimpleNamespace(
        id=uuid4(),
        status="pending",
        requested_at=datetime.now(UTC),
    )


class TestCreateErasureRequest:
    async def test_password_user_returns_raw_confirm_token(self):
        """Matrix case 1: password / n/a / raw / 201."""
        record = _record()
        raw_token = "raw-tok-pw-1"
        with patch.object(me_account, "AccountErasureService") as mock_svc_cls:
            mock_svc_cls.return_value.request_self_service_erasure = AsyncMock(
                return_value=(record, raw_token),
            )
            result = await create_erasure_request(
                request=_request(),
                user=_user_session(),
                db=AsyncMock(),
            )

        assert isinstance(result, ErasureRequestCreateResponse)
        assert result.request_id == record.id
        assert result.status == "pending"
        assert result.confirm_token == raw_token

    async def test_oauth_user_email_success_returns_null_token(self):
        """Matrix case 2: oauth / email success / null / 201.

        Service has already dispatched ``send_erasure_confirmation`` —
        verified by ``tests/services/test_account_erasure_service.py``.
        Here we verify the handler faithfully passes ``(record, None)``
        into the response schema.
        """
        record = _record()
        with patch.object(me_account, "AccountErasureService") as mock_svc_cls:
            mock_svc_cls.return_value.request_self_service_erasure = AsyncMock(
                return_value=(record, None),
            )
            result = await create_erasure_request(
                request=_request(),
                user=_user_session(user_id="u-oauth"),
                db=AsyncMock(),
            )

        assert isinstance(result, ErasureRequestCreateResponse)
        assert result.request_id == record.id
        assert result.confirm_token is None, (
            "OAuth users must NOT receive the raw token in the response body"
        )

    async def test_oauth_email_dispatch_failure_returns_503_no_echo(self):
        """Matrix case 3: oauth / email raises / 503 (no token echo).

        The service raises ``EmailDispatchError`` (status_code=503) when
        the OAuth confirmation email fails. The handler must NOT catch
        this — the global ``memory_cloud_exception_handler`` maps the
        ``status_code`` attribute to the HTTP response shape.

        ``EmailDispatchError`` is zero-argument by design (see its
        docstring + ``utils/exceptions.py``) so no token-bearing string
        can land in ``self.message`` and reach the JSON response body.
        This test verifies that contract from the route's perspective:
        ``self.message`` after construction is the fixed sentinel
        "Email dispatch service error", with no caller-supplied tail.
        """
        with (
            patch.object(me_account, "AccountErasureService") as mock_svc_cls,
            patch.object(me_account, "logger") as mock_logger,
        ):
            mock_svc_cls.return_value.request_self_service_erasure = AsyncMock(
                side_effect=EmailDispatchError(),
            )
            with pytest.raises(EmailDispatchError) as exc_info:
                await create_erasure_request(
                    request=_request(),
                    user=_user_session(user_id="u-oauth"),
                    db=AsyncMock(),
                )

        assert exc_info.value.status_code == 503

        # ``self.message`` is what ``memory_cloud_exception_handler`` puts
        # in the JSON response body. Asserting the fixed shape protects
        # against a future regression that re-introduces a free-form
        # ``message=`` parameter to the exception class.
        assert exc_info.value.message == "Email dispatch service error"

        # The route handler does no logging on the failure path (the
        # service already logged with redaction). This thin check just
        # confirms the route did not regress and start logging exception
        # details. Service-side redaction is asserted in
        # tests/services/test_account_erasure_service.py.
        haystack_parts: list[str] = []
        for call in mock_logger.method_calls:
            _name, args, kwargs = call
            haystack_parts.extend(str(a) for a in args)
            for k, v in kwargs.items():
                haystack_parts.append(str(k))
                haystack_parts.append(str(v))
        haystack = " ".join(haystack_parts)
        # No token-shaped substring should appear in any route-level log.
        assert "token" not in haystack.lower(), (
            "route handler must not log exception details that could contain the token"
        )

    async def test_password_user_regression_token_unchanged(self):
        """Matrix case 4: password / regression / raw (unchanged) / 201.

        Asserts that adding the OAuth confirmation wiring did not change
        the password-path return shape. Identical assertion to case 1
        with a different user — guards against accidental coupling.
        """
        record = _record()
        raw_token = "regression-tok-pw"
        with patch.object(me_account, "AccountErasureService") as mock_svc_cls:
            mock_svc_cls.return_value.request_self_service_erasure = AsyncMock(
                return_value=(record, raw_token),
            )
            result = await create_erasure_request(
                request=_request(),
                user=_user_session(user_id="u-pw-2"),
                db=AsyncMock(),
            )

        assert isinstance(result, ErasureRequestCreateResponse)
        assert result.confirm_token == raw_token
        assert result.confirm_token is not None, (
            "password path must continue receiving the raw token in response"
        )
