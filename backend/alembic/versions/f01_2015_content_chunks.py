"""#2015: add context_search_configs.content_chunks_enabled.

The per-context opt-in for content chunk vectors: when true, a long memory's
content is also embedded as chunk points so dense retrieval sees the body,
not only the summary. ``server_default`` false — existing and new rows land on
off, so nothing changes at migration time; only contexts an operator opts in
get chunk points, and only for writes and re-embeds after the flip.

Blue-green safety: ADD COLUMN with a server_default is metadata-only on
PG 11+ (no table rewrite); old app code never references the column.

Revision ID: f01_2015_content_chunks
Revises: f00_2019_llm_log_supersede
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "f01_2015_content_chunks"
down_revision = "f00_2019_llm_log_supersede"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add content_chunks_enabled, default false."""
    op.add_column(
        "context_search_configs",
        sa.Column(
            "content_chunks_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    """Drop the content_chunks_enabled column."""
    op.drop_column("context_search_configs", "content_chunks_enabled")
