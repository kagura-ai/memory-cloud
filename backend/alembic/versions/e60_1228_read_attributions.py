"""#1228: add context_read_attributions (cross-context recall read visibility).

A cross-context ``recall(context_ids=[A, B, ...])`` bills one quota unit —
the single ``usage_stats`` row under the primary context (A). Reads on the
other listed contexts were invisible to per-context consumers, so the
memory-health retrieval section false-WARNed ``write_only_store`` on
contexts read exclusively via cross-context recall.

This table records one diagnostic attribution row per ADDITIONAL listed
context. It is deliberately separate from ``usage_stats``: every
``usage_stats`` reader counts rows for quota/billing/analytics, and a
same-table marker would tax each of them with an exclusion filter.
Attribution rows are structurally invisible to them by construction.

Blue-green safety: pure CREATE TABLE — no existing table or app code is
touched; old app code never references the table.

Revision ID: e60_1228_read_attributions
Revises: e59_1220_router_calibrations

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32), and a longer ID fails the version-table UPDATE at upgrade
time (StringDataRightTruncationError; caught by the migration round-trip
test on this very revision's first draft).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers
revision = "e60_1228_read_attributions"
down_revision = "e59_1220_router_calibrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the context_read_attributions table + indexes."""
    op.create_table(
        "context_read_attributions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_context_read_attr_user_created",
        "context_read_attributions",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_context_read_attr_context",
        "context_read_attributions",
        ["context_id"],
    )


def downgrade() -> None:
    """Drop the context_read_attributions table."""
    op.drop_index("idx_context_read_attr_context", table_name="context_read_attributions")
    op.drop_index("idx_context_read_attr_user_created", table_name="context_read_attributions")
    op.drop_table("context_read_attributions")
