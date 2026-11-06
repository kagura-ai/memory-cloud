"""One record per completed public ``tools/call`` (#2675; extended by #2678).

``record_public_call`` is called exactly once per outer ``tools/call`` on the
public endpoint, from ``dispatch.call_public_tool`` — never for
``initialize``, ``tools/list``, ``server/discover`` or ``ping``. Today it
writes one structured log line. The record carries no query text, no memory
id, no session id and no client address: sessions and IPs are not people.
#2678 replaces the body of ``record_public_call``, not its call site, and may
add fields to ``PublicCallRecord``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID

from utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_CALL_OUTCOMES: tuple[str, ...] = (
    "success",
    "degraded",
    "denied",
    "rate_limited",
    "invalid_request",
    "error",
)

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


def record_public_call(record: PublicCallRecord) -> None:
    """Record one public call. Never raises."""
    try:
        fields = asdict(record)
        for key in ("endpoint_id", "workspace_id", "context_id", "resource_pk"):
            fields[key] = str(fields[key])
        logger.info("public_mcp_call", **fields)
    except Exception as exc:  # noqa: BLE001 - search must stay available
        try:
            logger.warning("public_mcp_telemetry_failed", error_type=type(exc).__name__)
        except Exception:  # noqa: BLE001, S110 - the logger itself failed
            pass
