"""#1220: add router_calibrations (per-context router arm performance).

Stage 4 of the #1212 query-intent router: persist per-bucket arm
performance so managed-cloud tuning can diverge from self-host defaults.
Rows with ``context_id IS NULL`` are the fleet defaults measured on the
frozen eval corpus (written by ``tests.eval.router_gate_runner``); rows
with a ``context_id`` come from live-traffic measurements. Keying mirrors
``embedding_calibrations``: partial-unique indexes split on NULL context
because Postgres unique constraints treat NULLs as distinct.

Blue-green safety: pure CREATE TABLE — no existing table or app code is
touched; old app code never references the table.

Revision ID: e59_1220_router_calibrations
Revises: e58_1212_routing_mode
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers
revision = "e59_1220_router_calibrations"
down_revision = "e58_1212_routing_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the router_calibrations table + partial-unique indexes."""
    op.create_table(
        "router_calibrations",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("bucket", sa.String(16), nullable=False),
        sa.Column("arm", sa.String(16), nullable=False),
        sa.Column("p_at_5", sa.Float, nullable=False),
        sa.Column("mrr_at_10", sa.Float, nullable=False),
        sa.Column("n_queries", sa.Integer, nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="frozen_corpus"),
        # No server_default: the measurement timestamp is always supplied by
        # the writer (a defaulted "now" would fake a sampling time).
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "bucket IN ('keyword', 'semantic', 'hybrid')",
            name="router_calibrations_bucket_check",
        ),
        sa.CheckConstraint(
            "arm IN ('keyword', 'semantic', 'hybrid', 'routed')",
            name="router_calibrations_arm_check",
        ),
        sa.CheckConstraint(
            "source IN ('frozen_corpus', 'live_traffic')",
            name="router_calibrations_source_check",
        ),
        sa.CheckConstraint(
            "p_at_5 >= 0.0 AND p_at_5 <= 1.0",
            name="router_calibrations_p_at_5_range",
        ),
        sa.CheckConstraint(
            "mrr_at_10 >= 0.0 AND mrr_at_10 <= 1.0",
            name="router_calibrations_mrr_range",
        ),
        sa.CheckConstraint("n_queries >= 0", name="router_calibrations_nonneg_n"),
    )
    op.create_index(
        "ix_router_calibrations_context_id",
        "router_calibrations",
        ["context_id"],
    )
    op.create_index(
        "uq_router_calibration_global",
        "router_calibrations",
        ["bucket", "arm", "source"],
        unique=True,
        postgresql_where=sa.text("context_id IS NULL"),
    )
    op.create_index(
        "uq_router_calibration_context",
        "router_calibrations",
        ["context_id", "bucket", "arm", "source"],
        unique=True,
        postgresql_where=sa.text("context_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the router_calibrations table."""
    op.drop_index("uq_router_calibration_context", table_name="router_calibrations")
    op.drop_index("uq_router_calibration_global", table_name="router_calibrations")
    op.drop_index("ix_router_calibrations_context_id", table_name="router_calibrations")
    op.drop_table("router_calibrations")
