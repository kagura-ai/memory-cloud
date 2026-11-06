"""Payload-free summary of a resource's ingestion events (#2679).

``GET /api/v1/observability/ingestion-events`` publishes, per resource, one
item per ``resource_events`` row: its id, receipt time, operation, the opaque
``doc_key`` of the document and whether the indexer has processed it. Never
the payload, the document id, the version, the idempotency key or the event
metadata.

Cursor: an ascending keyset on ``resource_events.id`` (opaque base64url JSON
``{"v": 1, "i": id}``), **time-sealed**: only rows whose ``created_at`` (the
inserting transaction's start time) is at least ``seal_seconds`` old are
published. Ids are taken at INSERT and commits land later, so a row with a
smaller id can become visible after a larger one; an ingest transaction
shorter than the seal cannot be skipped by a reader that only reads sealed
rows. The ingest routes write one short request transaction, so the default
120 s is generous. A client that wants a full re-sync omits the cursor.

Retention: ``resource_events`` rows live as long as their resource. A cursor
below the resource's oldest remaining event (while it has events) is answered
410 ``cursor_expired`` with the oldest usable cursor — reserved for a future
purge, and what a cursor of an earlier resource under a reused slug gets.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.resource import ResourceEvent
from services.observability_scope import ScopedResource
from services.public_mcp_usage_events import doc_key
from services.resource_indexer import load_indexer_state_row, recorded_failed_events
from utils.datetime import utcnow
from utils.exceptions import CursorExpiredError, CursorInvalidError

INGESTION_PAGE_MAX = 500
_MAX_CURSOR_CHARS = 100
_MAX_BIGINT = 2**63 - 1


def ingestion_doc_key(resource_pk: UUID, doc_id: str) -> str:
    """The resource-scoped opaque document key, the usage events' derivation (#2678).

    Ingestion items and public MCP usage events join on ``doc_key``.
    """
    return doc_key(resource_pk, doc_id, get_settings().audit_hmac_key)


def encode_ingestion_cursor(event_id: int) -> str:
    """The opaque cursor after ``event_id`` (base64url JSON, no padding)."""
    raw = json.dumps({"v": 1, "i": event_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_ingestion_cursor(value: str) -> int:
    """The event id a cursor points after; anything else is ``CursorInvalidError``."""
    if not value or len(value) > _MAX_CURSOR_CHARS:
        raise CursorInvalidError()
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise CursorInvalidError() from None
    if not isinstance(data, dict) or set(data) != {"v", "i"} or data["v"] != 1:
        raise CursorInvalidError()
    event_id = data["i"]
    if type(event_id) is not int or not 0 <= event_id <= _MAX_BIGINT:
        raise CursorInvalidError()
    return event_id


@dataclass(frozen=True)
class IngestionPage:
    """One page of sealed ingestion items and its coverage."""

    events: list[dict[str, Any]]
    next_cursor: str
    has_more: bool
    sealed_before: datetime
    seal_seconds: int
    oldest_event_id: int | None


async def read_ingestion_page(
    db: AsyncSession,
    resource: ScopedResource,
    *,
    cursor: str | None,
    limit: int,
    now: datetime | None = None,
) -> IngestionPage:
    """Read the next page of sealed ingestion events of one resource.

    Args:
        db: Session (nothing is written).
        resource: The resolved, in-scope resource.
        cursor: ``next_cursor`` of the previous page; ``None`` = from the start.
        limit: Items per page, 1..500.
        now: Clock override (naive UTC) for tests.

    Raises:
        CursorInvalidError: Not a cursor this endpoint issued (400).
        CursorExpiredError: The cursor is below the resource's oldest event (410).
    """
    if not 1 <= limit <= INGESTION_PAGE_MAX:
        raise ValueError(f"limit must be between 1 and {INGESTION_PAGE_MAX}")
    after = 0 if cursor is None else decode_ingestion_cursor(cursor)
    seal_seconds = get_settings().observability_ingestion_seal_seconds
    sealed_before = (now or utcnow()) - timedelta(seconds=seal_seconds)
    e = ResourceEvent

    oldest = (
        await db.execute(select(func.min(e.id)).where(e.resource_pk == resource.resource_pk))
    ).scalar_one_or_none()
    # A cursor after ``k`` has delivered every row up to ``k``; rows between
    # ``k`` and the oldest remaining one may be gone. ``oldest - 1`` (the
    # cursor the 410 hands out) misses nothing; 0 is the start (what an empty
    # first page hands back).
    if cursor is not None and oldest is not None and 0 < after < oldest - 1:
        raise CursorExpiredError(oldest_cursor=encode_ingestion_cursor(oldest - 1))

    rows = (
        await db.execute(
            select(e.id, e.op, e.doc_id, e.created_at)
            .where(
                e.resource_pk == resource.resource_pk,
                e.id > after,
                e.created_at <= sealed_before,
            )
            .order_by(e.id)
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    state = (
        await load_indexer_state_row(
            db, resource.context_id, resource.resource_pk, resource.resource_id
        )
        if resource.context_id is not None
        else None
    )
    failed_ids = (
        {entry["event_id"] for entry in recorded_failed_events(state.metrics)}
        if state is not None
        else set()
    )

    def processing(event_id: int) -> dict[str, Any]:
        if state is None:
            return {"status": "unknown", "failed": False}
        status = "processed" if state.last_offset >= event_id else "pending"
        return {"status": status, "failed": event_id in failed_ids}

    events = [
        {
            "event_id": str(row.id),
            "received_at": row.created_at,
            "op": row.op,
            "doc_key": ingestion_doc_key(resource.resource_pk, row.doc_id),
            "processing": processing(row.id),
        }
        for row in rows
    ]
    next_after = rows[-1].id if rows else after
    return IngestionPage(
        events=events,
        next_cursor=encode_ingestion_cursor(next_after),
        has_more=has_more,
        sealed_before=sealed_before,
        seal_seconds=seal_seconds,
        oldest_event_id=oldest,
    )
