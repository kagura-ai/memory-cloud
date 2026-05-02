"""Add cancellation_reason column + composite (analysis_id, cluster_id) index (#496).

Issue #496 (B3 of the Broadlistening v1 atomic split): the API + MCP
exposure layer needs two schema additions on top of #494's base tables:

1. ``memory_analyses.cancellation_reason TEXT NULL`` — populated when
   the DELETE soft-cancel endpoint flips ``status`` from ``running`` to
   ``cancelled``. Distinct from ``error`` so the future taxonomy
   (``user`` / ``admin`` / ``timeout`` / ``cost_cap``) can branch
   without overloading the failure column.

2. ``idx_memory_analysis_assignments_analysis_cluster`` on
   ``(analysis_id, cluster_id)`` — supports the
   ``recall(filters={"analysis_cluster": ...})`` filter chain that
   does ``WHERE analysis_id = :run_id AND cluster_id = :cid``.
   The existing single-column ``idx_memory_analysis_assignments_cluster``
   is kept (used by the reporter at write time to look up centroid
   neighborhoods) — the new composite index narrows the recall lookup
   without invalidating that path. Composite vs swap was the right
   call: dropping the single-column index would force the reporter
   to scan ``cluster_id`` via the composite, paying for the
   ``analysis_id`` prefix it doesn't filter on.

Both changes are catalog-only on PostgreSQL >= 11 — adding a NULL
column without a default is a metadata change, and ``CREATE INDEX``
with the default ``CONCURRENTLY=False`` is a brief ACCESS EXCLUSIVE
lock; cluster row counts are bounded (Pro daily quota is 3 runs,
each with O(sqrt(memories)) clusters), so the lock window is sub-
second on production-sized tables.

Revision ID: d08_496_analyses_cancel
Revises: d07_495_cluster_label_phase

NOTE: Revision IDs are capped at 32 chars because
``alembic_version.version_num`` is ``VARCHAR(32)`` in this database.
This migration uses ``d08_496_analyses_cancel`` (24 chars) — well
within the cap. The Python filename is allowed to be longer.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d08_496_analyses_cancel"
down_revision = "d07_495_cluster_label_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply the schema changes."""
    # 1. memory_analyses.cancellation_reason — NULL for non-cancelled rows.
    op.add_column(
        "memory_analyses",
        sa.Column(
            "cancellation_reason",
            sa.Text(),
            nullable=True,
            comment=(
                "Human-readable reason when status='cancelled'. "
                "NULL for running/succeeded/failed rows. Issue #496."
            ),
        ),
    )

    # 2. Composite index for the recall analysis_cluster filter
    #    (`WHERE analysis_id = ? AND cluster_id = ?` lookups).
    op.create_index(
        "idx_memory_analysis_assignments_analysis_cluster",
        "memory_analysis_assignments",
        ["analysis_id", "cluster_id"],
    )


def downgrade() -> None:
    """Reverse the schema changes."""
    op.drop_index(
        "idx_memory_analysis_assignments_analysis_cluster",
        table_name="memory_analysis_assignments",
    )
    op.drop_column("memory_analyses", "cancellation_reason")
