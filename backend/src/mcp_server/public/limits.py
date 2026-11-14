"""Fixed caps and Redis minute buckets of the public MCP endpoint (#2675).

The caps are constants, not settings: they bound the work one anonymous
request can cause. The buckets are the per-minute limit on this path (the
workspace's daily MCP quota belongs to the authenticated dispatcher, which
anonymous calls never reach), keyed by the endpoint id and, on the session
era, the server-minted session id — never by IP or anything else a client
sends. A session is a client connection, not a person. Since #2805 they are
not the only limit: a call they admit is then charged to the workspace-wide
seven-day budget (``services.public_usage_budget``, when the operator switch
is on), which a call they refuse never reaches.

Redis unreadable: ``tools/call`` is refused (every public ``recall`` costs an
embedding call, so an outage must not mean "unlimited"); the control methods
(``initialize``, ``tools/list``, ``server/discover``, ``ping``) are served.
"""

from __future__ import annotations

from typing import Literal

from config.settings import get_settings
from db.redis import incrby_counter
from mcp_server.public.publication import ServingEndpoint
from utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_MAX_REQUEST_BYTES = 64 * 1024
PUBLIC_MAX_QUERY_CHARS = 500
PUBLIC_DEFAULT_K = 5
PUBLIC_MAX_K = 20
PUBLIC_MAX_EXPLORE_DEPTH = 2
PUBLIC_DEFAULT_EXPLORE_LIMIT = 10
PUBLIC_MAX_EXPLORE_LIMIT = 25
PUBLIC_DEFAULT_TAGS_LIMIT = 50
PUBLIC_MAX_TAGS_LIMIT = 100
PUBLIC_MAX_MIN_COUNT = 10_000
PUBLIC_MAX_FILTER_TAGS = 10
PUBLIC_MAX_TAG_CHARS = 100
PUBLIC_MIN_MAX_CHARS = 10_000
PUBLIC_MAX_MAX_CHARS = 40_000
PUBLIC_DEFAULT_MAX_CHARS = 20_000
PUBLIC_HANDSHAKE_MULTIPLIER = 5
PUBLIC_RETRY_AFTER_SECONDS = 60

Bucket = Literal["call", "control"]


class PublicRateLimitUnavailable(Exception):
    """The call bucket could not be read; the call is refused (fail closed)."""


def _call_limit() -> int:
    return int(get_settings().mcp_public_rate_limit_per_minute)


def _session_limit() -> int:
    return int(get_settings().mcp_public_session_rate_limit_per_minute)


async def _over(key: str, limit: int) -> bool:
    return await incrby_counter(key, amount=1, ttl=60) > limit


async def check_public_rate_limit(
    endpoint: ServingEndpoint, *, session_id: str | None, bucket: Bucket
) -> int | None:
    """Count one request against the endpoint's minute buckets.

    Args:
        endpoint: The serving endpoint.
        session_id: The server-minted session id (session era ``tools/call``
            only), counted in its own bucket.
        bucket: ``"call"`` for ``tools/call``, ``"control"`` for the
            handshake / listing methods.

    Returns:
        ``PUBLIC_RETRY_AFTER_SECONDS`` when a bucket is exceeded, else ``None``.

    Raises:
        PublicRateLimitUnavailable: Redis could not be read for a call.
    """
    base = f"public_mcp:{endpoint.id}"
    try:
        if bucket == "control":
            limit = PUBLIC_HANDSHAKE_MULTIPLIER * _call_limit()
            if await _over(f"{base}:ctl:minute", limit):
                logger.warning(
                    "public_mcp_rate_limited", endpoint_id=str(endpoint.id), bucket="ctl"
                )
                return PUBLIC_RETRY_AFTER_SECONDS
            return None
        if await _over(f"{base}:call:minute", _call_limit()):
            logger.warning("public_mcp_rate_limited", endpoint_id=str(endpoint.id), bucket="call")
            return PUBLIC_RETRY_AFTER_SECONDS
        if session_id is not None and await _over(
            f"{base}:sess:{session_id}:minute", _session_limit()
        ):
            logger.warning(
                "public_mcp_rate_limited", endpoint_id=str(endpoint.id), bucket="session"
            )
            return PUBLIC_RETRY_AFTER_SECONDS
        return None
    except Exception as exc:
        logger.warning(
            "public_mcp_rate_limit_unavailable",
            endpoint_id=str(endpoint.id),
            bucket=bucket,
            error_type=type(exc).__name__,
        )
        if bucket == "call":
            raise PublicRateLimitUnavailable from exc
        return None
