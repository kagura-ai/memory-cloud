"""Add measurements table — HOW-MUCH measurement history lane (#1333).

A dedicated append-only numeric time-series store, kept out of ``memories`` so
it is structurally excluded from recall and untouchable by Sleep consolidation
(a merged/rewritten series would be destroyed data). One row per observation of
(context_id, metric) at measured_at; the composite index backs the
recall_series bucket scan.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e71_1333_measurements"
down_revision = "e70_1348_worker_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "measurements",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "context_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("measured_at", sa.DateTime(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_measurements_context_metric_measured_at",
        "measurements",
        ["context_id", "metric", "measured_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_measurements_context_metric_measured_at", table_name="measurements")
    op.drop_table("measurements")
