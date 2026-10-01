"""Slack ``conversations.list`` proxy for the connector channel picker (#1391).

This module is a thin, read-only client around Slack's ``conversations.list``
Web API. It exists so the connector's Fernet-decrypted bot token stays
server-side: the settings channel picker calls
``GET /workspace-connectors/{id}/channels`` and this module makes the outbound
Slack request on the browser's behalf.

Security invariants
-------------------
* The bot token is passed only in the outbound ``Authorization`` header. It is
  never logged (log events carry counts / cursors only) and never returned.
* The response is minimized to ``id`` / ``name`` / ``is_private`` — no member
  counts, topics, purposes, or other channel metadata.

Slack error mapping (never a raw 5xx):
* ``missing_scope`` on the public-only listing -> :class:`ConnectorScopeError`
  (409 ``CONNECTOR-SCOPE``); on the mixed listing it is the #1778 retry trigger.
* HTTP 429 / ``ratelimited`` -> :class:`SlackRateLimited` (the route surfaces a
  429 with ``Retry-After`` passthrough).
* any other transport / API failure -> :class:`ExternalServiceError` (502).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from utils.exceptions import ConnectorScopeError, ExternalServiceError
from utils.logger import get_logger

logger = get_logger(__name__)

# conversations.list is Slack rate-limit Tier 2 (~20 req/min per token); the
# route's short Redis cache absorbs repeated dialog opens.
SLACK_CONVERSATIONS_LIST_URL = "https://slack.com/api/conversations.list"

# ``conversations.list`` needs ``channels:read`` for public channels and
# ``groups:read`` for the private channels the bot is a member of (both in the
# default install set since #1778; #1391 shipped public-only). Slack does not
# widen an existing grant, so a pre-#1778 install answers the mixed request
# with ``missing_scope`` — ``fetch_slack_channels`` then retries public-only
# once and reports the scope the reconnect would add. DMs / group DMs stay out.
_SLACK_PAGE_LIMIT = 200
SLACK_CHANNEL_TYPES_ALL = "public_channel,private_channel"
SLACK_CHANNEL_TYPES_PUBLIC = "public_channel"
PUBLIC_CHANNELS_SCOPE = "channels:read"
PRIVATE_CHANNELS_SCOPE = "groups:read"


@dataclass(frozen=True)
class SlackChannel:
    """A single channel, minimized to the picker's needs."""

    id: str
    name: str
    is_private: bool
    # #1451: Slack does not deliver ``message`` events for channels the bot has
    # not joined, so selecting one ingests nothing and reports nothing.
    # ``conversations.list`` carries this for public channels — it was simply
    # being dropped here; the parser still treats an absent key defensively
    # (absent → not a member, the reading that cannot promise delivery we
    # cannot verify). Deliberately has NO default here: a cache entry written
    # before this field existed must raise on rehydrate so the route refetches,
    # rather than silently defaulting joined channels to "not a member".
    is_member: bool


@dataclass(frozen=True)
class SlackChannelsPage:
    """One page of channels plus Slack's opaque forward cursor (``None`` = last)."""

    channels: list[SlackChannel]
    next_cursor: str | None
    # #1778: False when the mixed listing was refused for lack of ``groups:read``
    # and this page came from the public-only retry — the route tells the
    # picker which scope a reconnect would add. Deliberately has NO default
    # (the #1451 pattern): a page cached before this field existed is
    # public-only by construction and must raise on rehydrate so a reconnect
    # that just granted the scope is not served the stale listing.
    private_listing: bool


class SlackRateLimited(Exception):
    """Slack signalled Tier-2 rate limiting.

    Carries the ``Retry-After`` seconds Slack returned (``None`` when the limit
    arrived as a body ``error`` without a header) so the route can pass it
    through on the 429 response.
    """

    def __init__(self, retry_after: int | None) -> None:
        super().__init__("Slack rate limit reached")
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> int | None:
    """Parse Slack's ``Retry-After`` header (integer seconds) defensively."""
    if not value:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


