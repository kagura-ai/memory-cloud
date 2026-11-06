"""Public MCP usage events, schema v1: rows, doc keys and the export cursor (#2678).

Three parts:

* **Row builder.** ``build_usage_event_row`` turns a neutral
  ``UsageEventInput`` (what one public ``tools/call`` did) into the column
  values of one ``public_mcp_usage_events`` row. Everything the client typed
  is normalised away: ``tool`` is one of the five public names or ``other``,
  ``error_code`` matches ``^[a-z_]{1,64}$`` or is ``other``, the protocol
  version is kept only when it has the date shape of an MCP version. Raw
  document ids become ``doc_key``s (HMAC with the deployment's audit key,
  scoped to the resource identity), deduplicated, capped at 25. The raw
  session id and the client name are used only when the session-details
  switch is on, and then only as a daily HMAC and a reduced family string.

* **Export models.** ``PublicMcpUsageEventV1`` / ``PublicMcpUsageGapV1`` /
  ``PublicMcpUsagePageV1``: the wire contract a self-hosted dashboard reads.

* **Cursor.** ``read_usage_events_page`` publishes rows in ``(txid, id)``
  order and only those whose inserting transaction is below the snapshot's
  ``xmin`` horizon, read in ONE statement so the horizon and the visible
  rows come from the same snapshot. Every transaction below the horizon has
  finished, so that set is final and a keyset over it cannot skip a row that
  commits later (a later commit has ``txid >= horizon``). A cursor below the
  retention purge mark is refused as expired instead of skipping silently.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any, Literal, NamedTuple
from uuid import UUID

from pydantic import ConfigDict, Field
from sqlalchemy import func, literal_column, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from models.api_base import TZAwareBaseModel
from models.public_mcp_usage import (
    PMU_TOOLS,
    PublicMcpUsageEvent,
    PublicMcpUsageRetention,
)
from utils.datetime import to_naive_utc, utcnow
from utils.exceptions import CursorExpiredError, CursorInvalidError
from utils.hashing import hmac_sha256_hex

SCHEMA_VERSION = 1

# Distinct documents kept per event (= the public explore limit; recall k <= 20).
PUBLIC_TELEMETRY_MAX_DOCUMENTS = 25
# Hex characters kept of an HMAC (128 bits).
KEY_HEX_CHARS = 32
CLIENT_FAMILY_MAX_CHARS = 32
USAGE_PAGE_MAX = 1000

_DOC_KEY_DOMAIN = "public-mcp-doc"
_SESSION_KEY_DOMAIN = "public-mcp-session"
_ERROR_CODE_RE = re.compile(r"^[a-z_]{1,64}$")
_PROTOCOL_VERSION_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CLIENT_FAMILY_DROP_RE = re.compile(r"[^a-z0-9._-]")
_MAX_DOC_ID_CHARS = 255
_MAX_CURSOR_CHARS = 200
_MAX_BIGINT = 2**63 - 1

# The transaction-id horizon of the statement's snapshot: every transaction
# with a lower id has finished.
_HORIZON_SQL = "pg_snapshot_xmin(pg_current_snapshot())::text::bigint"

_PUBLIC_TOOL_NAMES = frozenset(t for t in PMU_TOOLS if t != "other")

ToolName = Literal["recall", "reference", "explore", "list_tags", "get_context_info", "other"]
OutcomeName = Literal[
    "success", "degraded", "denied", "rate_limited", "invalid_request", "error", "cancelled"
]
EraName = Literal["session", "stateless"]
GapReason = Literal["queue_full", "write_failed", "shutdown"]


# ============================================================================
# Keys and normalisation
# ============================================================================


def doc_key(resource_pk: UUID, resource_doc_id: str, key: str) -> str:
    """The opaque, resource-scoped key of one document (32 hex).

    Args:
        resource_pk: The resource identity (``resources.id``), not the slug.
        resource_doc_id: The resource's own document id.
        key: The deployment's audit HMAC key.

    Returns:
        32 lowercase hex characters.
    """
    message = f"{_DOC_KEY_DOMAIN}\n{resource_pk}\n{resource_doc_id}"
    return hmac_sha256_hex(message, key)[:KEY_HEX_CHARS]


def session_key(endpoint_id: UUID, session_id: str, day: date, key: str) -> str:
    """A daily key for one session on one endpoint (32 hex); rotates at UTC midnight."""
    message = f"{_SESSION_KEY_DOMAIN}\n{day.isoformat()}\n{endpoint_id}\n{session_id}"
    return hmac_sha256_hex(message, key)[:KEY_HEX_CHARS]


def normalise_tool(name: str | None) -> str:
    """One of the five public tool names, else ``other`` (never the typed string)."""
    return name if name in _PUBLIC_TOOL_NAMES else "other"


def normalise_error_code(code: str | None) -> str | None:
    """``code`` when it is ``[a-z_]{1,64}``, ``other`` otherwise, ``None`` for none."""
    if code is None:
        return None
    return code if _ERROR_CODE_RE.fullmatch(code) else "other"


def normalise_protocol_version(value: str | None) -> str | None:
    """The version when it has the ``YYYY-MM-DD`` shape of an MCP version, else ``None``."""
    if value is None or not _PROTOCOL_VERSION_RE.fullmatch(value):
        return None
    return value


def normalise_client_family(name: str | None) -> str | None:
    """A client's self-declared name reduced to ``[a-z0-9._-]{1,32}`` (unverified)."""
    if not name:
        return None
    reduced = _CLIENT_FAMILY_DROP_RE.sub("", name.lower())[:CLIENT_FAMILY_MAX_CHARS]
    return reduced or None


