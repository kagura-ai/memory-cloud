"""Add bm25_idf_drift_log table.

Issue #343: BM25 IDF drift observability for the kagura_memories collection.

The cron task (disabled by default; gated by BM25_DRIFT_CRON_ENABLED env var)
walks each context's per-context Qdrant collection slice, computes
Population Stability Index (PSI) of the IDF distribution between memory-only
and collection-global term frequencies, and writes one row here per context
per cycle. Production enablement is scheduled for v0.14.0 once Privacy Policy
(#359) and right-to-erasure (#360) land.

Revision ID: a98_bm25_idf_drift_log
Revises: a97_resources_entity

NOTE: Revision ID capped at 32 chars (alembic_version.version_num is
VARCHAR(32) — asyncpg raises StringDataRightTruncationError otherwise).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "a98_bm25_idf_drift_log"
down_revision = "a97_resources_entity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the bm25_idf_drift_log table with constraints and indexes."""
    op.create_table(
        "bm25_idf_drift_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "measured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # PSI is NULL when the min-N gates fail (psi_status='insufficient_data').
        # NUMERIC over Float because alert thresholds are precision-sensitive.
        sa.Column("psi", sa.Numeric(10, 6), nullable=True),
        sa.Column("psi_status", sa.String(30), nullable=False),
        sa.Column("m_memory_points", sa.Integer, nullable=False),
        sa.Column("r_resource_points", sa.Integer, nullable=False),
        sa.Column("num_terms", sa.Integer, nullable=False),
        # Each entry: {"index": int32, "df_memory": int, "df_global": int,
        #              "idf_memory": float, "idf_global": float, "delta": float}
        # The "index" is the mmh3.hash of a token; the original token text
        # is NOT stored here. Reverse lookup (token recovery) is a separate
        # admin operation scheduled for v0.14.0.
        sa.Column("top_divergent_terms", JSONB, nullable=True),
        sa.CheckConstraint(
            "psi_status IN ('insufficient_data', 'stable', 'minor_drift', 'significant_drift')",
            name="valid_bm25_idf_drift_status",
        ),
        # Pin the invariant in the DB: PSI is NULL iff status is insufficient_data.
        # Application bugs that desync them get caught at INSERT time.
        sa.CheckConstraint(
            "(psi_status = 'insufficient_data') = (psi IS NULL)",
            name="bm25_idf_drift_psi_null_iff_insufficient",
        ),
        sa.CheckConstraint(
            "m_memory_points >= 0 AND r_resource_points >= 0 AND num_terms >= 0",
            name="bm25_idf_drift_nonneg_counts",
        ),
    )

    # Most common admin query: latest rows for a given context.
    op.create_index(
        "idx_bm25_idf_drift_context_time",
        "bm25_idf_drift_log",
        ["context_id", sa.text("measured_at DESC")],
    )
    # Cross-context recent timeline for the operator dashboard.
    op.create_index(
        "idx_bm25_idf_drift_measured_at",
        "bm25_idf_drift_log",
        [sa.text("measured_at DESC")],
    )
    # Partial index keeps the alerted-state lookup small even as the table grows.
    op.create_index(
        "idx_bm25_idf_drift_alerted",
        "bm25_idf_drift_log",
        ["psi_status", sa.text("measured_at DESC")],
        postgresql_where=sa.text("psi_status IN ('minor_drift', 'significant_drift')"),
    )


def downgrade() -> None:
    """Drop the bm25_idf_drift_log table."""
    op.drop_index("idx_bm25_idf_drift_alerted", table_name="bm25_idf_drift_log")
    op.drop_index("idx_bm25_idf_drift_measured_at", table_name="bm25_idf_drift_log")
    op.drop_index("idx_bm25_idf_drift_context_time", table_name="bm25_idf_drift_log")
    op.drop_table("bm25_idf_drift_log")
