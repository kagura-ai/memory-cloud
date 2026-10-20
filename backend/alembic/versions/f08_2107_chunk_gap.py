"""#2107: add context_search_configs.content_chunks_gap_at and content_chunks_gap_cursor.

``content_chunks_gap_at``: when a content chunk gap was last stamped on the
context (a write that kept no chunks while its chunks were inactive, a merge
into such a context, an embedding switch); the reconciler re-queues the rows
without current chunks once the context is active, then clears it. Nullable,
no default.

``content_chunks_gap_cursor``: the reconciler's keyset cursor (memory id).
Nullable, no default.

A partial index on ``content_chunks_gap_at`` (``WHERE ... IS NOT NULL``): the
reconciler reads only the contexts with a gap.

No data step: the contexts whose earlier gaps were never recorded are
stamped once by the scheduler leader at startup, only where the deployment
gates content chunks by plan (``services.content_chunk_gaps.
seed_content_chunk_gaps``). The downgrade drops the columns and the index.

Revision ID: f08_2107_chunk_gap
Revises: f07_2098_embedding_switched_at
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers
revision = "f08_2107_chunk_gap"
down_revision = "f07_2098_embedding_switched_at"
branch_labels = None
depends_on = None

_INDEX = "ix_context_search_configs_chunk_gap"


def upgrade() -> None:
    """Add both columns and the partial index."""
    op.add_column(
        "context_search_configs",
        sa.Column("content_chunks_gap_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "context_search_configs",
        sa.Column("content_chunks_gap_cursor", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        _INDEX,
        "context_search_configs",
        ["content_chunks_gap_at"],
        postgresql_where=sa.text("content_chunks_gap_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the index and both columns."""
    op.drop_index(_INDEX, table_name="context_search_configs")
    op.drop_column("context_search_configs", "content_chunks_gap_cursor")
    op.drop_column("context_search_configs", "content_chunks_gap_at")
