"""memory_access_events — append-only agent memory-access audit (RFC-0002 P0-5, #1278).

The append-only audit row for the eight memory operations performed under
**verified agent identity**: recall / reference / remember / update / forget /
load_pinned / bootstrap / feedback (design F3,
``docs/design/memory-access-events.md``). Rows store identifiers, outcome,
latency, policy decision, and keyed (HMAC) hashes — NEVER raw prompts,
retrieved content, secrets, or PII.

Audit-grade posture (distinct from the telemetry-grade
``context_read_attributions``): NO foreign keys — the trail must survive the
deletion of the agent/context/key it references (an access row for a deleted
context is exactly what an investigation needs). Append-only is enforced at
the DB level by a ``BEFORE UPDATE OR DELETE`` + ``BEFORE TRUNCATE`` trigger
(migration ``e66``, the ``e50_1128`` secret-store precedent), with a narrow
erasure carve-out limited to ``(user_id, session_id, run_id, event_metadata)``.

Retention (v1): unlimited, no partitioning, NO sampling. Escalation trigger —
**100M rows or 12 months, whichever first** — then monthly range partitioning
on ``occurred_at`` (the additive, pre-designated response). Ops plan: F7,
``docs/ops/memory-access-events-retention.md``. Volume scales with agent-bound
adoption (≥1 row per recall + per load_pinned per agent turn), not total
traffic; the P1 unbound-traffic extension MUST re-open this sizing before it
can flip on.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# CHECK constraints are derived from these module-level tuples byte-identically
# to the alembic migration literal (the house drift-pin convention; pinned by
# tests/test_memory_access_event_constants.py). New values are APPENDED, never
# reordered.
MAE_OPERATIONS: tuple[str, ...] = (
    "recall",
    "reference",
    "remember",
    "update",
    "forget",
    "load_pinned",
    "bootstrap",
    "feedback",
)
MAE_OUTCOMES: tuple[str, ...] = ("success", "denied", "error", "partial")
MAE_SURFACES: tuple[str, ...] = ("mcp", "rest")
MAE_PRINCIPAL_TYPES: tuple[str, ...] = ("api_key", "oauth", "session")
# 'would_deny' = shadow mode; 'unbound' = agent identity from a verified
# baggage claim on a credential not itself bound to that agent (attribution
# without containment). NULL = binding evaluation not applicable.
MAE_POLICY_DECISIONS: tuple[str, ...] = (
    "allowed",
    "binding_denied",
    "rbac_denied",
    "would_deny",
    "unbound",
)

# 4 KB canonical PII-sensitive JSONB cap (llm_call_log precedent).
MAE_METADATA_MAX_BYTES = 4096

# The erasure carve-out: the ONLY columns the append-only trigger permits an
# UPDATE to touch (GDPR/APPI pseudonymize + scrub). Kept here so the migration
# trigger and any reader share one source of truth.
MAE_MUTABLE_COLUMNS: tuple[str, ...] = ("user_id", "session_id", "run_id", "event_metadata")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


class MemoryAccessEvent(Base):
    """One append-only audit row per audited memory operation (#1278)."""

    __tablename__ = "memory_access_events"

    # BIGSERIAL — the house pattern for append-only logs; doubles as a keyset
    # cursor.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Identifiers only; NO foreign keys (rows outlive agents/contexts/keys).
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    context_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # OAuth sub, non-FK convention; pseudonymized on erasure (carve-out).
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    api_key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Verified only; non-NULL on every P0 row (NULL reserved for the P1
    # unbound-traffic extension).
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # run_id is carried on the wire as the vendor attribute ``kagura.agent.run.id``
    # (OTel GenAI semconv has no standard run/execution-id yet). The column is
    # name-agnostic; upstream-tracking + additive migration obligation: #1285.
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    surface: Mapped[str] = mapped_column(String(10), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    policy_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    policy_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # HMAC-SHA256 of the query text (dedicated audit key); raw query NEVER stored.
    query_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(f"operation IN ({_in_list(MAE_OPERATIONS)})", name="valid_mae_operation"),
        CheckConstraint(f"outcome IN ({_in_list(MAE_OUTCOMES)})", name="valid_mae_outcome"),
        CheckConstraint(f"surface IN ({_in_list(MAE_SURFACES)})", name="valid_mae_surface"),
        CheckConstraint(
            f"principal_type IN ({_in_list(MAE_PRINCIPAL_TYPES)})",
            name="valid_mae_principal",
        ),
        CheckConstraint(
            f"policy_decision IS NULL OR policy_decision IN ({_in_list(MAE_POLICY_DECISIONS)})",
            name="valid_mae_policy",
        ),
        CheckConstraint(
            f"octet_length(event_metadata::text) <= {MAE_METADATA_MAX_BYTES}",
            name="mae_metadata_size",
        ),
        Index("idx_mae_occurred", "occurred_at"),
        Index("idx_mae_workspace_occurred", "workspace_id", "occurred_at"),
        # Partial: agent_id is non-NULL on every P0 row; the partial form
        # anticipates the P1 unbound-traffic extension (agent-less rows).
        Index(
            "idx_mae_agent_occurred",
            "agent_id",
            "occurred_at",
            postgresql_where=text("agent_id IS NOT NULL"),
        ),
    )
