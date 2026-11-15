"""Admin reads of erasure requests (Issue #2834).

``GET /api/v1/admin/erasure-requests`` (a filtered page) and
``GET /api/v1/admin/erasure-requests/{request_id}`` (one request), behind the
system-admin gate in every ``SYSTEM_ADMIN_MODE`` (``AdminUser``: a system
admin in ``session`` mode, an operator principal in ``service`` mode; ``off``
is 404).

Each answer is built field by field from named columns:

- ``user_id`` and ``initiated_by`` are the stored values. A request that
  completed (or failed after its Postgres step) stores the subject's
  pseudonym, so the ``user_id`` filter finds such a request by its pseudonym.
- ``failure_kind`` is one token of a closed set (``models.erasure.FailureKind``).
- ``retry`` holds the five named keys of the summary's retry block, each
  checked for its shape.

Both routes only read: one session, no write, no audit row.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import ConfigDict
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.erasure import ErasureRequest, FailureKind, failure_kind_of
from utils.exceptions import NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/erasure-requests", tags=["admin"])

# The six values of ``models.erasure.VALID_STATUSES`` (a test pins the parity).
ErasureStatus = Literal["pending", "cooling_off", "in_progress", "complete", "failed", "cancelled"]

# ``retry.step`` / ``retry.error_class``: a step name or an exception class name.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

_RETRY_KEY = "retry"

# What the database takes: no stored text holds a NUL, and an OFFSET is a
# 64-bit integer. A value outside these is a 422, never a statement.
_NO_NUL = r"^[^\x00]*$"
_MAX_OFFSET = 2**63 - 1


class ErasureRetry(TZAwareBaseModel):
    """The retry block of a request that was rescheduled or re-queued."""

    model_config = ConfigDict(extra="forbid")

    attempts: int | None
    step: str | None
    error_class: str | None
    last_attempt_at: datetime | None
    embeddings_requeued: int | None


class ErasureRequestItem(TZAwareBaseModel):
    """One erasure request."""

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    user_id: str
    status: ErasureStatus
    is_self_service: bool
    reason_code: str
    initiated_by: str
    requested_at: datetime
    confirmed_at: datetime | None
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    retry: ErasureRetry | None
    failure_kind: FailureKind | None


class ErasureRequestListResponse(TZAwareBaseModel):
    """A page of erasure requests and the count of all that match."""

    model_config = ConfigDict(extra="forbid")

    items: list[ErasureRequestItem]
    total: int
    limit: int
    offset: int


def _count(value: Any) -> int | None:
    """A stored count: a non-negative integer (``bool`` is not one), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _identifier(value: Any) -> str | None:
    """A stored step or class name: an identifier of at most 64 characters, else None."""
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None:
        return value
    return None


