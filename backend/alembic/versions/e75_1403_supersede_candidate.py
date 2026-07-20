"""Add memories.supersede_candidate (server-only supersede suggestion).

Issue #1403 (option B): the k-NN cold-start seeding detects, at ingest, when a
new memory's top-1 neighbor is a near-duplicate (cosine >= the supersede-suggest
threshold) — i.e. the new memory likely supersedes an existing fact. That
suggestion is stored here and surfaced, liveness-guarded, on recall()/reference()
so a client can offer a confirm -> create_edge(edge_type="supersedes") action
(never auto-created — the #1208 over-supersede prevention stays intact).

A DEDICATED column rather than a key inside the client-writable ``details`` JSON:
storing it in ``details`` would let a client forge the ``memory_id`` (leaking
another memory's summary through the read-path enrichment, bypassing the
per-memory access check) or silently drop it on a ``details`` replace. This column
is written only by the server (the async k-NN seeding) and cleared only when the
suggested supersedes edge is created.

Additive + nullable: existing rows read as NULL (no suggestion), no backfill.

Revision ID: e75_1403_supersede_candidate
Revises: e74_1401_mae_explore
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e75_1403_supersede_candidate"
down_revision = "e74_1401_mae_explore"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable supersede_candidate JSON column to memories."""
    op.add_column(
        "memories",
        sa.Column("supersede_candidate", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Drop the supersede_candidate column."""
    op.drop_column("memories", "supersede_candidate")
