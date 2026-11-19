"""#2918: workspace slot suspension state.

Two new tables, both without foreign keys, and one index:

- ``workspace_slot_states``: one row per owner (their ordered keep-selection,
  when it was saved, since when a selection is required, a version);
- ``workspace_slot_suspensions``: one row per suspended workspace, stamped with
  the owner it was suspended for, a reason (CHECK = ``allowance``) and the
  event that suspended it; indexed by owner.

Nothing is backfilled. No existing table is altered or referenced, so the
revision takes no lock on an existing table: ``CREATE TABLE`` and the index
build lock only the new, empty tables (and the ``alembic_version`` row every
revision updates). It is safe while the previous release serves traffic.
While ``WORKSPACE_SLOT_SUSPENSION=off`` (the default) the triggers, gates,
sweep and account routes run no statement on these tables; account erasure and
user deletion delete the subject's rows only when the tables exist. Code and
migration may therefore be deployed in either order.

``if_not_exists`` makes a rerun after an interrupted upgrade a no-op.

Downgrade drops both tables and every suspension with them: run it only with
``WORKSPACE_SLOT_SUSPENSION=off``.

Revision ID: f56_2918_workspace_slots
Revises: f55_2916_sync_run_retention
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f56_2918_workspace_slots"
down_revision: str | None = "f55_2916_sync_run_retention"
branch_labels: str | None = None
depends_on: str | None = None

_OWNER_INDEX = "ix_workspace_slot_suspensions_owner_user_id"


def upgrade() -> None:
    """Create the two slot tables and the owner index (no existing table touched)."""
    op.create_table(
        "workspace_slot_states",
        sa.Column("owner_user_id", sa.String(255), nullable=False),
        sa.Column(
            "keep_workspace_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("selection_saved_at", sa.DateTime(), nullable=True),
        sa.Column("selection_required_since", sa.DateTime(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("owner_user_id"),
        if_not_exists=True,
    )
    op.create_table(
        "workspace_slot_suspensions",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False, server_default="allowance"),
        sa.Column("cause", sa.String(32), nullable=False),
        sa.Column("suspended_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id"),
        sa.CheckConstraint("reason = 'allowance'", name="ck_workspace_slot_suspension_reason"),
        if_not_exists=True,
    )
    op.create_index(
        _OWNER_INDEX,
        "workspace_slot_suspensions",
        ["owner_user_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop both tables (every suspension goes with them; mode ``off`` only)."""
    op.drop_index(_OWNER_INDEX, table_name="workspace_slot_suspensions", if_exists=True)
    op.drop_table("workspace_slot_suspensions", if_exists=True)
    op.drop_table("workspace_slot_states", if_exists=True)
