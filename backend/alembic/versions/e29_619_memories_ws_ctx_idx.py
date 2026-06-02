"""Add compound partial index (workspace_id, context_id) on memories (#619).

``aggregate_tags``, ``get_context_stats``, and ``_refresh_hub_tag_cache`` all
filter ``memories`` by ``WHERE workspace_id = … AND context_id = … AND
deleted_at IS NULL``.  The current schema has two single-column indexes
(``ix_memories_workspace_id``, ``ix_memories_context_id``) which PostgreSQL
bitmap-merges for that triple-predicate shape.

This migration adds a compound partial index that covers the three-column scope
scan in a single tight range lookup — eliminating the bitmap merge and the
soft-deleted row overhead.

### Index shape

``idx_memories_ws_ctx`` is a plain B-tree on ``(workspace_id, context_id)``
with a ``WHERE deleted_at IS NULL`` partial predicate.  The partial clause
keeps soft-deleted rows out of the index, which:

- Avoids write-amplification for the delete-soft path (``UPDATE memories SET
  deleted_at = now()`` removes the row from the index without a full
  heap-fetch rebuild).
- Shrinks the index by the fraction of soft-deleted rows, improving cache
  efficiency for the 99 % live-row reads.

The existing single-column indexes remain in place; this index does not replace
them — they continue to serve unscoped queries and the planner's other plans.

### Zero-downtime build

``memories`` is a high-write table.  A plain transactional ``CREATE INDEX``
takes ``ACCESS EXCLUSIVE`` for the build duration, blocking writes.  Following
the b02/e26 pattern, the index is built with ``CREATE INDEX CONCURRENTLY IF NOT
EXISTS`` inside ``autocommit_block`` (Alembic wraps migrations in a transaction
by default; ``CONCURRENTLY`` cannot run inside one), with an INVALID-leftover
guard so a retry after a mid-build failure rebuilds cleanly.

### Downgrade

Drops the index ``CONCURRENTLY``.  The single-column indexes are unaffected.

Revision ID: e29_619_memories_ws_ctx_idx
Revises: e28_850_workspace_connectors
Create Date: 2026-06-02

(Revision ID kept ≤ 32 chars to fit ``alembic_version.version_num
varchar(32)``; the longer descriptive name lives in the filename.)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e29_619_memories_ws_ctx_idx"
down_revision: str | Sequence[str] | None = "e28_850_workspace_connectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "idx_memories_ws_ctx"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memories (workspace_id, context_id) "
    "WHERE deleted_at IS NULL"
)


def _index_is_invalid(name: str) -> bool:
    """Return True if ``name`` exists in ``pg_index`` in an INVALID state.

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    with ``indisvalid = false``.  A retry's ``IF NOT EXISTS`` would skip it
    (the name exists) without rebuilding, leaving the cluster permanently
    without a usable index — so the leftover must be dropped first.
    """
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 "
            "FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND i.indisvalid IS FALSE"
        ),
        {"name": name},
    ).first()
    return row is not None


def upgrade() -> None:
    """Build the compound partial index on memories concurrently."""
    invalid = _index_is_invalid(_INDEX_NAME)

    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the compound partial index concurrently."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
