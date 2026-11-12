"""Strict anonymous burst protection for isolated OIDC protocol work."""

from __future__ import annotations

import hashlib
import time

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from config.settings import get_settings
from db.redis import increment_counter
from utils.logger import get_logger

logger = get_logger(__name__)
_PATHS = frozenset({"/oidc/authorize", "/oidc/token", "/oidc/revoke", "/oidc/userinfo"})
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"error": "temporarily_unavailable"},
        status_code=503,
        headers={**_NO_STORE, "Retry-After": "1"},
    )


async def check_oidc_rate_limit(request: Request) -> Response | None:
    """Refuse over-budget or unverifiable protocol requests before DB/crypto.

    ``request.client.host`` is the existing ASGI peer identity. Uvicorn may
    replace it only under its configured trusted proxy policy; raw forwarded
    headers are deliberately not interpreted here. No credential, request
    body, user ID or application-supplied client ID participates in the key.
    """
    path = request.url.path
    if path not in _PATHS:
        return None
    try:
        limit = get_settings().oidc_rate_limit_per_minute
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return _unavailable()
        peer = request.client.host if request.client else "unknown"
        peer_digest = hashlib.sha256(peer.encode("utf-8")).hexdigest()
        now = int(time.time())
        operation = path.rsplit("/", 1)[-1]
        key = f"oidc_rate_limit:{operation}:{peer_digest}:{now // 60}"
        count = await increment_counter(key, ttl=60)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return _unavailable()
    except Exception:
        # Redis wrappers can carry connection URLs in exception strings; keep
        # the failure diagnostic fixed and refuse authentication work.
        logger.warning("oidc_rate_limit_unavailable")
        return _unavailable()
    if count > limit:
        retry_after = max(1, 60 - now % 60)
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": "OIDC rate limit exceeded."},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(retry_after)},
        )
    return None
