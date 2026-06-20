"""Add ``reference_count`` adoption signal; drop dead ``use_count`` + stale index (#1046).

Issue #1046: separate *adoption* (a deliberate ``reference()`` Layer-3 fetch)
from *surfacing* (recall top-k return + explore spreading-activation), which
today both bump the same ``access_count``. This migration:

1. Adds ``reference_count INTEGER NOT NULL DEFAULT 0`` — the new adoption signal
   bumped only by ``reference()``. The server default backfills every existing
   row to 0 in one statement (no historical adoption is recoverable, so 0 is the
   only honest starting value — consumers #1048/#1049 must grandfather these).
2. Drops the stale ``idx_consolidation`` index. It indexed the dead ``use_count``
   column and its leading ``(user_id, long_term)`` columns never matched the
   consolidation candidate query (which filters ``scope = 'working'``, covered by
   ``idx_user_scope``).
3. Drops the dead ``use_count`` column. It was declared but never written (the
   live ``use_count`` is the neural-node attribute, derived from ``access_count``;
   the memories column itself stayed 0 for every row).

Sleep consolidation is intentionally untouched here — it still reads
``access_count``. Switching that gate to the adoption signal is the #1049
follow-up.

Revision ID: e41_1046_reference_count
Revises: e40_979_embedding_retry_count
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e41_1046_reference_count"
down_revision = "e40_979_embedding_retry_count"
branch_labels = None
depends_on = None


_STALE_INDEX = "idx_consolidation"


def upgrade() -> None:
    """Add reference_count; drop the stale idx_consolidation index + dead use_count."""
    op.add_column(
        "memories",
        sa.Column(
            "reference_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.drop_index(_STALE_INDEX, table_name="memories")
    op.drop_column("memories", "use_count")


def downgrade() -> None:
    """Restore use_count + idx_consolidation; drop reference_count."""
    # Re-add use_count with a temporary server_default to backfill existing rows,
    # then strip the default to match the original column (ORM-side default only).
    op.add_column(
        "memories",
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("memories", "use_count", server_default=None)
    op.create_index(
        _STALE_INDEX,
        "memories",
        ["user_id", "long_term", "use_count", "importance"],
    )
    op.drop_column("memories", "reference_count")
