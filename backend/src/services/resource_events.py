"""Read-path service for resource event browsing (Issue #316).

Cursor-paginated, workspace-scoped query over ``resource_events`` powering
the Resource Detail "Data" tab. This is a developer debug tool: fixed
filters only (``op`` / ``doc_id`` / ``version`` / ``since``), no JSONB query
DSL (security + perf sink, per the issue's design review).

Tenancy invariant: every query resolves ``(workspace_id, resource_id)`` to
the authoritative ``resources.id`` UUID via :func:`resolve_resource_pk` and
filters ``resource_events.resource_pk`` by it. A missing Resource row
(cross-workspace probe / pre-a97 orphan) returns an empty page, never a
slug-only fallback (CWE-639 / OWASP A01) — same fail-safe posture as
``get_latest_schema``.

Pagination contract:

- Keyset on the BigInt append-only PK ``resource_events.id`` DESC. The PK is
  the insertion order, so a compound ``created_at:id`` cursor is unnecessary
  (unlike ``analysis/query_service`` whose timestamps genuinely collide).
- ``cursor`` is an opaque token; the v1 encoding is the last ``id`` of the
  previous page as a string. Decoding lives at the route layer so a malformed
  cursor becomes a 400, not a 500.
- ``next_cursor`` is ``None`` when the page is the last one.
- ``limit`` is clamped server-side (default 20, max 100).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.resource import ResourceEvent
from services.resource_lookup import resolve_resource_pk
from utils.logger import get_logger

logger = get_logger(__name__)

# Server-enforced clamp. 20 matches the UI page size; 100 caps the worst-case
# transport frame (100 events × up to ~1 MB payload is bounded further by the
# payload-size guard applied at the route layer).
DEFAULT_EVENTS_PAGE_SIZE = 20
MAX_EVENTS_PAGE_SIZE = 100


def _clamp_limit(value: int | None) -> int:
    """Clamp a caller-supplied ``limit`` to ``[1, MAX_EVENTS_PAGE_SIZE]``."""
    if value is None or value <= 0:
        return DEFAULT_EVENTS_PAGE_SIZE
    return min(value, MAX_EVENTS_PAGE_SIZE)


def _normalize_since(since: datetime | None) -> datetime | None:
    """Coerce an aware ``since`` to naive UTC for comparison with the
    naive-UTC ``resource_events.created_at`` column (#489 wire convention)."""
    if since is None:
        return None
    if since.tzinfo is not None:
        return since.astimezone(UTC).replace(tzinfo=None)
    return since


async def list_resource_events(
    db: AsyncSession,
    workspace_id: UUID,
    resource_id: str,
    *,
    limit: int | None = None,
    cursor_id: int | None = None,
    op: str | None = None,
    doc_id: str | None = None,
    version: int | None = None,
    since: datetime | None = None,
) -> tuple[list[ResourceEvent], str | None]:
    """List ingest events for a workspace-scoped resource, newest first.

    Args:
        db: Async DB session.
        workspace_id: Owning workspace UUID (tenancy boundary).
        resource_id: External-facing resource slug.
        limit: Page size; clamped to ``[1, MAX_EVENTS_PAGE_SIZE]`` (default
            ``DEFAULT_EVENTS_PAGE_SIZE``).
        cursor_id: Keyset cursor — the last ``id`` of the previous page. Only
            events with ``id < cursor_id`` are returned. ``None`` for page 1.
        op: Filter by operation (``"upsert"`` / ``"delete"``). Exact match.
        doc_id: Filter by document id. Exact match.
        version: Filter by document version. Exact match.
        since: Lower bound (inclusive) on ``created_at``. Aware datetimes are
            normalized to naive UTC.

    Returns:
        ``(events, next_cursor)`` where ``events`` is at most ``limit`` rows
        ordered by ``id`` DESC, and ``next_cursor`` is the opaque token for the
        next page (the last row's ``id`` as a string) or ``None`` on the last
        page. Returns ``([], None)`` when the resource does not exist in this
        workspace (fail-safe — never a slug-only scan).
    """
    page_size = _clamp_limit(limit)

    resource_pk = await resolve_resource_pk(db, workspace_id, resource_id)
    if resource_pk is None:
        # Missing Resource row: cross-workspace probe or pre-a97 orphan. Fail
        # safe to an empty page rather than leak another workspace's events.
        logger.info(
            "list_resource_events_no_resource",
            workspace_id=str(workspace_id),
            resource_id=resource_id,
        )
        return [], None

    query = select(ResourceEvent).where(ResourceEvent.resource_pk == resource_pk)

    if op is not None:
        query = query.where(ResourceEvent.op == op)
    if doc_id is not None:
        query = query.where(ResourceEvent.doc_id == doc_id)
    if version is not None:
        query = query.where(ResourceEvent.version == version)
    normalized_since = _normalize_since(since)
    if normalized_since is not None:
        query = query.where(ResourceEvent.created_at >= normalized_since)
    if cursor_id is not None:
        query = query.where(ResourceEvent.id < cursor_id)

    # Fetch one extra row to detect whether a next page exists without a
    # second COUNT query.
    query = query.order_by(ResourceEvent.id.desc()).limit(page_size + 1)

    result = await db.execute(query)
    rows = list(result.scalars().all())

    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        next_cursor = str(rows[-1].id)

    return rows, next_cursor
