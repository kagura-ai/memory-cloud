"""SQLAlchemy model for the comprehensive LLM call ledger (#474).

Issue #474: Append-only event log capturing every LLM, embedding, and
rerank API call across the application. The companion to ``sleep_reports``
(run-shaped, one row per sleep run) — this table is event-shaped, one row
per provider call, used by call sites that don't fit the run shape:

- **Recall reranking** (Voyage / Cohere) — ``reranker_service`` is called
  from the recall path, not the sleep path. Cohere bills per "search
  unit" ($2 / 1k SU) which has no place in ``sleep_reports``.
- **Recall path embedding** — every recall/semantic search call embeds the
  query. Today this is uninstrumented (#475 PR-3 will switch the recall
  path to ``embed_with_usage()`` once this table lands).
- **Future ``ask()`` synthesis** and **/ceo-style multi-lens** — both
  call LLMs outside Sleep.
- **One-off admin actions** (backfill, manual recalibration) that call
  LLMs.

Cost aggregation in #472 UNION ALLs ``sleep_reports`` (run-shaped, source
of truth for Sleep cost) and ``llm_call_log`` (event-shaped, source of
truth for non-Sleep cost) and groups by ``caller`` for the dashboard
breakdown. ``llm_call_log`` rows are NEVER written by Sleep Maintenance —
Sleep is a run-shaped emitter and its cost lives in ``sleep_reports`` +
``sleep_report_llm_usage``. Writing Sleep cost twice would force a
reconciliation contract that delivers no value (gate1 yellow #3,
2026-05-14 decision; see memory ``9db43e36`` for the canonical pattern).

## Design notes

**``cost_usd`` is write-time snapshot.** The writer joins ``llm_pricing``
at insert time with ``effective_from <= occurred_at`` and stores the
computed value as a snapshot. Lazy/read-time computation would tie cost
reports to the pricing table's current state, breaking historical
reproducibility across price changes (gate1 yellow #1, 2026-05-14). A
pricing-table miss writes ``cost_usd = 0`` and sets
``call_metadata['pricing_miss'] = true`` so the row stays queryable and
the gap surfaces in the dashboard as "cost unknown" without an
``IntegrityError`` on the hot path.

**``call_metadata`` size is capped at 4 KB via CHECK constraint.** The
field is free-form JSONB intended for opaque audit fields (request IDs,
latency, retry counts). Capping it at the schema layer defends against
accidentally storing prompt bodies — a privacy/PII concern that is
explicitly out of scope for this issue (the codebase's first ``JSONB``
size CHECK, well-documented in this docstring as the canonical pattern
for future PII-sensitive JSONB columns). Application-layer writers should
never serialize raw prompts into this column.

**``paid_by`` mirrors ``sleep_reports.paid_by``** ('platform' vs 'byok')
so the #472 UNION ALL aggregation can group both tables on the same axis
without coalescing NULLs. ``platform`` covers the standard kagura-managed
billing path; ``byok`` covers users routing through their own API keys.

**``caller`` nullability matrix.** A defense-in-depth Python assertion in
``services/llm_call_log_writer.py`` enforces these contracts on the
writer side too:

| caller    | user_id | workspace_id | context_id |
|-----------|---------|--------------|------------|
| recall    | NOT NULL| NOT NULL     | NOT NULL   |
| rerank    | NOT NULL| NOT NULL     | NOT NULL   |
| ask       | NOT NULL| NOT NULL     | NOT NULL   |
| admin     | nullable| nullable     | nullable   |
| sleep     | (never written, see above)                |

The 'sleep' value in the CHECK constraint exists for forward-compat
(future migration that might unify sleep ledger here) but the writer
refuses to emit it today. Adding it to the CHECK now is cheap; adding it
later would force a CHECK-constraint migration.

## Out of scope for this issue

- **Partitioning / retention strategy.** v1 ships with no partitioning
  and unlimited retention. The 100M-row growth threshold is the
  escalation trigger to file a follow-up issue. ``occurred_at`` index
  + ``(workspace_id, occurred_at)`` index are sized for low-tens-of-
  millions of rows.
- **Aggregation API** that UNIONs this table with ``sleep_reports`` —
  that lives in #472 (extension / sibling endpoint), not this issue.
- **Recall path instrumentation** itself — this issue only lands the
  table + a writer service stub. The recall path embed/rerank call
  sites switch to the writer in #475 PR-3.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID as PyUUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Public list of allowed ``caller`` values. Imported by the writer
# service for input validation (defense in depth — the DB CHECK is the
# source of truth). Keep in sync with the ``valid_llm_call_log_caller``
# CHECK constraint string below AND the matching string in the alembic
# migration (op.create_table form, see ``e12_474_llm_call_log``).
LLM_CALL_LOG_CALLERS: tuple[str, ...] = (
    "sleep",
    "recall",
    "rerank",
    "ask",
    "admin",
)

# Public list of allowed ``call_type`` values. The codebase consistently
# uses ``unit_type`` / ``pricing_model`` / ``call_type``-style names
# (snake_case + ``_type`` suffix) — see ``llm_pricing.unit_type``. The
# issue body draft used ``call_kind`` but that doesn't match this
# convention.
LLM_CALL_LOG_CALL_TYPES: tuple[str, ...] = (
    "completion",
    "embedding",
    "rerank",
)

# Mirrors ``models.sleep.SLEEP_REPORT_PAID_BY_VALUES``. Kept local rather
# than imported across modules so ``models/__init__.py`` doesn't take an
# implicit dependency on Sleep models being import-ordered first.
LLM_CALL_LOG_PAID_BY_VALUES: tuple[str, ...] = ("platform", "byok")

# 4 KB cap on the ``call_metadata`` JSONB field. Defends against
# accidental storage of full prompt bodies. The check uses
# ``octet_length(call_metadata::text)`` because PostgreSQL doesn't expose
# a native JSONB-byte-size function — converting through text gives a
# stable upper bound (always >= the on-disk JSONB size since text is the
# verbose form).
LLM_CALL_LOG_METADATA_MAX_BYTES: int = 4096


class LLMCallLog(Base):
    """Append-only event log for one LLM/embedding/rerank API call (#474).

    See module docstring for the design rationale (write-time cost
    snapshot, ``call_metadata`` size cap, nullability contracts per
    ``caller``, Sleep stays in ``sleep_reports``).

    Usage cost per call (computed at write time from ``llm_pricing``):

        cost_usd = SUM_over_unit_type(
            tokens_for_unit_type * price_per_unit / unit_denominator
        )

    A pricing miss writes ``cost_usd = 0`` and surfaces in the dashboard
    via ``call_metadata.pricing_miss = true`` — never raise on the hot
    path (``services/llm_call_log_writer.py`` enforces this).
    """

    __tablename__ = "llm_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Naive UTC by convention (matches ``sleep_reports.started_at`` and
    # ``llm_pricing.effective_from``). Joining against ``llm_pricing``
    # for the write-time price snapshot requires both sides agree.
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Identity columns. Nullability semantics depend on ``caller`` —
    # the per-caller matrix lives in the module docstring; the writer
    # service Python-asserts it. The DB columns themselves are all
    # nullable so the ``admin`` path (which may write rows with no user
    # context, e.g. backfill jobs) can land without a CHECK constraint
    # carve-out per caller.
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    context_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    caller: Mapped[str] = mapped_column(String(20), nullable=False)
    call_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # ``provider`` / ``model`` widths match ``llm_pricing`` columns so
    # the cost-grade join doesn't need a cast / length coercion.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    # Per-call usage. Any subset may be NULL depending on ``call_type``:
    # ``completion`` populates input/output (+ cached_input / cache_write
    # when the provider supports cache pricing — Anthropic, OpenAI),
    # ``embedding`` populates ``embedding_tokens``, ``rerank`` populates
    # exactly one of (``rerank_tokens`` for Voyage, ``rerank_search_units``
    # for Cohere). The writer Python-asserts the right field is set;
    # the DB columns stay loose so future ``call_type`` values
    # (multi-modal, audio, …) can land without ALTER TABLE per kind.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_search_units: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Write-time cost snapshot. NUMERIC(12, 6) handles a single call up
    # to $999,999.999999 — orders of magnitude beyond any plausible
    # per-call cost. NOT NULL with default 0 so aggregation queries
    # don't need COALESCE; pricing misses store 0 + a flag in
    # ``call_metadata``.
    # Both ``default=`` and ``server_default=`` declared so the value is
    # populated on ORM-constructed rows (Python-side) and the schema
    # carries a DDL DEFAULT clause matching the migration (catches drift
    # between model and migration in ``test_schema_drift.py``).
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # 'platform' = kagura-managed billing path. 'byok' = user-supplied
    # API key (no kagura billing on this row, but still useful for
    # operational cost telemetry). Mirrors ``sleep_reports.paid_by``.
    paid_by: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="platform",
        server_default="platform",
    )

    # Free-form audit JSON. Allowed keys (writer convention, not
    # schema-enforced): ``request_id``, ``latency_ms``, ``retry_count``,
    # ``pricing_miss`` (sentinel for ``cost_usd=0`` rows). Schema-level
    # 4 KB cap defends against prompt bodies. The Python attribute is
    # named ``call_metadata`` (not ``metadata``) because ``metadata``
    # is reserved by the SQLAlchemy Declarative API; the SQL column
    # name follows the attribute name (which is the goal — the
    # migration creates a column literally called ``call_metadata``).
    call_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "caller IN ('sleep', 'recall', 'rerank', 'ask', 'admin')",
            name="valid_llm_call_log_caller",
        ),
        CheckConstraint(
            "call_type IN ('completion', 'embedding', 'rerank')",
            name="valid_llm_call_log_call_type",
        ),
        CheckConstraint(
            "paid_by IN ('platform', 'byok')",
            name="valid_llm_call_log_paid_by",
        ),
        CheckConstraint(
            "cost_usd >= 0",
            name="valid_llm_call_log_cost_nonneg",
        ),
        # First-of-its-kind in this codebase: schema-level cap on a
        # JSONB column's serialized size. octet_length on JSONB::text
        # gives a stable upper bound (text form is the most verbose,
        # always >= the on-disk JSONB size). The 4 KB threshold is
        # large enough for ~40-50 short audit fields and small enough
        # that a full prompt body (typically 2-10 KB) hits the wall.
        CheckConstraint(
            f"call_metadata IS NULL OR "
            f"octet_length(call_metadata::text) <= {LLM_CALL_LOG_METADATA_MAX_BYTES}",
            name="valid_llm_call_log_metadata_size",
        ),
        # Single-column index on occurred_at supports the "all-tenant
        # cost over a date range" admin query.
        Index("idx_llm_call_log_occurred_at", "occurred_at"),
        # Composite index matching the per-tenant aggregation shape
        # (workspace_id, occurred_at) — mirrors
        # ``idx_sleep_reports_workspace_source_started`` on sleep_reports
        # so the #472 UNION ALL has matching index support on both legs.
        Index(
            "idx_llm_call_log_workspace_period",
            "workspace_id",
            "occurred_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<LLMCallLog(id={self.id} caller={self.caller} "
            f"call_type={self.call_type} {self.provider}/{self.model} "
            f"cost={self.cost_usd} at={self.occurred_at})>"
        )
