"""#1245: index memory_analysis_assignments.memory_id for FK cascade deletes.

``memory_analysis_assignments.memory_id`` carries an ON DELETE CASCADE FK
to ``memories``, but no index leads with ``memory_id`` — the PK is
``(analysis_id, memory_id)`` and the secondary indexes cover only
``cluster_id`` / ``(analysis_id, cluster_id)``. Every hard DELETE of a
``memories`` row therefore fires the RI trigger's
``DELETE FROM memory_analysis_assignments WHERE memory_id = $1`` as a full
sequential scan. Hard deletes are routine (sleep merge-retention purge,
admin user purge, resource re-indexing), and assignment rows accumulate at
~1 per analyzed memory per run, so bulk purges degrade into N table scans
under row locks.

Blue-green safety: the same accumulation argument that motivates the index
means the table can be large, so the build follows the b02/e26/e29 pattern —
``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` inside ``autocommit_block``
(Alembic wraps migrations in a transaction by default; ``CONCURRENTLY``
cannot run inside one), with an INVALID-leftover guard so a retry after a
mid-build failure rebuilds cleanly. A plain transactional ``CREATE INDEX``
would take a SHARE lock that blocks the analysis pipeline's bulk assignment
INSERTs and every memories-delete RI trigger for the build duration.

Revision ID: e62_1245_assign_mem_idx
Revises: e61_1240_one_running_uq

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision: str = "e62_1245_assign_mem_idx"
down_revision: str | Sequence[str] | None = "e61_1240_one_running_uq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "idx_memory_analysis_assignments_memory"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memory_analysis_assignments (memory_id)"
)


def _index_is_invalid(name: str) -> bool:
    """Return True if ``name`` exists in ``pg_index`` in an INVALID state.

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index
    row with ``indisvalid = false``. A retry's ``IF NOT EXISTS`` would skip
    it (the name exists) without rebuilding — so the leftover must be
    dropped first. Same guard as e29_619.
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
    """Build the memory_id FK index concurrently."""
    invalid = _index_is_invalid(_INDEX_NAME)

    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the memory_id FK index concurrently."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