# ============================================================================
# Row builder
# ============================================================================


@dataclass(frozen=True)
class UsageEventInput:
    """What one public ``tools/call`` did, as the hook hands it to the writer.

    ``doc_ids`` are the raw document ids in rank order (not deduplicated);
    ``session_id`` and ``client_name`` are transient and never stored as given.
    """

    endpoint_id: UUID
    workspace_id: UUID
    context_id: UUID
    resource_pk: UUID
    resource_id: str
    tool: str | None
    outcome: str
    era: str
    started_at: datetime
    duration_ms: int
    error_code: str | None = None
    protocol_version: str | None = None
    result_count: int | None = None
    continuation: bool = False
    doc_ids: tuple[str, ...] = ()
    session_id: str | None = None
    client_name: str | None = None


@dataclass(frozen=True)
class BuiltUsageEvent:
    """One row's column values plus the doc keys it introduced."""

    row: dict[str, Any]
    # (doc_key, resource_doc_id) for every kept document.
    doc_keys: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    resource_pk: UUID | None = None


# Every row carries every column so a batch is one multi-row INSERT.
_ROW_KEYS: tuple[str, ...] = (
    "event_id",
    "occurred_at",
    "kind",
    "endpoint_id",
    "workspace_id",
    "context_id",
    "resource_pk",
    "resource_id",
    "tool",
    "outcome",
    "error_code",
    "era",
    "protocol_version",
    "duration_ms",
    "result_count",
    "continuation",
    "documents",
    "documents_truncated",
    "session_key",
    "client_family",
    "dropped_count",
    "gap_started_at",
    "gap_ended_at",
    "gap_reason",
)


def _blank_row() -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(_ROW_KEYS)
    row["event_id"] = uuid.uuid4()
    row["continuation"] = False
    row["documents_truncated"] = False
    return row


def _observe_documents(
    resource_pk: UUID, doc_ids: tuple[str, ...], key: str
) -> tuple[list[dict[str, Any]] | None, bool, tuple[tuple[str, str], ...]]:
    """Deduplicate ``doc_ids`` by doc key: chunks per document and best rank."""
    documents: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    truncated = False
    for rank, doc_id in enumerate(doc_ids, start=1):
        if not doc_id or len(doc_id) > _MAX_DOC_ID_CHARS:
            continue
        k = doc_key(resource_pk, doc_id, key)
        seen = documents.get(k)
        if seen is not None:
            seen["chunks"] += 1
            continue
        if len(documents) >= PUBLIC_TELEMETRY_MAX_DOCUMENTS:
            truncated = True
            continue
        documents[k] = {"doc_key": k, "chunks": 1, "rank": rank}
        sources[k] = doc_id
    if not documents:
        return None, truncated, ()
    return list(documents.values()), truncated, tuple(sources.items())


