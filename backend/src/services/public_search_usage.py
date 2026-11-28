"""REST public searches made with a bound key, in the public usage events (#3035).

A REST public search (``POST /api/v1/public/{context_id}/search``) made with
an API key bound to the context is recorded as one ``tool_call`` row of the
publication the public MCP endpoint serves for that context (route ``rest``,
caller ``bound_key``, tool ``recall``, era ``stateless``), through the same
per-process usage writer as public MCP calls. It is recorded only when
``PUBLIC_SEARCH_TELEMETRY_ENABLED`` and ``MCP_PUBLIC_TELEMETRY_ENABLED`` are
on, a writer runs in the process, the key's plan gives bound keys an
allowance, and the context has a serving publication. Anonymous REST searches
are never recorded, whatever the switches say.

The route creates one record per request and owns its ``emit()`` in a
``finally``: exactly one offer per recorded request, whatever the exit
(refusal by the key's own bucket, failure, cancellation, answer); a failure
after the answer was built wins over it. A refusal by a serving gate
(capacity lock, slot suspension) leaves no event, as on public MCP, where the
transport refuses those before any call record. When recording does not
apply the route uses ``NULL_RECORD``, whose methods do nothing.

Visitors: a server holding a bound key may send ``Kagura-Visitor: <value>``.
The header is read only for a live record, through ``VisitorHeaderSource``,
which touches nothing of the request but that header. Exactly one value of
``[A-Za-z0-9_-]{1,64}`` is turned at once into the daily
``visitor_key(endpoint, value, day)``; the value itself is never kept, logged
or stored. Anything else is ignored (``repeated`` / ``format``), never
refused. Only a served search carries a visitor to the writer.

Recording never changes an answer: the publication lookup runs in a
savepoint (an error there logs one warning and records nothing), and the
record itself never raises.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from mcp_server.public.publication import ServingEndpoint, load_context_publication
from models.public_mcp_usage import (
    PMU_CALLER_BOUND_KEY,
    PMU_REST_ERA,
    PMU_ROUTE_REST,
    PMU_SEARCH_TOOL,
)
from services.public_mcp_usage_events import UsageEventInput, visitor_key
from services.public_mcp_usage_writer import get_usage_writer
from utils.exceptions import (
    CapacityLockedError,
    QuotaExceededError,
    RateLimitError,
    TemporarilyUnavailableError,
    WorkspaceSlotSuspendedError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

VISITOR_HEADER = "kagura-visitor"
VISITOR_IGNORED_REASONS: tuple[str, ...] = ("repeated", "format")
_VISITOR_VALUE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A refusal by these, raised inside ``gate()``, leaves no event.
_SERVING_GATE_REFUSALS = (CapacityLockedError, WorkspaceSlotSuspendedError, QuotaExceededError)


def public_search_recording_enabled() -> bool:
    """Whether REST bound-key searches are recorded in this process now.

    Both switches on and a usage writer running here (a process whose
    lifespan started none has nothing to offer to).
    """
    settings = get_settings()
    return bool(
        settings.public_search_telemetry_enabled
        and settings.mcp_public_telemetry_enabled
        and get_usage_writer() is not None
    )


# ============================================================================
# The visitor header
# ============================================================================


@dataclass(frozen=True, slots=True, repr=False)
class VisitorHeaderSource:
    """The request, held only to read ``Kagura-Visitor`` when a record needs it.

    Touches nothing else of the request (no state, session, cookie or other
    header): the route still does not read the session (#2823).
    """

    _request: Request

    def values(self) -> list[str]:
        """Every ``Kagura-Visitor`` value the request carries."""
        return self._request.headers.getlist(VISITOR_HEADER)

    def __repr__(self) -> str:
        return "VisitorHeaderSource()"


def visitor_header_source(request: Request) -> VisitorHeaderSource:
    """FastAPI dependency: a lazy source of the visitor header (read nothing yet)."""
    return VisitorHeaderSource(request)


def resolve_visitor(
    values: Sequence[str], *, endpoint_id: UUID, day: date, key: str
) -> tuple[str | None, str | None]:
    """``(visitor_key, None)`` for one well-formed value, else ``(None, reason)``.

    No header is ``(None, None)``; two or more are ``repeated``; a value that
    is not 1-64 characters of ``[A-Za-z0-9_-]`` is ``format``.
    """
    if not values:
        return None, None
    if len(values) > 1:
        return None, "repeated"
    if _VISITOR_VALUE.fullmatch(values[0]) is None:
        return None, "format"
    return visitor_key(endpoint_id, values[0], day, key), None


# ============================================================================
# The record
# ============================================================================


class SearchRecord(Protocol):
    """What the route calls; ``BoundSearchRecord`` or ``NULL_RECORD``."""

    live: bool
    visitor_ignored: str | None

    @property
    def publication(self) -> ServingEndpoint | None: ...

    def resolve_visitor(self) -> None: ...

    def gate(self) -> Any: ...

    def served(self, *, degraded: bool, result_count: int, doc_ids: tuple[str, ...] | None) -> None:
        """Record the answer."""

    def failed(self, exc: BaseException) -> None:
        """Record the failure that left the route (wins over an answer)."""

    def emit(self) -> None:
        """Offer the event once; never raises."""


class _NullRecord:
    """Recording does not apply: every method does nothing."""

    __slots__ = ()
    live = False
    visitor_ignored: str | None = None

    @property
    def publication(self) -> ServingEndpoint | None:
        return None

    def resolve_visitor(self) -> None:
        return None

    @contextmanager
    def gate(self) -> Iterator[None]:
        yield

    def served(self, *, degraded: bool, result_count: int, doc_ids: tuple[str, ...] | None) -> None:
        return None

    def failed(self, exc: BaseException) -> None:
        return None

    def emit(self) -> None:
        return None

    def __repr__(self) -> str:
        return "NULL_RECORD"


NULL_RECORD: SearchRecord = _NullRecord()


def _failure(exc: BaseException) -> tuple[str, str | None]:
    """The stored outcome and error code of an exception that left the route."""
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", None
    if isinstance(exc, RateLimitError):
        return "rate_limited", "rate_limit_exceeded"
    if isinstance(exc, TemporarilyUnavailableError):
        return "error", "temporarily_unavailable"
    return "error", "internal_error"


class BoundSearchRecord:
    """One REST search made with a bound key, offered once from ``emit()``."""

    __slots__ = (
        "_clock",
        "_clock_start",
        "_doc_ids",
        "_emitted",
        "_error_code",
        "_outcome",
        "_publication",
        "_result_count",
        "_skip",
        "_started_at",
        "_visitor_key",
        "_visitor_source",
        "visitor_ignored",
    )

    live = True

    def __init__(
        self,
        *,
        publication: ServingEndpoint,
        started_at: datetime,
        clock_start: float,
        visitor: object,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._publication = publication
        self._started_at = started_at
        self._clock_start = clock_start
        self._clock = clock
        self._visitor_source: object = visitor
        self._visitor_key: str | None = None
        self.visitor_ignored: str | None = None
        # Bound before the route's try: anything that escapes unrecorded is a failure.
        self._outcome = "error"
        self._error_code: str | None = "internal_error"
        self._result_count: int | None = None
        self._doc_ids: tuple[str, ...] = ()
        self._skip = False
        self._emitted = False

    @property
    def publication(self) -> ServingEndpoint | None:
        return self._publication

    def resolve_visitor(self) -> None:
        """Read the visitor header once, keep only its daily key."""
        source, self._visitor_source = self._visitor_source, None
        if not isinstance(source, VisitorHeaderSource):
            return
        try:
            values = source.values()
            self._visitor_key, self.visitor_ignored = resolve_visitor(
                values,
                endpoint_id=self._publication.id,
                day=self._started_at.date(),
                key=get_settings().audit_hmac_key,
            )
        except Exception:  # noqa: BLE001 - a visitor never fails a search
            self._visitor_key, self.visitor_ignored = None, None

    @contextmanager
    def gate(self) -> Iterator[None]:
        """A serving gate's refusal leaves no event (as on public MCP)."""
        try:
            yield
        except _SERVING_GATE_REFUSALS:
            self._skip = True
            raise

    def served(self, *, degraded: bool, result_count: int, doc_ids: tuple[str, ...] | None) -> None:
        self._outcome = "degraded" if degraded else "success"
        self._error_code = None
        self._result_count = result_count
        self._doc_ids = doc_ids or ()

    def failed(self, exc: BaseException) -> None:
        self._outcome, self._error_code = _failure(exc)
        self._result_count = None
        self._doc_ids = ()

    def emit(self) -> None:
        if self._emitted:
            return
        self._emitted = True
        if self._skip:
            return
        try:
            writer = get_usage_writer()
            if writer is None:
                return
            p = self._publication
            writer.offer(
                UsageEventInput(
                    endpoint_id=p.id,
                    workspace_id=p.workspace_id,
                    context_id=p.context_id,
                    resource_pk=p.resource_pk,
                    resource_id=p.resource_id,
                    tool=PMU_SEARCH_TOOL,
                    outcome=self._outcome,
                    era=PMU_REST_ERA,
                    started_at=self._started_at,
                    duration_ms=max(0, int((self._clock() - self._clock_start) * 1000)),
                    error_code=self._error_code,
                    result_count=self._result_count,
                    doc_ids=self._doc_ids,
                    route=PMU_ROUTE_REST,
                    caller=PMU_CALLER_BOUND_KEY,
                    visitor_key=self._visitor_key,
                )
            )
        except Exception as exc:  # noqa: BLE001 - recording never changes an answer
            try:
                logger.warning("public_search_usage_failed", error_type=type(exc).__name__)
            except Exception:  # noqa: BLE001, S110 - the logger itself failed
                pass

    def __repr__(self) -> str:
        return f"BoundSearchRecord(outcome={self._outcome!r})"


async def start_bound_record(
    db: AsyncSession,
    *,
    context: Any,
    started_at: datetime,
    clock_start: float,
    visitor: object,
) -> SearchRecord:
    """The record of a bound-key search, or ``NULL_RECORD`` (no serving publication).

    The lookup runs in a savepoint on the request session, so an error there
    cannot abort the request's transaction; it logs one warning (the error
    class only) and the search goes on unrecorded.
    """
    try:
        async with db.begin_nested():
            publication = await load_context_publication(
                db, context_id=context.id, workspace_id=context.workspace_id
            )
    except Exception as exc:  # noqa: BLE001 - recording never changes an answer
        logger.warning("public_search_publication_unread", error_type=type(exc).__name__)
        return NULL_RECORD
    if publication is None:
        return NULL_RECORD
    return BoundSearchRecord(
        publication=publication,
        started_at=started_at,
        clock_start=clock_start,
        visitor=visitor,
    )
