"""#1869: partial index on memories.summary_embedding_id for the shared-point check.

A resource-ingested memory's vector point id is ``uuid5(resource:doc:version)``
and lives in ``summary_embedding_id`` (#1829): the same document indexed into
two contexts that share a collection is ONE point for TWO rows. Before
deleting such a point, ``MemoryService._delete_memory_point`` asks whether
another live row still names it::

    SELECT EXISTS (SELECT 1 FROM memories
                   WHERE summary_embedding_id = $1 AND id <> $2
                     AND deleted_at IS NULL)

The check runs once per row from forget by id, forget by query and the
working-memory cleanup, and it cannot be scoped by ``context_id`` — the row it
looks for is in another context. ``summary_embedding_id`` had no index, so
each check was a sequential scan of ``memories``.

``idx_memories_summary_embedding_live`` is a B-tree on
``(summary_embedding_id)``, partial on ``deleted_at IS NULL AND
summary_embedding_id IS NOT NULL``: only live rows that have a point carry it,
which is the predicate of the query above.

Blue-green safety: ``memories`` is a large, high-write table, so the build
follows the b02/e26/e29/e62 pattern — ``CREATE INDEX CONCURRENTLY IF NOT
EXISTS`` inside ``autocommit_block`` (Alembic wraps migrations in a
transaction by default; ``CONCURRENTLY`` cannot run inside one), with an
INVALID-leftover guard so a retry after a mid-build failure rebuilds cleanly.
The index only adds a read path: code from before this revision runs
unchanged against it.

Revision ID: e94_1869_summary_emb_idx
Revises: e93_1852_embedding_attempted_at

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e94_1869_summary_emb_idx"
down_revision: str | Sequence[str] | None = "e93_1852_embedding_attempted_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "idx_memories_summary_embedding_live"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memories (summary_embedding_id) "
    "WHERE deleted_at IS NULL AND summary_embedding_id IS NOT NULL"
)


def _index_is_invalid(name: str) -> bool:
    """Return True if ``name`` exists in ``pg_index`` in an INVALID state.

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index
    row with ``indisvalid = false``. A retry's ``IF NOT EXISTS`` would skip
    it (the name exists) without rebuilding — so the leftover must be
    dropped first. Same guard as e29_619 / e62_1245.
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
    """Build the partial summary_embedding_id index concurrently."""
    invalid = _index_is_invalid(_INDEX_NAME)

    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the partial summary_embedding_id index concurrently."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