def build_usage_event_row(
    event: UsageEventInput, *, key: str, session_details_enabled: bool
) -> BuiltUsageEvent:
    """The ``public_mcp_usage_events`` values for one public ``tools/call``.

    Args:
        event: The call, with server-resolved identity.
        key: The deployment's audit HMAC key.
        session_details_enabled: Whether the daily session key and the
            client family are recorded.

    Returns:
        The row and the (doc_key, resource_doc_id) pairs to remember.
    """
    outcome = event.outcome
    documents, truncated, doc_keys = _observe_documents(event.resource_pk, event.doc_ids, key)
    row = _blank_row()
    started = to_naive_utc(event.started_at)
    row.update(
        occurred_at=started,
        kind="tool_call",
        endpoint_id=event.endpoint_id,
        workspace_id=event.workspace_id,
        context_id=event.context_id,
        resource_pk=event.resource_pk,
        resource_id=event.resource_id,
        tool=normalise_tool(event.tool),
        outcome=outcome,
        error_code=(
            None if outcome in ("success", "degraded") else normalise_error_code(event.error_code)
        ),
        era=event.era,
        protocol_version=normalise_protocol_version(event.protocol_version),
        duration_ms=max(0, int(event.duration_ms)),
        result_count=event.result_count,
        continuation=bool(event.continuation),
        documents=documents,
        documents_truncated=truncated,
    )
    if session_details_enabled and event.era == "session":
        if event.session_id:
            row["session_key"] = session_key(
                event.endpoint_id, event.session_id, started.date(), key
            )
        row["client_family"] = normalise_client_family(event.client_name)
    return BuiltUsageEvent(row=row, doc_keys=doc_keys, resource_pk=event.resource_pk)


def build_gap_row(
    *, dropped_count: int, started_at: datetime, ended_at: datetime, reason: str
) -> dict[str, Any]:
    """A ``gap`` row: ``dropped_count`` events were lost between the two times."""
    row = _blank_row()
    row.update(
        occurred_at=utcnow(),
        kind="gap",
        dropped_count=dropped_count,
        gap_started_at=to_naive_utc(started_at),
        gap_ended_at=to_naive_utc(ended_at),
        gap_reason=reason,
    )
    return row


# ============================================================================
# Export models (schema v1)
# ============================================================================


