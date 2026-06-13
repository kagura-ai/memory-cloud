"""Issue #992 Phase 1 — auth-boundary error-shape convergence + 422 handler.

Covers two things this PR introduces:

1. A ``RequestValidationError`` handler that converts FastAPI's default
   ``{"detail": [ ... ]}`` 422 body into the canonical ``{error, message,
   details}`` envelope (matching ``ValidationError`` / ``VAL-001``), and
   — critically — STRIPS the echoed ``input`` (and ``ctx``/``url``) so a
   rejected payload is never reflected back on the frozen public surface
   (CWE-639 / info-disclosure defense; gate1 CSO finding (d)).

2. Auth-boundary route handlers now raise canonical ``MemoryCloudException``
   subclasses instead of raw ``HTTPException``, so their bodies match the
   ``{error, message, details}`` contract with status codes unchanged.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from fastapi.exceptions import RequestValidationError


def _run(coro):
    return asyncio.run(coro)


def test_request_validation_handler_uses_canonical_envelope_and_strips_input():
    from api.main import request_validation_exception_handler

    # A realistic FastAPI/pydantic-v2 error entry: note the ``input`` echoes
    # the caller's submitted secret, and ``url``/``ctx`` may carry fragments.
    exc = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", "password"),
                "msg": "Field required",
                "input": {"login_id": "alice", "secret": "hunter2-PLAINTEXT"},
                "url": "https://errors.pydantic.dev/2.0/v/missing",
                "ctx": {"foo": "bar"},
            }
        ]
    )
    req = MagicMock()
    req.url.path = "/api/v1/auth/login"

    resp = _run(request_validation_exception_handler(req, exc))
    body = json.loads(resp.body)

    assert resp.status_code == 422
    assert body["error"] == "VAL-001"
    assert body["message"]  # human-readable, non-empty
    # Trimmed projection only — loc/msg/type, nothing else.
    assert body["details"]["errors"] == [
        {"loc": ["body", "password"], "msg": "Field required", "type": "missing"}
    ]
    # The submitted payload MUST NOT be echoed anywhere in the response.
    assert "hunter2-PLAINTEXT" not in resp.body.decode()
    assert "input" not in body["details"]["errors"][0]


def test_request_validation_handler_handles_multiple_errors():
    from api.main import request_validation_exception_handler

    exc = RequestValidationError(
        [
            {"type": "missing", "loc": ("body", "a"), "msg": "Field required", "input": {}},
            {"type": "string_type", "loc": ("body", "b"), "msg": "not a string", "input": 5},
        ]
    )
    req = MagicMock()
    req.url.path = "/x"

    resp = _run(request_validation_exception_handler(req, exc))
    body = json.loads(resp.body)
    assert len(body["details"]["errors"]) == 2
    assert {e["loc"][-1] for e in body["details"]["errors"]} == {"a", "b"}


# --- #992 Phase 2: global StarletteHTTPException stopgap handler ---


def test_http_exception_handler_reshapes_to_canonical_and_preserves_headers():
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from api.main import http_exception_handler

    req = MagicMock()
    req.url.path = "/api/v1/whatever"
    exc = StarletteHTTPException(
        status_code=403,
        detail="Insufficient scope",
        headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
    )

    resp = _run(http_exception_handler(req, exc))
    body = json.loads(resp.body)

    assert resp.status_code == 403
    # Canonical envelope with the reserved HTTP-<status> placeholder code.
    assert body == {
        "error": "HTTP-403",
        "message": "Insufficient scope",
        "details": {},
    }
    # RFC 6750 challenge header must survive the reshape.
    assert resp.headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope"'


def test_http_exception_handler_preserves_structured_dict_detail():
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from api.main import http_exception_handler

    # external_keys.py raises dict-detail HTTPExceptions for conflict payloads;
    # the handler must preserve the object under details.detail (not drop it),
    # so the frontend consumer branching on detail.error keeps working.
    req = MagicMock()
    req.url.path = "/api/v1/external-keys"
    payload = {"error": "reranker_provider_conflict", "conflicting_provider": "cohere"}
    exc = StarletteHTTPException(status_code=409, detail=payload)

    resp = _run(http_exception_handler(req, exc))
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["error"] == "HTTP-409"
    assert body["message"] == "Request failed"  # no single human string for a dict
    assert body["details"]["detail"] == payload  # structured detail preserved


# --- auth-boundary conversions: status + error_code parity ---


def test_invalid_credentials_error_is_401_auth002():
    from utils.exceptions import InvalidCredentialsError

    e = InvalidCredentialsError()
    assert e.status_code == 401
    assert e.error_code == "AUTH-002"
    assert e.message == "Invalid credentials"


def test_mfa_session_uses_authentication_error_401():
    from utils.exceptions import AuthenticationError

    e = AuthenticationError("Invalid or expired MFA session")
    assert e.status_code == 401
    assert e.error_code == "AUTH-001"
    assert e.message == "Invalid or expired MFA session"


def test_admin_protection_error_keeps_message_and_strips_reason():
    # admin.py self-delete / protected-admin conversions.
    from utils.exceptions import AdminProtectionError

    e = AdminProtectionError(
        "Cannot delete the last remaining system administrator", reason="admin_protected"
    )
    assert e.status_code == 403
    assert e.error_code == "ADMIN-001"
    assert e.message == "Cannot delete the last remaining system administrator"
    # reason is a private classification, never serialized (CWE-639 pattern).
    assert e.reason == "admin_protected"
    assert "reason" not in e.details
