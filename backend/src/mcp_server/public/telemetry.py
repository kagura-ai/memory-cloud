"""One record per completed public ``tools/call`` (#2675; extended by #2678).

``record_public_call`` is called exactly once per outer ``tools/call`` on the
public endpoint, from ``dispatch.call_public_tool`` — never for
``initialize``, ``tools/list``, ``server/discover`` or ``ping``. It writes one
structured log line and, when ``MCP_PUBLIC_TELEMETRY_ENABLED`` is on, offers
the call to the usage event writer (#2678), a ``put_nowait`` that never waits
on the database. The log line carries no query text, no memory id, no session
id, no document id and no client address: sessions and IPs are not people.
The record's ``doc_ids``, ``session_id`` and ``client_name`` are transient:
the writer turns them into keyed hashes (or nothing) before anything is stored.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from services.public_mcp_usage_events import UsageEventInput
from services.public_mcp_usage_writer import get_usage_writer
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_CALL_OUTCOMES: tuple[str, ...] = (
    "success",
    "degraded",
    "denied",
    "rate_limited",
    "invalid_request",
    "error",
    "cancelled",  # #2678: the client went away or the server shut down mid-call
)

# Transient fields: never in the log line.
_NOT_LOGGED = ("doc_ids", "session_id", "client_name", "started_at")

Era = Literal["session", "stateless"]


@dataclass(frozen=True)
class PublicCallRecord:
    """What one public ``tools/call`` did, for later aggregation."""

    endpoint_id: UUID
    public_id: str
    workspace_id: UUID
    context_id: UUID
    resource_pk: UUID
    resource_id: str
    tool: str  # the requested name, cut to 64 characters
    outcome: str  # one of PUBLIC_CALL_OUTCOMES
    duration_ms: int  # the whole public pipeline, allowlist check to response
    era: Era
    protocol_version: str | None
    result_count: int | None  # recall/explore/list_tags rows, reference 1/0, else None
    continuation: bool  # reference with content_offset > 0
    # #2678
    started_at: datetime = field(default_factory=utcnow)  # naive UTC start of the call
    error_code: str | None = None  # the public error code of a refusal or failure
    doc_ids: tuple[str, ...] = ()  # returned rows' document ids, rank order
    session_id: str | None = None  # session era only
    client_name: str | None = None  # the client's self-declared name, unverified


def record_public_call(record: PublicCallRecord) -> None:
    """Log one public call and offer it to the usage event writer. Never raises."""
    try:
        fields = asdict(record)
        for key in _NOT_LOGGED:
            fields.pop(key, None)
        for key in ("endpoint_id", "workspace_id", "context_id", "resource_pk"):
            fields[key] = str(fields[key])
        logger.info("public_mcp_call", **fields)
        writer = get_usage_writer()
        if writer is not None:
            writer.offer(
                UsageEventInput(
                    endpoint_id=record.endpoint_id,
                    workspace_id=record.workspace_id,
                    context_id=record.context_id,
                    resource_pk=record.resource_pk,
                    resource_id=record.resource_id,
                    tool=record.tool,
                    outcome=record.outcome,
                    era=record.era,
                    started_at=record.started_at,
                    duration_ms=record.duration_ms,
                    error_code=record.error_code,
                    protocol_version=record.protocol_version,
                    result_count=record.result_count,
                    continuation=record.continuation,
                    doc_ids=record.doc_ids,
                    session_id=record.session_id,
                    client_name=record.client_name,
                )
            )
    except Exception as exc:  # noqa: BLE001 - search must stay available
        try:
            logger.warning("public_mcp_telemetry_failed", error_type=type(exc).__name__)
        except Exception:  # noqa: BLE001, S110 - the logger itself failed
            pass
