"""Admin endpoints for service notices over MCP (Issue #2762).

Routes under ``/api/v1/admin/mcp-notices``, behind ``require_admin``
(``AdminUser``): a system admin in ``SYSTEM_ADMIN_MODE=session``, an operator
console in ``service`` mode, nobody in ``off`` mode (the routes then answer
404 and drop out of the OpenAPI document).

- ``POST``: write a notice. The text passes the notice sanitiser (one line
  of plain text, no links, markup or hidden characters, 1-300 characters);
  ``expires_at`` is required and timezone-aware, ``starts_at`` defaults to
  now. Caps: 3 live or scheduled, 30 days long, at most 90 days ahead, 5
  created in any 24 hours (withdrawn ones count).
- ``GET``: the notices, newest first, each with an aggregate
  ``delivered_count`` (no recipient is ever listed), and ``delivering``
  (``MCP_NOTICES_ENABLED``).
- ``POST /{notice_id}/withdraw``: end a notice now; idempotent. Claims that
  start after it commits skip the notice; one already running may still
  deliver it once.

The text cannot be edited and there is no delete: a correction is withdraw +
create, so everyone who saw a notice saw the same text. Every create and
withdraw writes an ``audit_logs`` row in the same transaction (the text's
sha256, never the text) and a WARNING log line with the actor, the id and the
character count, never the text. The routes work while delivery is off, so a
notice can be prepared first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.mcp_notice_service import (
    NoticeActor,
    NoticeListState,
    NoticeView,
    create_notice,
    list_notices,
    mcp_notices_enabled,
    withdraw_notice,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/mcp-notices", tags=["admin-mcp-notices"])


# ============================================================================
# Schemas
# ============================================================================


class McpNoticeCreateRequest(BaseModel):
    """A new notice.

    Attributes:
        text: One line of plain text, 1-300 characters (at most 900 UTF-8
            bytes) after normalisation: no links (schemes, ``www.``, host
            names, IP addresses), no ``< > [ ] ` " \\ { } @``, no control,
            invisible or formatting characters.
        expires_at: When delivery stops (timezone-aware; at most 30 days
            after ``starts_at``).
        starts_at: When delivery starts (timezone-aware; default now; at most
            90 days ahead, not in the past).
    """

    model_config = ConfigDict(extra="forbid")

    text: StrictStr
    expires_at: AwareDatetime
    starts_at: AwareDatetime | None = None


class McpNoticeResponse(TZAwareBaseModel):
    """A notice. ``state``: ``scheduled``, ``active`` or ``ended`` (expired or withdrawn)."""

    id: UUID
    text: str
    starts_at: datetime
    expires_at: datetime
    withdrawn_at: datetime | None = None
    created_at: datetime
    state: Literal["scheduled", "active", "ended"]
    delivered_count: int


class McpNoticeListResponse(BaseModel):
    """The notices and whether the deployment delivers them (``MCP_NOTICES_ENABLED``)."""

    notices: list[McpNoticeResponse]
    delivering: bool


def _response(view: NoticeView) -> McpNoticeResponse:
    return McpNoticeResponse(
        id=view.id,
        text=view.text,
        starts_at=view.starts_at,
        expires_at=view.expires_at,
        withdrawn_at=view.withdrawn_at,
        created_at=view.created_at,
        state=view.state,
        delivered_count=view.delivered_count,
    )


def _actor(admin: dict, request: Request) -> NoticeActor:
    """The admin's email and user_id (``operator:<address>`` for an operator)."""
    user_id = str(admin.get("user_id") or admin.get("sub") or "")
    return NoticeActor(
        email=str(admin.get("email") or user_id),
        user_id=user_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


# ============================================================================
# Routes
# ============================================================================


@router.post("", response_model=McpNoticeResponse, status_code=status.HTTP_201_CREATED)
async def create_mcp_notice(
    payload: McpNoticeCreateRequest,
    request: Request,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> McpNoticeResponse:
    """Write a service notice.

    Raises:
        McpNoticeTextRejectedError: 422 ``MCPNOTICE-001`` (``details.reason``).
        McpNoticeWindowInvalidError: 422 ``MCPNOTICE-002`` (``details.reason``).
        McpNoticeActiveLimitError: 409 ``MCPNOTICE-003``.
        McpNoticeDailyLimitError: 409 ``MCPNOTICE-007``.
    """
    actor = _actor(admin, request)
    view = await create_notice(
        db,
        raw_text=payload.text,
        expires_at=payload.expires_at,
        starts_at=payload.starts_at,
        actor=actor,
    )
    await db.commit()
    logger.warning(
        "mcp_notice_created",
        actor=actor.email,
        notice_id=str(view.id),
        text_chars=len(view.text),
    )
    return _response(view)


@router.get("", response_model=McpNoticeListResponse)
async def list_mcp_notices(
    admin: AdminUser,
    state: NoticeListState = Query("all", description="active, scheduled, ended or all"),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> McpNoticeListResponse:
    """The notices, newest first, with an aggregate ``delivered_count`` each."""
    views = await list_notices(db, state=state, limit=limit)
    return McpNoticeListResponse(
        notices=[_response(view) for view in views],
        delivering=mcp_notices_enabled(),
    )


@router.post("/{notice_id}/withdraw", response_model=McpNoticeResponse)
async def withdraw_mcp_notice(
    notice_id: UUID,
    request: Request,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> McpNoticeResponse:
    """End a notice now (idempotent: an ended notice is returned unchanged).

    Raises:
        McpNoticeNotFoundError: 404 ``MCPNOTICE-004``.
    """
    actor = _actor(admin, request)
    view, changed = await withdraw_notice(db, notice_id=notice_id, actor=actor)
    await db.commit()
    if changed:
        logger.warning(
            "mcp_notice_withdrawn",
            actor=actor.email,
            notice_id=str(view.id),
            text_chars=len(view.text),
        )
    return _response(view)
