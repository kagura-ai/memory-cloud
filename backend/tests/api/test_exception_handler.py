"""Tests for the global memory_cloud_exception_handler in api/main.py.

Locks down two contracts critical to the #401 CWE-639 fix:

1. ``AuthorizationError`` MUST emit ``details: {}`` regardless of what the
   exception carries internally — defense in depth against a future
   contributor accidentally passing forensics kwargs as ``**details``.
2. Other ``MemoryCloudException`` subclasses (NotFoundException, RateLimitError,
   etc.) MUST preserve their ``details`` payload — clients depend on these
   fields (e.g. ``retry_after``, ``feature``, ``resource_id``).

These tests run at the handler level without a TestClient — no Redis, no DB,
no FastAPI app required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import Request

from api.main import memory_cloud_exception_handler
from utils.exceptions import (
    AdminProtectionError,
    AuthorizationError,
    NotFoundException,
    RateLimitError,
)


def _request_mock(path: str = "/test") -> Request:
    """Minimal Request stub — the handler only reads ``request.url.path``."""
    req = MagicMock(spec=Request)
    req.url.path = path
    return req


class TestAuthorizationErrorFilter:
    """CWE-639 defense in depth — AuthorizationError NEVER leaks details."""

    @pytest.mark.asyncio
    async def test_strips_smuggled_details_keys(self):
        """A future contributor accidentally adding ``**details`` to an
        AuthorizationError raise (e.g. ``raise AuthorizationError("msg",
        forensic_marker="smuggled")``) must NOT see those keys in the
        response body. The handler enforces this by overriding ``details``
        to ``{}`` for any AuthorizationError instance.
        """
        exc = AuthorizationError("Insufficient permissions", smuggled_key="leak_me")
        # Verify the kwarg reached the exception (this is the "if step 1 fails"
        # scenario — the test_exceptions.py contract prevents this in practice,
        # but the handler is the SECOND line of defense).
        assert exc.details == {"smuggled_key": "leak_me"}

        response = await memory_cloud_exception_handler(_request_mock(), exc)
        body = json.loads(response.body)
        assert body["details"] == {}, (
            "AuthorizationError MUST emit details={} regardless of what the "
            "exception carries. CWE-639: leaking the deny sub-reason re-introduces "
            "the workspace-enumeration vector that the uniform message hides."
        )
        # Other fields should still be set correctly.
        assert body["error"] == "AUTH-101"
        assert body["message"] == "Insufficient permissions"
        assert response.status_code == 403


class TestAdminProtectionErrorFilter:
    """CWE-639 defense in depth — AdminProtectionError NEVER leaks details.

    Mirrors the AuthorizationError contract one tier down: the exception
    type is constructed with no ``**details`` passthrough today, but the
    handler still strips ``details`` so a future contributor cannot quietly
    add a forensics field by routing through ``exc.details`` directly.
    """

    @pytest.mark.asyncio
    async def test_strips_smuggled_details_keys(self):
        exc = AdminProtectionError(
            "Cannot demote the initial system administrator.",
            reason="initial_admin",
        )
        # Simulate a future contributor poking at ``details`` directly
        # (bypassing the constructor's keyword-only signature).
        exc.details["smuggled_key"] = "leak_me"
        assert exc.details == {"smuggled_key": "leak_me"}

        response = await memory_cloud_exception_handler(_request_mock(), exc)
        body = json.loads(response.body)
        assert body["details"] == {}, (
            "AdminProtectionError MUST emit details={} regardless of what the "
            "exception carries. Defense in depth against future kwarg drift."
        )
        assert body["error"] == "ADMIN-001"
        assert body["message"] == "Cannot demote the initial system administrator."
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_reason_never_serialized(self):
        """``exc.reason`` is structured-log nomenclature only — confirm it
        never appears in the response body even when set."""
        exc = AdminProtectionError(
            "Cannot demote the last remaining system administrator.",
            reason="last_admin",
        )
        response = await memory_cloud_exception_handler(_request_mock(), exc)
        body = json.loads(response.body)
        assert "reason" not in body
        assert "reason" not in body.get("details", {})


class TestOtherExceptionDetailsPreserved:
    """Negative test — the AuthorizationError filter MUST NOT over-fire and
    strip ``details`` from sibling MemoryCloudException subclasses that legitimately
    expose structured fields to clients (NotFoundException's resource_id,
    RateLimitError's retry_after, etc.)."""

    @pytest.mark.asyncio
    async def test_not_found_exception_preserves_details(self):
        """NotFoundException currently sets no kwargs in details, but if a
        future change does (e.g. via ``**details``), the handler must NOT
        strip them — clients may rely on structured 404 info.
        """
        exc = NotFoundException("Context", "ctx-uuid-123")
        response = await memory_cloud_exception_handler(_request_mock(), exc)
        body = json.loads(response.body)
        # NotFoundException's __init__ does not pass any **details today, so
        # body["details"] is {} here. The test asserts that the handler does
        # NOT actively erase what the exception had (filter is targeted to
        # AuthorizationError, not blanket-applied).
        assert body["details"] == exc.details
        assert body["error"] == "RES-001"
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_rate_limit_error_preserves_retry_after(self):
        """RateLimitError carries ``retry_after`` in details — clients depend
        on this to back off properly. The handler MUST pass it through."""
        exc = RateLimitError("Rate limit exceeded", retry_after=42)
        response = await memory_cloud_exception_handler(_request_mock(), exc)
        body = json.loads(response.body)
        assert body["details"] == {"retry_after": 42}
        assert response.status_code == 429
