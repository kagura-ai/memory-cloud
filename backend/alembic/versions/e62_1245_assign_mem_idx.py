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

Blue-green safety: pure additive index; old app code is unaffected.

Revision ID: e62_1245_assign_mem_idx
Revises: e61_1240_one_running_uq

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

from alembic import op

# revision identifiers
revision = "e62_1245_assign_mem_idx"
down_revision = "e61_1240_one_running_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_memory_analysis_assignments_memory",
        "memory_analysis_assignments",
        ["memory_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_memory_analysis_assignments_memory",
        table_name="memory_analysis_assignments",
    )
