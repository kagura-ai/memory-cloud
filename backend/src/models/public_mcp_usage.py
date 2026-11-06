"""Public MCP usage events (#2678): one row per completed public ``tools/call``.

``public_mcp_usage_events`` is the durable, privacy-minimised stream behind the
anonymous public MCP endpoint (#2675). A row carries the server-resolved
endpoint identity, the tool (one of the five public names or ``other``), the
outcome, timing and bounded document observations keyed by an opaque HMAC
``doc_key``. It never carries query text, result text, memory ids, the raw
session id, an IP address, a user agent or any name.

``kind='gap'`` rows record telemetry loss in the same stream (events dropped
because the queue was full, a write failed, or the process shut down); they
carry no workspace data and appear in every export scope.

No foreign keys on the events table (audit posture, as
``memory_access_events``): rows outlive endpoints, so an unpublish and
re-publish is visible as a change of ``endpoint_id``.

Export order is ``(txid, id)``: ``txid`` is the inserting top-level
transaction (``pg_current_xact_id()``), which with the snapshot ``xmin``
horizon gives a cursor that cannot skip a late commit
(``services/public_mcp_usage_events.py``).

CHECK constraints are rendered from the module tuples below byte-identically
to the migration literals (``f28_2678_public_mcp_usage_events``); new values
are APPENDED, never reordered.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.json_types import NullableJSONB

PMU_KINDS: tuple[str, ...] = ("tool_call", "gap")
PMU_TOOLS: tuple[str, ...] = (
    "recall",
    "reference",
    "explore",
    "list_tags",
    "get_context_info",
    "other",
)
PMU_OUTCOMES: tuple[str, ...] = (
    "success",
    "degraded",
    "denied",
    "rate_limited",
    "invalid_request",
    "error",
    "cancelled",
)
PMU_ERAS: tuple[str, ...] = ("session", "stateless")
PMU_GAP_REASONS: tuple[str, ...] = ("queue_full", "write_failed", "shutdown")

# 4 KB JSONB cap (the llm_call_log / memory_access_events precedent).
PMU_DOCUMENTS_MAX_BYTES = 4096

# The inserting top-level transaction id as a bigint (xid8 never wraps).
PMU_TXID_DEFAULT = "pg_current_xact_id()::text::bigint"

PMU_TOOL_CALL_SHAPE = (
    "kind <> 'tool_call' OR (endpoint_id IS NOT NULL AND workspace_id IS NOT NULL "
    "AND context_id IS NOT NULL AND resource_pk IS NOT NULL AND resource_id IS NOT NULL "
    "AND tool IS NOT NULL AND outcome IS NOT NULL AND era IS NOT NULL "
    "AND duration_ms IS NOT NULL)"
)
PMU_GAP_SHAPE = (
    "kind <> 'gap' OR (dropped_count IS NOT NULL AND gap_started_at IS NOT NULL "
    "AND gap_ended_at IS NOT NULL AND gap_reason IS NOT NULL)"
)


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


class PublicMcpUsageEvent(Base):
    """One completed public ``tools/call``, or one recorded telemetry gap."""

    __tablename__ = "public_mcp_usage_events"

    # Storage order; the second key of the export cursor. Never exported.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # The id a consumer stores.
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, server_default=func.gen_random_uuid()
    )
    txid: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(PMU_TXID_DEFAULT)
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    context_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource_pk: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    era: Mapped[str | None] = mapped_column(String(10), nullable=True)
    protocol_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    continuation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    documents: Mapped[list | None] = mapped_column(NullableJSONB, nullable=True)
    documents_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    session_key: Mapped[str | None] = mapped_column(CHAR(32), nullable=True)
    client_family: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dropped_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gap_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gap_ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gap_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint(f"kind IN ({_in_list(PMU_KINDS)})", name="valid_pmue_kind"),
        CheckConstraint(f"tool IS NULL OR tool IN ({_in_list(PMU_TOOLS)})", name="valid_pmue_tool"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_in_list(PMU_OUTCOMES)})",
            name="valid_pmue_outcome",
        ),
        CheckConstraint(f"era IS NULL OR era IN ({_in_list(PMU_ERAS)})", name="valid_pmue_era"),
        CheckConstraint(
            f"gap_reason IS NULL OR gap_reason IN ({_in_list(PMU_GAP_REASONS)})",
            name="valid_pmue_gap_reason",
        ),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="pmue_duration_ms"),
        CheckConstraint("dropped_count IS NULL OR dropped_count >= 1", name="pmue_dropped_count"),
        CheckConstraint(
            f"documents IS NULL OR octet_length(documents::text) <= {PMU_DOCUMENTS_MAX_BYTES}",
            name="pmue_documents_size",
        ),
        CheckConstraint(PMU_TOOL_CALL_SHAPE, name="pmue_tool_call_shape"),
        CheckConstraint(PMU_GAP_SHAPE, name="pmue_gap_shape"),
        Index("ix_pmue_txid_id", "txid", "id"),
        Index("ix_pmue_occurred_at", "occurred_at"),
        Index(
            "ix_pmue_endpoint_occurred",
            "endpoint_id",
            "occurred_at",
            postgresql_where=text("endpoint_id IS NOT NULL"),
        ),
    )


class PublicMcpDocKey(Base):
    """Maps an opaque ``doc_key`` back to the resource's own document id.

    Lets an owner-side reader resolve a key to the currently public document
    without inverting the HMAC. Rows follow the resource on erasure.
    """

    __tablename__ = "public_mcp_doc_keys"

    resource_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    doc_key: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    resource_doc_id: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class PublicMcpUsageRetention(Base):
    """Singleton purge mark: every purged event is at or below ``(txid, id)``.

    A cursor below the mark may have missed purged rows and is answered with
    ``cursor_expired``; the migration inserts row 1.
    """

    __tablename__ = "public_mcp_usage_retention"

    # A singleton keyed by the literal 1 (the migration inserts it): no sequence,
    # and the defaults are written the way the migration writes them, so
    # create_all() and `alembic upgrade head` build the same table.
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    purged_through_txid: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    purged_through_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    purged_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (CheckConstraint("id = 1", name="pmur_singleton"),)
