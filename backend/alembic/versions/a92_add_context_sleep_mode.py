"""Add sleep_mode column to contexts table.

Issue #101: Context-level Sleep Maintenance mode control.
Allows per-context configuration of which sleep phases run:
  - 'full': All phases (default, for personal AI memory)
  - 'edges_only': Edge Discovery + Reindex only (for resource ingest contexts)
  - 'skip': No sleep maintenance (for large-scale or externally managed contexts)

Revision ID: a92_context_sleep_mode
Revises: a91_sleep_reports
"""

from alembic import op

import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a92_context_sleep_mode"
down_revision = "a91_sleep_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add sleep_mode column with CHECK constraint."""
    op.add_column(
        "contexts",
        sa.Column(
            "sleep_mode",
            sa.String(20),
            nullable=False,
            server_default="full",
        ),
    )
    op.create_check_constraint(
        "valid_sleep_mode",
        "contexts",
        "sleep_mode IN ('full', 'edges_only', 'skip')",
    )


def downgrade() -> None:
    """Remove sleep_mode column."""
    op.drop_constraint("valid_sleep_mode", "contexts", type_="check")
    op.drop_column("contexts", "sleep_mode")
