"""Add ``embedding_retry_count`` for bounded auto-requeue of failed embeddings (#979).

Issue #979: the periodic embedding sweep only requeued ``pending`` and stale
``processing`` rows — never ``failed``. A transient embedding/Qdrant blip left a
memory ``failed`` permanently (unsearchable) until an admin manually triggered
the retry endpoint. This column lets the sweep auto-requeue ``failed`` rows,
bounded by a retry counter so a permanently-poison row cannot loop forever.

Adds ``embedding_retry_count INTEGER NOT NULL DEFAULT 0``. The server default
backfills every existing row to 0 in one statement (so prior ``failed`` rows
get a fresh auto-retry budget on first sweep after deploy).

Revision ID: e40_979_embedding_retry_count
Revises: e39_962_failed_file_gc
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e40_979_embedding_retry_count"
down_revision = "e39_962_failed_file_gc"
branch_labels = None
depends_on = None


_SWEEP_INDEX = "idx_memories_embedding_unsettled"


def upgrade() -> None:
    """Add embedding_retry_count + the embedding-sweep partial index."""
    op.add_column(
        "memories",
        sa.Column(
            "embedding_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Partial index for the 30s embedding sweep (#979): scan only the
    # not-yet-settled rows instead of the whole (mostly 'success') table.
    op.create_index(
        _SWEEP_INDEX,
        "memories",
        ["embedding_status"],
        postgresql_where=sa.text("embedding_status <> 'success'"),
    )


def downgrade() -> None:
    op.drop_index(_SWEEP_INDEX, table_name="memories")
    op.drop_column("memories", "embedding_retry_count")
