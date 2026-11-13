"""The signed-in user's opt-in for service notices over MCP (Issue #2762).

``/me/mcp-notices`` — session auth only (``SessionUser``), like the rest of
``/me``: an API key or an OAuth bearer token cannot opt a person in. The body
is JSON, so a cross-site form post cannot reach the PUT, and the session
cookie is ``SameSite=Lax``.

- ``GET`` — the current answer and whether the deployment offers notices.
- ``PUT`` — record a new answer (source ``settings``). Turning it ON needs
  ``MCP_NOTICES_ENABLED`` (409 ``MCPNOTICE-005``) and the current wording
  version (409 ``MCPNOTICE-006``; the page reloads); turning it OFF always
  works. An unchanged answer writes nothing. Limited per user (429).

The answer is kept in ``mcp_notice_consents``, apart from the announcement
email answers (#2761).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from db.base import get_db
from db.redis import increment_counter
from models.api_base import TZAwareBaseModel
from services.mcp_notice_service import (
    current_mcp_notice_consent,
    current_mcp_notice_wording_version,
    record_mcp_notice_consent,
)
from utils.exceptions import (
    McpNoticesNotOfferedError,
    McpNoticeWordingStaleError,
    RateLimitError,
    RedisError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/me/mcp-notices", tags=["mcp-notices"])

# Bounds the append-only history a script could grow.
_PREF_WINDOW_SECONDS = 60 * 60
_PREF_CHANGES_PER_USER = 20


class McpNoticesResponse(TZAwareBaseModel):
    """The signed-in user's service-notice setting.

    Attributes:
        offered: Whether the deployment offers notices (``MCP_NOTICES_ENABLED``).
            Turning the setting off works either way.
        mcp_notices: The current answer (false when never answered).
        answered: Whether the user ever answered.
        source: Where the current answer came from (``settings``), null when
            never answered.
        recorded_at: When the current answer was recorded.
        wording_version: The consent copy version to send back when turning it
            on; null while the deployment does not offer notices.
    """

    offered: bool
    mcp_notices: bool
    answered: bool
    source: str | None = None
    recorded_at: datetime | None = None
    wording_version: str | None = None


class McpNoticesUpdateResponse(McpNoticesResponse):
    """``recorded`` is false when the answer was already the current one."""

    recorded: bool


class McpNoticesUpdateRequest(BaseModel):
    """A new answer. ``wording_version`` is required to turn it on."""

    model_config = ConfigDict(extra="forbid")

    mcp_notices: StrictBool
    wording_version: str | None = Field(None, max_length=64)


async def _over_limit(user_id: str) -> bool:
    """Count one ``PUT``; True when over the hourly limit. Fails open.

    Every request counts, an unchanged answer included (as for the
    announcement-email setting), so the limit is on requests, not changes.
    """
    try:
        count = await increment_counter(f"mcp_notice_pref_user:{user_id}", ttl=_PREF_WINDOW_SECONDS)
    except RedisError as exc:
        logger.warning("mcp_notices_rate_limit_unavailable", error_type=type(exc).__name__)
        return False
    return count > _PREF_CHANGES_PER_USER


@router.get("", response_model=McpNoticesResponse)
async def get_mcp_notices(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> McpNoticesResponse:
    """The signed-in user's service-notice setting."""
    version = current_mcp_notice_wording_version()
    current = await current_mcp_notice_consent(db, user["user_id"])
    return McpNoticesResponse(
        offered=version is not None,
        mcp_notices=bool(current and current.value),
        answered=current is not None,
        source=current.source if current else None,
        recorded_at=current.recorded_at if current else None,
        wording_version=version,
    )


@router.put("", response_model=McpNoticesUpdateResponse)
async def update_mcp_notices(
    payload: McpNoticesUpdateRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> McpNoticesUpdateResponse:
    """Turn service notices in AI tools on or off for the signed-in user.

    Raises:
        McpNoticesNotOfferedError: 409 ``MCPNOTICE-005``, turning it on while
            the deployment does not offer notices.
        McpNoticeWordingStaleError: 409 ``MCPNOTICE-006``, turning it on with
            a wording version other than the current one.
        RateLimitError: 429, more than 20 requests in an hour (unchanged
            answers count too).
        NotFoundException: 404, the account is gone.
    """
    user_id = user["user_id"]
    value = payload.mcp_notices
    version = current_mcp_notice_wording_version()
    if value:
        if version is None:
            raise McpNoticesNotOfferedError()
        if payload.wording_version != version:
            raise McpNoticeWordingStaleError()

    if await _over_limit(user_id):
        raise RateLimitError(
            "Too many changes to this setting. Please try again later.",
            retry_after=_PREF_WINDOW_SECONDS,
        )

    result = await record_mcp_notice_consent(
        db,
        user_id=user_id,
        value=value,
        wording_version=version if value else None,
    )
    await db.commit()
    if result.recorded:
        logger.info("mcp_notices_setting_changed", user_id=user_id, value=value)
    state = result.state
    return McpNoticesUpdateResponse(
        offered=version is not None,
        mcp_notices=state.value,
        answered=True,
        source=state.source,
        recorded_at=state.recorded_at,
        wording_version=version,
        recorded=result.recorded,
    )
