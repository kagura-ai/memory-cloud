"""#2098: add context_search_configs.embedding_switched_at.

When the context's embedding route last changed through the migration CLI
(``switch_context_embedding``: --switch, --run, --rollback-to). ``--rollback-to``
re-queues from it. ``updated_at`` was the default before and moves on every
search-setting edit, so a rollback after an edit missed what was written in
between. Nullable, no default, no constraint: ADD COLUMN is metadata-only on
PG 11+. No backfill: NULL means "switched before this column existed, or never
switched"; the rollback then falls back to ``updated_at`` and says so.

Revision ID: f07_2098_embedding_switched_at
Revises: f06_2144_llm_selector
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "f07_2098_embedding_switched_at"
down_revision = "f06_2144_llm_selector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable embedding_switched_at (no backfill)."""
    op.add_column(
        "context_search_configs",
        sa.Column("embedding_switched_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop embedding_switched_at."""
    op.drop_column("context_search_configs", "embedding_switched_at")
