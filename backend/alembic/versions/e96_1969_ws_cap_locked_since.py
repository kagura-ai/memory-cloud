"""#1969: ``workspaces.capacity_locked_since`` — start of the current capacity-over lock.

The capacity-over lock (#1941) is computed at gate time and never stored, so
nothing knows for how long a workspace has been over. The 30-day purge needs
that clock. The daily sweep (``services/capacity_purge.py``) stamps this column
when the lock is on and clears it when the lock is off; the purge selects
``capacity_locked_since <= now - 30 days``.

``workspaces.capacity_purge_final_notice_at``: when the day-29 "1 day left"
mail of the current lock went out. The purge requires it on an earlier calendar
day, so nothing is deleted without the final warning; cleared with the stamp.

``workspaces.capacity_purge_first_notice_at``: when the first countdown mail
of the current lock went out. The purge also requires it at least 7 calendar
days earlier, so a late start of the notices never gives a one-day warning;
cleared with the stamp.

Partial index: the sweep's two reads are "stamped rows" (notices, purge) and
the candidate scan; both are tiny next to the table, and the partial index
keeps the stamped-row read an index scan whatever the table size.

Revision ID: e96_1969_ws_cap_locked_since
Revises: e95_1951_memories_ws_not_null
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e96_1969_ws_cap_locked_since"
down_revision: str | None = "e95_1951_memories_ws_not_null"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("capacity_locked_since", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("capacity_purge_final_notice_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("capacity_purge_first_notice_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_workspaces_capacity_locked_since",
        "workspaces",
        ["capacity_locked_since"],
        unique=False,
        postgresql_where=sa.text("capacity_locked_since IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_capacity_locked_since", table_name="workspaces")
    op.drop_column("workspaces", "capacity_purge_first_notice_at")
    op.drop_column("workspaces", "capacity_purge_final_notice_at")
    op.drop_column("workspaces", "capacity_locked_since")
