"""#2103: add context_search_configs.content_chunks_enabled_at.

When ``content_chunks_enabled`` was last turned on. A plan's cap on how many
contexts may have content chunk vectors on keeps the earliest-enabled ones
active, so the choice over the cap is deterministic; no existing column
records the enable (``updated_at`` moves on every search-setting edit).

Rows already on are backfilled from ``updated_at``, the closest proxy (for a
row whose last write was the enable it is exact). Nullable, no
``server_default`` and no constraint: ADD COLUMN is metadata-only on PG 11+
and nothing needs a ``NOT VALID`` / ``VALIDATE``. A NULL (should not exist
after the backfill) ranks last.

Revision ID: f02_2103_chunks_enabled_at
Revises: f01_2015_content_chunks
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "f02_2103_chunks_enabled_at"
down_revision = "f01_2015_content_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add content_chunks_enabled_at and backfill it for rows already on."""
    op.add_column(
        "context_search_configs",
        sa.Column("content_chunks_enabled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE context_search_configs SET content_chunks_enabled_at = updated_at "
            "WHERE content_chunks_enabled AND content_chunks_enabled_at IS NULL"
        )
    )


def downgrade() -> None:
    """Drop the content_chunks_enabled_at column."""
    op.drop_column("context_search_configs", "content_chunks_enabled_at")