def _timestamp(value: Any) -> datetime | None:
    """A stored ISO 8601 string as a datetime, else None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def erasure_retry_of(summary: Any) -> ErasureRetry | None:
    """The named keys of ``deleted_data_summary["retry"]``.

    Args:
        summary: The stored ``deleted_data_summary`` (a dict, or None).

    Returns:
        None when the summary has no ``retry`` dict; else a model of the five
        keys, each None when its stored value has another shape. No other key
        of the block or of the summary is read.
    """
    if not isinstance(summary, dict):
        return None
    block = summary.get(_RETRY_KEY)
    if not isinstance(block, dict):
        return None
    return ErasureRetry(
        attempts=_count(block.get("attempts")),
        step=_identifier(block.get("step")),
        error_class=_identifier(block.get("error_class")),
        last_attempt_at=_timestamp(block.get("last_attempt_at")),
        embeddings_requeued=_count(block.get("embeddings_requeued")),
    )


# The columns both reads select. ``failure_reason`` and ``deleted_data_summary``
# are inputs of ``failure_kind`` and ``retry`` only.
_COLUMNS = (
    ErasureRequest.id,
    ErasureRequest.user_id,
    ErasureRequest.status,
    ErasureRequest.is_self_service,
    ErasureRequest.reason_code,
    ErasureRequest.initiated_by,
    ErasureRequest.requested_at,
    ErasureRequest.confirmed_at,
    ErasureRequest.scheduled_for,
    ErasureRequest.started_at,
    ErasureRequest.completed_at,
    ErasureRequest.cancelled_at,
    ErasureRequest.failure_reason,
    ErasureRequest.deleted_data_summary,
)


def _item(row: Any) -> ErasureRequestItem:
    """Build one item from a row of ``_COLUMNS``."""
    return ErasureRequestItem(
        request_id=row.id,
        user_id=row.user_id,
        status=row.status,
        is_self_service=row.is_self_service,
        reason_code=row.reason_code,
        initiated_by=row.initiated_by,
        requested_at=row.requested_at,
        confirmed_at=row.confirmed_at,
        scheduled_for=row.scheduled_for,
        started_at=row.started_at,
        completed_at=row.completed_at,
        cancelled_at=row.cancelled_at,
        retry=erasure_retry_of(row.deleted_data_summary),
        failure_kind=failure_kind_of(row.status, row.failure_reason),
    )


@router.get(
    "",
    response_model=ErasureRequestListResponse,
    summary="List erasure requests",
)
async def list_erasure_requests(
    admin: AdminUser,
    status: ErasureStatus | None = Query(default=None, description="One of the six statuses."),
    user_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=255,
        pattern=_NO_NUL,
        description=(
            "Exact match on the stored `user_id`: the subject's id, or its pseudonym "
            "once the request completed."
        ),
    ),
    is_self_service: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=_MAX_OFFSET),
    db: AsyncSession = Depends(get_db),
) -> ErasureRequestListResponse:
    """A page of erasure requests, newest request first (then by id).

    The filters combine with AND; `total` counts the requests that match them.

    Each item:

    - `user_id`: as stored. The subject's id until the erasure completes, its
      pseudonym afterwards (also on a request that failed after its Postgres
      step).
    - `initiated_by`: as stored. The admin's id or `operator:<address>` for an
      admin request; the subject (pseudonymized with it) for a self-service one.
    - `retry`: `attempts`, `step`, `error_class`, `last_attempt_at` and
      `embeddings_requeued` of a request that was rescheduled or re-queued,
      else null.
    - `failure_kind`: for a `failed` request one of `after_postgres_commit`,
      `billing_contract_active`, `workspace_transfer_required`,
      `vector_store_unavailable`, `lock_conflict`, `other`; null otherwise.
    """
    conditions: list[ColumnElement[bool]] = []
    if status is not None:
        conditions.append(ErasureRequest.status == status)
    if user_id is not None:
        conditions.append(ErasureRequest.user_id == user_id)
    if is_self_service is not None:
        conditions.append(ErasureRequest.is_self_service.is_(is_self_service))

    total = await db.scalar(select(func.count()).select_from(ErasureRequest).where(*conditions))
    rows = (
        await db.execute(
            select(*_COLUMNS)
            .where(*conditions)
            .order_by(ErasureRequest.requested_at.desc(), ErasureRequest.id.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [_item(row) for row in rows]

    logger.info(
        "erasure_requests_listed",
        admin_user=admin["user_id"],
        count=len(items),
        total=total or 0,
        limit=limit,
        offset=offset,
        status=status,
        is_self_service=is_self_service,
        user_id_filter=user_id is not None,
    )
    return ErasureRequestListResponse(items=items, total=total or 0, limit=limit, offset=offset)


@router.get(
    "/{request_id}",
    response_model=ErasureRequestItem,
    summary="Read one erasure request",
    responses={404: {"description": "No erasure request has this id."}},
)
async def get_erasure_request(
    request_id: uuid.UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ErasureRequestItem:
    """One erasure request, with the fields of a list item."""
    row = (await db.execute(select(*_COLUMNS).where(ErasureRequest.id == request_id))).one_or_none()
    if row is None:
        raise NotFoundException("Erasure request")

    logger.info("erasure_request_read", admin_user=admin["user_id"], request_id=str(request_id))
    return _item(row)
