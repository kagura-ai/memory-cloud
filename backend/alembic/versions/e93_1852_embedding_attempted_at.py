"""#1852: the embedding pipeline gets its own clock, ``memories.embedding_attempted_at``.

The claim that moves a row to ``processing`` and the failure ``UPDATE`` both
stamped ``updated_at`` (#1317 made that explicit), so every memory looked
edited a moment after it was written: ``updated_at > created_at`` held for
virtually every row. ``list(filters={"updated_after": ...})`` and the
``updated`` kind of ``changes_since`` read ``updated_at`` as "a person or a
promotion changed this", so the pipeline moves to a column of its own.

Nullable, no backfill: a ``processing`` row from before this migration has a
NULL clock and is treated as stale (reclaimed on the next sweep), a ``failed``
row as immediately retry-eligible — the same NULL semantics the backoff clause
already had for ``updated_at``.

Revision ID: e93_1852_embedding_attempted_at
Revises: e92_1807_linked_by_fk

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e93_1852_embedding_attempted_at"
down_revision: str | None = "e92_1807_linked_by_fk"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("embedding_attempted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "embedding_attempted_at")
