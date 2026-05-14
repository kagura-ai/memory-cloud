"""Add llm_call_log: append-only event log for non-Sleep LLM/embedding/rerank calls (#474).

Issue #474: Companion to ``sleep_reports`` (run-shaped) — captures
per-call cost events from recall reranking, recall embedding (wired up
in #475 PR-3), future ``ask()`` synthesis, and one-off admin actions.
Cost aggregation in #472 UNION ALLs ``sleep_reports`` and ``llm_call_log``
on ``(workspace_id, occurred_at)`` and groups by ``caller`` for the
dashboard breakdown.

This revision adds schema only. Pricing seed rows for Voyage rerank-2 /
Voyage rerank-2-lite / Cohere rerank-3.5 / Cohere embed-multilingual-v3
land in the next revision (``e13_474_pricing_seeds``) so schema and
data fixes have independent rollback units.

Design notes (mirror module docstring of ``models/llm_call_log.py``):

- ``cost_usd`` is computed at write time from ``llm_pricing`` and stored
  as a snapshot (NOT NULL DEFAULT 0). Pricing misses store 0 + a flag in
  ``call_metadata``.
- ``call_metadata`` is JSONB with a 4 KB CHECK constraint defending
  against accidental storage of prompt bodies. This is the codebase's
  first schema-level JSONB size cap.
- ``paid_by`` mirrors ``sleep_reports.paid_by`` so the #472 UNION ALL
  groups both legs on the same axis.
- ``caller`` CHECK includes 'sleep' for forward-compat (future migration
  may unify) but the writer service refuses to emit it today — Sleep
  cost stays in ``sleep_reports`` + ``sleep_report_llm_usage``.
- Two indexes: ``(occurred_at)`` for the admin-wide query,
  ``(workspace_id, occurred_at)`` for per-tenant aggregation. Mirrors
  ``idx_sleep_reports_workspace_source_started`` so the UNION ALL has
  matching index support on both legs.

Out of scope (deferred to follow-ups):
- Partitioning / retention (escalation trigger: 100M rows).
- Recall path instrumentation itself (#475 PR-3).
- Aggregation API extension (#472).

Revision ID: e12_474_llm_call_log
Revises: e11_merge_e10_heads

NOTE: Revision ID is 20 chars (alembic_version.version_num is VARCHAR(32),
asyncpg raises StringDataRightTruncationError above 25 — see c02_471
header for the canonical note).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e12_474_llm_call_log"
down_revision: str | Sequence[str] | None = "e11_merge_e10_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create llm_call_log with CHECK constraints + 2 indexes."""
    op.create_table(
        "llm_call_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Naive DateTime to match ``sleep_reports.started_at`` and
        # ``llm_pricing.effective_from`` (the cost-snapshot join target).
        sa.Column("occurred_at", sa.DateTime, nullable=False),
        sa.Column("user_id", sa.String(255), nullable=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=True),
        sa.Column("context_id", UUID(as_uuid=True), nullable=True),
        sa.Column("caller", sa.String(20), nullable=False),
        sa.Column("call_type", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        # Per-call usage columns. Any subset may be NULL per ``call_type``;
        # the writer service enforces the correct field is set.
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("cached_input_tokens", sa.Integer, nullable=True),
        sa.Column("cache_write_tokens", sa.Integer, nullable=True),
        sa.Column("embedding_tokens", sa.Integer, nullable=True),
        sa.Column("rerank_tokens", sa.Integer, nullable=True),
        sa.Column("rerank_search_units", sa.Integer, nullable=True),
        # Write-time cost snapshot. NUMERIC(14, 10) mirrors
        # ``llm_pricing.price_per_unit`` precision so sub-microcent
        # per-call costs (e.g., 20 embedding tokens at $0.02/1M =
        # $0.0000004) don't round to 0 and silently undercount when
        # SUMmed across many rows (Copilot loop 2 #1). 4 digits before
        # the decimal cap per-call cost at ~$9,999.99 — orders of
        # magnitude above any plausible single API call. NOT NULL
        # DEFAULT 0 so aggregation queries don't need COALESCE; pricing
        # misses store 0 + a flag in ``call_metadata``.
        sa.Column(
            "cost_usd",
            sa.Numeric(14, 10),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "paid_by",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'platform'"),
        ),
        # Free-form audit JSON. 4 KB cap enforced by CHECK below.
        sa.Column("call_metadata", JSONB, nullable=True),
        sa.CheckConstraint(
            "caller IN ('sleep', 'recall', 'rerank', 'ask', 'admin')",
            name="valid_llm_call_log_caller",
        ),
        sa.CheckConstraint(
            "call_type IN ('completion', 'embedding', 'rerank')",
            name="valid_llm_call_log_call_type",
        ),
        sa.CheckConstraint(
            "paid_by IN ('platform', 'byok')",
            name="valid_llm_call_log_paid_by",
        ),
        sa.CheckConstraint(
            "cost_usd >= 0",
            name="valid_llm_call_log_cost_nonneg",
        ),
        # First-of-its-kind in this codebase: 4 KB schema-level cap on a
        # JSONB column to defend against prompt-body storage.
        # ``octet_length(jsonb::text)`` gives a stable upper bound — the
        # text serialization is the most verbose form, always >= the
        # on-disk JSONB size. 4096 bytes fits ~40-50 short audit fields;
        # a typical prompt body (2-10 KB) hits the wall.
        sa.CheckConstraint(
            "call_metadata IS NULL OR octet_length(call_metadata::text) <= 4096",
            name="valid_llm_call_log_metadata_size",
        ),
    )
    op.create_index(
        "idx_llm_call_log_occurred_at",
        "llm_call_log",
        ["occurred_at"],
    )
    op.create_index(
        "idx_llm_call_log_workspace_period",
        "llm_call_log",
        ["workspace_id", "occurred_at"],
    )


def downgrade() -> None:
    """Drop llm_call_log + indexes in reverse order."""
    op.drop_index("idx_llm_call_log_workspace_period", table_name="llm_call_log")
    op.drop_index("idx_llm_call_log_occurred_at", table_name="llm_call_log")
    op.drop_table("llm_call_log")
