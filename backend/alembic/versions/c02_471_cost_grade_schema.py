"""Add cost-grade schema: sleep_report_llm_usage child table + llm_pricing master + sleep_reports embedding columns.

Issue #471: Foundation for #472 (aggregation API) and #473 (dashboard UI).
The current ``sleep_reports`` table tracks token usage as scalar totals
(``llm_calls_made``, ``llm_tokens_used``, ``embedding_calls_made``) which
is operational metric, not cost metric. To compute actual `$` cost the
schema must capture model identity, input/output split, cached input,
and a snapshot of the price-per-token at the time the run executed.

This migration adds:

1. ``sleep_report_llm_usage`` — child table with per-(phase, provider,
   model) breakdown of LLM token usage. The legacy roll-up columns on
   ``sleep_reports`` stay populated by the reporter as a sum over child
   rows for back-compat with existing dashboards / log analyzers.

2. ``llm_pricing`` — append-only master table of model rates indexed
   by ``(provider, model, unit_type, effective_from, context_min_tokens)``.
   Cost-aggregation joins pick the row whose ``effective_from <=
   sleep_run.started_at`` so historical reports stay reproducible across
   rate changes. ``unit_type`` enum (text + CHECK, intentionally NOT
   PostgreSQL ENUM — ALTER-friendly, plays well with alembic) covers
   input/output/cache_read/cache_write/embedding tokens AND
   rerank_tokens / rerank_search_units. The latter two are reserved for
   #474 (no rows seeded in #471) but the enum value must exist now to
   avoid a CHECK-constraint migration when #474 lands.
   ``unit_denominator`` bridges per-1M-tokens (Anthropic / OpenAI / Google)
   and per-1k-search-units (Cohere rerank) in the same row shape.

3. Embedding scalar columns on ``sleep_reports`` —
   ``embedding_provider``, ``embedding_model``, ``embedding_tokens``.
   Embedding is instance-global (one ``EMBEDDING_PROVIDER`` /
   ``EMBEDDING_MODEL`` per process per ``backend/src/config/settings.py``)
   so a single triple per run is sufficient. v0.15.0 reporter populates
   these from the reindex phase only — phase 1 / phase 2 also call the
   embedding API but don't increment any counter today; #475 closes that
   gap separately.

Seed pricing rows are inserted by the next migration
(``c03_471_seed_pricing``) — splitting schema and data keeps the
rollback path simpler.

Revision ID: c02_471_cost_grade_schema
Revises: c01_360_erasure_requests

NOTE: Revision ID is 25 chars (alembic_version.version_num is VARCHAR(32)
— asyncpg raises StringDataRightTruncationError otherwise).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c02_471_cost_grade_schema"
down_revision: str | Sequence[str] | None = "c01_360_erasure_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create cost-grade schema: child table + pricing master + sleep_reports columns."""
    # 1. sleep_report_llm_usage — per-(phase, provider, model) child table.
    op.create_table(
        "sleep_report_llm_usage",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "report_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sleep_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column(
            "input_tokens",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_tokens",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cached_input_tokens",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("calls", sa.Integer, nullable=False, server_default=sa.text("0")),
        # ``tokenizer_version`` is audit only — not used as a price-lookup key.
        sa.Column("tokenizer_version", sa.String(50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "phase IN ('edge_discovery', 'dedup_merge', 'importance_reeval', "
            "'consolidation', 'reindex')",
            name="valid_sleep_report_llm_usage_phase",
        ),
    )
    # PostgreSQL does NOT automatically index foreign-key columns, so keep
    # an explicit index on report_id. The GROUP BY query in #472 will also
    # frequently filter by (provider, model) across a date range of reports;
    # report_id at the tail of the composite index helps JOIN ordering.
    op.create_index(
        "idx_sleep_report_llm_usage_report_id",
        "sleep_report_llm_usage",
        ["report_id"],
    )
    op.create_index(
        "idx_sleep_report_llm_usage_provider_model",
        "sleep_report_llm_usage",
        ["provider", "model", "report_id"],
    )

    # 2. llm_pricing — append-only master table.
    op.create_table(
        "llm_pricing",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("unit_type", sa.String(30), nullable=False),
        # Naive DateTime to match ``sleep_reports.started_at`` and the
        # rest of the codebase (UTC-by-convention). Avoids tz-aware vs
        # tz-naive TypeError at the SQLAlchemy expression level when
        # #472 joins on ``effective_from <= started_at``.
        sa.Column("effective_from", sa.DateTime, nullable=False),
        sa.Column(
            "context_min_tokens",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("context_max_tokens", sa.Integer, nullable=True),
        sa.Column("price_per_unit", sa.Numeric(14, 10), nullable=False),
        sa.Column(
            "currency",
            sa.String(3),
            nullable=False,
            server_default=sa.text("'USD'"),
        ),
        sa.Column(
            "unit_denominator",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("1000000"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "model",
            "unit_type",
            "effective_from",
            "context_min_tokens",
            name="uq_llm_pricing_lookup_key",
        ),
        sa.CheckConstraint(
            "unit_type IN ('input_tokens', 'output_tokens', "
            "'cache_read_tokens', 'cache_write_tokens', "
            "'embedding_tokens', 'rerank_tokens', 'rerank_search_units')",
            name="valid_llm_pricing_unit_type",
        ),
        sa.CheckConstraint(
            "price_per_unit >= 0",
            name="valid_llm_pricing_price_nonneg",
        ),
        sa.CheckConstraint(
            "unit_denominator > 0",
            name="valid_llm_pricing_unit_denominator_positive",
        ),
        sa.CheckConstraint(
            "context_min_tokens >= 0",
            name="valid_llm_pricing_context_min_nonneg",
        ),
        sa.CheckConstraint(
            "context_max_tokens IS NULL OR context_max_tokens > context_min_tokens",
            name="valid_llm_pricing_context_max_gt_min",
        ),
    )
    # NOTE: no explicit ``idx_llm_pricing_lookup`` — PostgreSQL's
    # auto-created index for the ``uq_llm_pricing_lookup_key`` UNIQUE
    # constraint covers the lookup query (provider, model, unit_type,
    # effective_from prefix). An additional index would just duplicate
    # write cost and storage. See models/llm_pricing.py for the same
    # decision documented at the model level.

    # 3. Embedding cost-grade columns on sleep_reports. All nullable / 0
    # default for backfill compatibility — pre-migration rows render as
    # "cost unknown" in #473's dashboard.
    op.add_column(
        "sleep_reports",
        sa.Column("embedding_provider", sa.String(50), nullable=True),
    )
    op.add_column(
        "sleep_reports",
        sa.Column("embedding_model", sa.String(100), nullable=True),
    )
    op.add_column(
        "sleep_reports",
        sa.Column(
            "embedding_tokens",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    """Drop cost-grade schema in reverse order."""
    # Reverse order: columns on sleep_reports → llm_pricing → child table.
    op.drop_column("sleep_reports", "embedding_tokens")
    op.drop_column("sleep_reports", "embedding_model")
    op.drop_column("sleep_reports", "embedding_provider")

    op.drop_table("llm_pricing")

    op.drop_index(
        "idx_sleep_report_llm_usage_provider_model",
        table_name="sleep_report_llm_usage",
    )
    op.drop_index(
        "idx_sleep_report_llm_usage_report_id",
        table_name="sleep_report_llm_usage",
    )
    op.drop_table("sleep_report_llm_usage")