async def fetch_slack_channels(
    *,
    bot_token: str,
    cursor: str | None = None,
) -> SlackChannelsPage:
    """Fetch one page of channels (public + private) from ``conversations.list``.

    Asks for both channel types first. When Slack refuses that with
    ``missing_scope`` — an install granted before ``groups:read`` was
    requested — it retries once with public channels only, so an older
    install keeps the listing it had, and marks the page
    ``private_listing=False``. If even the public-only request lacks its
    scope the error is raised as before.

    Args:
        bot_token: The connector's decrypted Slack bot token. Sent only as the
            outbound ``Authorization: Bearer`` header; never logged or returned.
        cursor: Slack's opaque pagination cursor from a prior page, or ``None``
            for the first page.

    Returns:
        A :class:`SlackChannelsPage` with minimized channels, the next cursor
        (``None`` when Slack reports no further pages) and whether private
        channels were included.

    Raises:
        ConnectorScopeError: Slack returned ``missing_scope`` for the
            public-only listing too (409).
        SlackRateLimited: Slack rate-limited the token (HTTP 429 or
            ``ratelimited`` body error).
        ExternalServiceError: any other transport or Slack API failure (502).
    """
    # Explicit connect + read timeouts so a stalled Slack endpoint cannot block
    # the async worker indefinitely (mirrors connectors_slack._exchange_slack_code).
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            return await _conversations_list(
                client, bot_token=bot_token, cursor=cursor, types=SLACK_CHANNEL_TYPES_ALL
            )
        except _MissingScope as exc:
            # Pre-#1778 grant: one public-only retry, never a third request —
            # and none at all when Slack says ``channels:read`` itself is
            # missing, since the retry could only fail the same way.
            if PUBLIC_CHANNELS_SCOPE in exc.needed:
                raise ConnectorScopeError() from None
            logger.info("slack_conversations_list_private_scope_missing")
        try:
            return await _conversations_list(
                client, bot_token=bot_token, cursor=cursor, types=SLACK_CHANNEL_TYPES_PUBLIC
            )
        except _MissingScope:
            raise ConnectorScopeError() from None


class _MissingScope(Exception):
    """Slack answered ``missing_scope``; ``needed`` is the scope set it named.

    Internal to this module: ``fetch_slack_channels`` turns it into the
    public-only retry or the 409 ``ConnectorScopeError`` the route expects.
    """

    def __init__(self, needed: frozenset[str]) -> None:
        super().__init__("Slack bot token lacks a required scope")
        self.needed = needed


def _parse_needed(value: object) -> frozenset[str]:
    """Scopes from Slack's comma-separated ``needed`` field (empty when absent)."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(s.strip() for s in value.split(",") if s.strip())


async def _conversations_list(
    client: httpx.AsyncClient,
    *,
    bot_token: str,
    cursor: str | None,
    types: str,
) -> SlackChannelsPage:
    """One ``conversations.list`` request for ``types``; see ``fetch_slack_channels``."""
    params: dict[str, str | int] = {
        "limit": _SLACK_PAGE_LIMIT,
        "types": types,
        "exclude_archived": "true",
    }
    if cursor:
        params["cursor"] = cursor
    headers = {"Authorization": f"Bearer {bot_token}"}

    try:
        resp = await client.get(SLACK_CONVERSATIONS_LIST_URL, params=params, headers=headers)
        if resp.status_code == 429:
            raise SlackRateLimited(_parse_retry_after(resp.headers.get("Retry-After")))
        resp.raise_for_status()
        data = resp.json()
    except SlackRateLimited:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        # Transport error, non-2xx status, or unparseable body. The exception
        # text can carry the request URL; log only the type so no token leaks.
        logger.warning("slack_conversations_list_failed", error_type=type(exc).__name__)
        raise ExternalServiceError("Slack", "Failed to list channels") from exc

    if not data.get("ok"):
        error = str(data.get("error") or "unknown")
        if error == "missing_scope":
            raise _MissingScope(_parse_needed(data.get("needed")))
        if error in ("ratelimited", "rate_limited"):
            raise SlackRateLimited(None)
        # A concrete Slack error code (e.g. invalid_auth) — safe, non-secret —
        # surfaces as a structured 502, never a raw 5xx.
        logger.warning("slack_conversations_list_error", slack_error=error)
        raise ExternalServiceError("Slack", f"conversations.list: {error}")

    channels = [
        SlackChannel(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            is_private=bool(item.get("is_private", False)),
            is_member=bool(item.get("is_member", False)),
        )
        for item in (data.get("channels") or [])
        if item.get("id")
    ]
    # Slack returns "" for next_cursor on the last page — normalize to None.
    next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or None

    logger.info(
        "slack_conversations_list_fetched",
        channel_count=len(channels),
        has_cursor=cursor is not None,
        has_next=next_cursor is not None,
        types=types,
    )
    return SlackChannelsPage(
        channels=channels,
        next_cursor=next_cursor,
        private_listing=types == SLACK_CHANNEL_TYPES_ALL,
    )
