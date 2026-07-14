"""Correlation middleware (RFC-0002 P0-4, Issue #1277).

Parses W3C Trace Context (``traceparent``) + ``baggage`` from every
``/api/v1/*`` request into the per-request correlation contextvar, as a
sibling of ``RequestLoggingMiddleware``. Advisory-only: a malformed header
never fails the request (missing/invalid → server-generated trace/span,
dropped tokens). The contextvar is reset after the response so a value from
one request can never bleed into another (defense in depth on top of ASGI
per-request task isolation).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from api.correlation import build_correlation_from_headers, set_correlation


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Populate the correlation contextvar from trace headers per request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Reset first so a leaked value can never survive into this request,
        # then parse (advisory — never raises).
        set_correlation(None)
        try:
            ctx = build_correlation_from_headers(
                traceparent=request.headers.get("traceparent"),
                baggage=request.headers.get("baggage"),
            )
            set_correlation(ctx)
        except Exception:  # pragma: no cover - defensive; correlation is advisory
            set_correlation(None)
        try:
            return await call_next(request)
        finally:
            set_correlation(None)
