"""#3014: a context's user facts lane switch on ``context_search_configs``.

Adds ``user_facts_lane`` (BOOLEAN NOT NULL DEFAULT false). No backfill
UPDATE: every existing row reads ``false`` through the constant default
(metadata-only on PostgreSQL), so no recall changes until an owner turns the
lane on.

Locks: ``ADD COLUMN`` takes a brief ``ACCESS EXCLUSIVE`` lock on
``context_search_configs`` only. No other table is locked (the foreign key to
``contexts`` is not touched).

Order: run this migration BEFORE starting the new code. The new code selects
and inserts the column on every search-config read and context creation. The
previous release never names the column (its INSERTs get ``false`` from the
server default and its SELECTs list their columns), so it keeps working
against the migrated schema. Re-runnable (``IF NOT EXISTS`` / ``IF EXISTS``).

Downgrade drops the column; every context then recalls without the lane.

Revision ID: f63_3014_user_facts_lane
Revises: f62_2996_resource_rows_persist
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f63_3014_user_facts_lane"
down_revision: str | None = "f62_2996_resource_rows_persist"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "context_search_configs"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("user_facts_lane", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "user_facts_lane", if_exists=True)