class _Strict(TZAwareBaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicMcpUsageDocumentV1(_Strict):
    """One observed document: its opaque key, how many results it had, best rank."""

    doc_key: str = Field(pattern=r"^[0-9a-f]{32}$")
    chunks: int = Field(ge=1)
    rank: int = Field(ge=1)


class PublicMcpUsageEventV1(_Strict):
    """One completed public ``tools/call``."""

    schema_version: Literal[1] = 1
    event_id: UUID
    kind: Literal["tool_call"] = "tool_call"
    occurred_at: datetime
    endpoint_id: UUID
    workspace_id: UUID
    context_id: UUID
    resource_pk: UUID
    resource_id: str = Field(max_length=255)
    tool: ToolName
    outcome: OutcomeName
    error_code: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")
    era: EraName
    protocol_version: str | None = Field(default=None, max_length=16)
    duration_ms: int = Field(ge=0)
    result_count: int | None = None
    continuation: bool = False
    documents: list[PublicMcpUsageDocumentV1] | None = Field(
        default=None, max_length=PUBLIC_TELEMETRY_MAX_DOCUMENTS
    )
    documents_truncated: bool = False
    session_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    client_family: str | None = Field(
        default=None, max_length=CLIENT_FAMILY_MAX_CHARS, pattern=r"^[a-z0-9._-]+$"
    )


class PublicMcpUsageGapV1(_Strict):
    """Events the server could not record; the stream says so instead of skipping."""

    schema_version: Literal[1] = 1
    event_id: UUID
    kind: Literal["gap"] = "gap"
    occurred_at: datetime
    dropped_count: int = Field(ge=1)
    gap_started_at: datetime
    gap_ended_at: datetime
    gap_reason: GapReason


UsageStreamItem = Annotated[
    PublicMcpUsageEventV1 | PublicMcpUsageGapV1, Field(discriminator="kind")
]


class PublicMcpUsageCoverageV1(_Strict):
    """What a page can and cannot vouch for."""

    gaps_in_page: int = Field(ge=0)
    dropped_in_page: int = Field(ge=0)
    # Rows written but not yet sealed (their transaction is at or above the horizon).
    unsealed_events: int = Field(ge=0)
    oldest_retained_at: datetime | None = None
    retention_days: int = Field(ge=1)
    writer_enabled: bool


class PublicMcpUsagePageV1(_Strict):
    """One page of the usage stream, in commit-safe ``(txid, id)`` order."""

    schema_version: Literal[1] = 1
    deployment_id: str
    as_of: datetime
    events: list[UsageStreamItem]
    next_cursor: str
    has_more: bool
    coverage: PublicMcpUsageCoverageV1
    # The client family is what the client said about itself.
    client_family_verified: Literal[False] = False


# ============================================================================
# Cursor
# ============================================================================


class UsageCursor(NamedTuple):
    """A position in the stream: after the row ``(txid, id)``."""

    txid: int
    id: int


START_CURSOR = UsageCursor(0, 0)


def encode_cursor(cursor: UsageCursor) -> str:
    """The opaque cursor string (base64url JSON, no padding)."""
    raw = json.dumps({"v": 1, "t": cursor.txid, "i": cursor.id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _cursor_int(value: object) -> int:
    if type(value) is not int or not (0 <= value <= _MAX_BIGINT):
        raise CursorInvalidError()
    return value


def decode_cursor(value: str) -> UsageCursor:
    """Parse a cursor from ``encode_cursor``; anything else is ``CursorInvalidError``."""
    if not isinstance(value, str) or not value or len(value) > _MAX_CURSOR_CHARS:
        raise CursorInvalidError()
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise CursorInvalidError() from None
    if not isinstance(data, dict) or set(data) != {"v", "t", "i"} or data["v"] != 1:
        raise CursorInvalidError()
    return UsageCursor(_cursor_int(data["t"]), _cursor_int(data["i"]))


# ============================================================================
# Page reader
# ============================================================================


@dataclass(frozen=True)
class UsageEventsPage:
    """One page of sealed rows plus what the coverage block reports."""

    events: list[PublicMcpUsageEventV1 | PublicMcpUsageGapV1]
    next_cursor: str
    has_more: bool
    horizon: int
    unsealed_events: int
    oldest_retained_at: datetime | None


def _scope(workspace_id: UUID, resource_pk: UUID | None) -> list[Any]:
    """Rows of the workspace (and resource); gap rows belong to every scope."""
    e = PublicMcpUsageEvent
    conditions: list[Any] = [or_(e.kind == "gap", e.workspace_id == workspace_id)]
    if resource_pk is not None:
        conditions.append(or_(e.kind == "gap", e.resource_pk == resource_pk))
    return conditions


_GAP_FIELDS = frozenset(PublicMcpUsageGapV1.model_fields) - {"schema_version"}
_EVENT_FIELDS = frozenset(PublicMcpUsageEventV1.model_fields) - {"schema_version"}


def to_export(row: PublicMcpUsageEvent) -> PublicMcpUsageEventV1 | PublicMcpUsageGapV1:
    """The wire form of one stored row (never ``id`` or ``txid``)."""
    if row.kind == "gap":
        return PublicMcpUsageGapV1.model_validate({f: getattr(row, f) for f in _GAP_FIELDS})
    return PublicMcpUsageEventV1.model_validate({f: getattr(row, f) for f in _EVENT_FIELDS})


async def read_usage_events_page(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    resource_pk: UUID | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> UsageEventsPage:
    """Read the next page of sealed usage rows after ``cursor``.

    Args:
        db: Session (READ COMMITTED; nothing is written).
        workspace_id: The workspace whose rows are read (gap rows always).
        resource_pk: Narrow to one resource identity (gap rows always).
        cursor: ``next_cursor`` of the previous page; ``None`` = oldest retained.
        limit: Rows per page, 1..``USAGE_PAGE_MAX``.

    Returns:
        The page. ``next_cursor`` is the last row when more rows follow;
        otherwise it is the sealed horizon (never below the input cursor).

    Raises:
        CursorInvalidError: ``cursor`` is not one this server issued.
        CursorExpiredError: rows after ``cursor`` may have been purged.
        ValueError: ``limit`` out of range.
    """
    if not 1 <= limit <= USAGE_PAGE_MAX:
        raise ValueError(f"limit must be between 1 and {USAGE_PAGE_MAX}")
    start = START_CURSOR if cursor is None else decode_cursor(cursor)
    e = PublicMcpUsageEvent
    scope = _scope(workspace_id, resource_pk)
    horizon = literal_column(_HORIZON_SQL)

    # Read before the page, so it is no later than the page's own horizon
    # (the horizon only moves forward): what an empty page vouches for.
    floor = int((await db.execute(select(horizon))).scalar_one())

    # ONE statement: the horizon and the visible rows share the snapshot.
    page_stmt = (
        select(e, horizon.label("horizon"))
        .where(
            e.txid < horizon,
            tuple_(e.txid, e.id) > tuple_(start.txid, start.id),
            *scope,
        )
        .order_by(e.txid, e.id)
        .limit(limit + 1)
    )
    rows = (await db.execute(page_stmt)).all()

    # Read after the page: a purge commits its deletes and its mark together,
    # so a purge the page could not see the rows of is visible here.
    r = PublicMcpUsageRetention
    stats_stmt = select(
        select(func.count())
        .select_from(e)
        .where(e.txid >= horizon, *scope)
        .scalar_subquery()
        .label("unsealed"),
        select(func.min(e.occurred_at)).where(*scope).scalar_subquery().label("oldest"),
        select(r.purged_through_txid).where(r.id == 1).scalar_subquery().label("mark_txid"),
        select(r.purged_through_id).where(r.id == 1).scalar_subquery().label("mark_id"),
    )
    stats = (await db.execute(stats_stmt)).one()
    mark = UsageCursor(int(stats.mark_txid or 0), int(stats.mark_id or 0))
    if cursor is not None and start < mark:
        raise CursorExpiredError(oldest_cursor=encode_cursor(mark))

    has_more = len(rows) > limit
    rows = rows[:limit]
    page_horizon = int(rows[0].horizon) if rows else floor
    if has_more:
        last = rows[-1][0]
        next_cursor = encode_cursor(UsageCursor(last.txid, last.id))
    else:
        # Every row of the scope below the horizon has been returned, and any
        # row still to come has a txid at or above it: the cursor moves up to
        # the horizon, so an idle reader keeps pace with the purge mark
        # instead of being refused as expired for rows it was never owed.
        sealed = UsageCursor(page_horizon - 1, _MAX_BIGINT)
        next_cursor = encode_cursor(max(start, sealed))
    return UsageEventsPage(
        events=[to_export(row[0]) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        horizon=page_horizon,
        unsealed_events=int(stats.unsealed or 0),
        oldest_retained_at=stats.oldest,
    )


def build_usage_page(
    page: UsageEventsPage,
    *,
    deployment_id: str,
    retention_days: int,
    writer_enabled: bool,
    as_of: datetime | None = None,
) -> PublicMcpUsagePageV1:
    """The schema v1 envelope around one page."""
    gaps = [item for item in page.events if isinstance(item, PublicMcpUsageGapV1)]
    return PublicMcpUsagePageV1(
        deployment_id=deployment_id,
        as_of=as_of or utcnow(),
        events=page.events,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        coverage=PublicMcpUsageCoverageV1(
            gaps_in_page=len(gaps),
            dropped_in_page=sum(g.dropped_count for g in gaps),
            unsealed_events=page.unsealed_events,
            oldest_retained_at=page.oldest_retained_at,
            retention_days=retention_days,
            writer_enabled=writer_enabled,
        ),
    )


# ============================================================================
# Owner health summary
# ============================================================================


@dataclass(frozen=True)
class UsageHealthCounts:
    """Event and gap counts of one window (owner health route)."""

    events: int
    outcomes: dict[str, int]
    gap_count: int
    gap_dropped: int
    last_gap_at: datetime | None


async def usage_health_counts(
    db: AsyncSession, *, endpoint_id: UUID | None, since: datetime
) -> UsageHealthCounts:
    """Events of one endpoint and the deployment's gap rows since ``since``.

    Gap rows carry no workspace (they say how much was lost, not whose), so
    every owner sees the deployment's gaps: a hidden gap would defeat them.
    """
    e = PublicMcpUsageEvent
    outcomes: dict[str, int] = {}
    if endpoint_id is not None:
        rows = await db.execute(
            select(e.outcome, func.count())
            .where(e.endpoint_id == endpoint_id, e.occurred_at >= since)
            .group_by(e.outcome)
        )
        outcomes = {str(outcome): int(n) for outcome, n in rows.all()}
    gaps = (
        await db.execute(
            select(
                func.count(), func.coalesce(func.sum(e.dropped_count), 0), func.max(e.occurred_at)
            ).where(e.kind == "gap", e.occurred_at >= since)
        )
    ).one()
    return UsageHealthCounts(
        events=sum(outcomes.values()),
        outcomes=outcomes,
        gap_count=int(gaps[0]),
        gap_dropped=int(gaps[1]),
        last_gap_at=gaps[2],
    )
