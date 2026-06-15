"""Route lingering ``failed`` file_objects rows into the GC (#962).

Issue #962: ``file_objects`` rows in terminal ``status='failed'`` with
``deleted_at IS NULL`` were reaped by no garbage-collection path. The orphan
sweeper targeted only ``status='reserved'`` and the #552 nightly GC targeted
only ``status='uploaded' AND deleted_at IS NOT NULL``, so ``failed`` rows held
no quota and were UI-hidden but accumulated as dead rows.

The application change in this PR has the orphan sweeper stamp ``deleted_at`` on
any lingering ``failed`` row and broadens the nightly GC to reap
``status IN ('uploaded', 'failed')``. This migration:

1. One-time backfill — soft-delete pre-existing ``failed AND deleted_at IS NULL``
   rows (Phase-1-era + pre-#552 ``confirm_upload`` failures) so the GC reaps them
   without waiting for the next orphan-sweeper tick.
2. Widen ``idx_file_objects_soft_deleted_gc`` to cover ``failed`` so the GC scan
   stays index-backed.

Revision ID: e39_962_failed_file_gc
Revises: e38_982_edge_gate_kind

DOWNGRADE: restores the narrow (uploaded-only) index predicate. The backfill is
forward-only — re-NULLing ``deleted_at`` would resurrect the un-GC'able rows the
upgrade was written to remove, so downgrade intentionally does not undo it.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e39_962_failed_file_gc"
down_revision = "e38_982_edge_gate_kind"
branch_labels = None
depends_on = None

_GC_INDEX = "idx_file_objects_soft_deleted_gc"


def upgrade() -> None:
    """Backfill lingering failed rows + widen the GC index to cover ``failed``."""
    # 1. One-time cleanup: route pre-existing lingering failed rows into the GC.
    op.execute(
        "UPDATE file_objects SET deleted_at = NOW() WHERE status = 'failed' AND deleted_at IS NULL"
    )
    # 2. Widen the GC partial index to match the broadened sweep predicate.
    op.drop_index(_GC_INDEX, table_name="file_objects")
    op.create_index(
        _GC_INDEX,
        "file_objects",
        ["deleted_at"],
        postgresql_where=sa.text("status IN ('uploaded', 'failed') AND deleted_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Restore the narrow uploaded-only index predicate (backfill is forward-only)."""
    op.drop_index(_GC_INDEX, table_name="file_objects")
    op.create_index(
        _GC_INDEX,
        "file_objects",
        ["deleted_at"],
        postgresql_where=sa.text("status = 'uploaded' AND deleted_at IS NOT NULL"),
    )
