"""Operation history retention add-on (#2739).

Entitled workspaces keep their operation history for 90 days
(``config.retention.OPERATION_HISTORY_RETENTION_DAYS``). This adds the
``addon_history_retention_days_bonus`` cache column on ``workspaces`` (days
added to that base, default 0) and extends the ``workspace_addons.addon_type``
CHECK to allow ``extra_history_retention_days`` (+1 day per unit, see
``ADDON_UNIT_VALUES``). Mirrors e97_1975_memories_per_day_addon.

The CHECK is re-added ``NOT VALID`` and validated in a transaction of its own
(``validate_constraint``), so the steps before it are re-runnable. It is a
plain string literal so the schema-drift detector (tests/test_schema_drift.py)
can read the addon_type set.

Revision ID: f34_2739_history_retention_addon
Revises: f33_2739_operation_history
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import validate_constraint

revision = "f34_2739_history_retention_addon"
down_revision = "f33_2739_operation_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "addon_history_retention_days_bonus",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        if_not_exists=True,
    )
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        "ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type "
        "CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota', "
        "'extra_rest_quota', 'extra_public_quota', 'extra_members', 'extra_contexts', "
        "'extra_analysis_runs', 'extra_sleep_contexts', 'extra_connectors', "
        "'extra_memories_per_day', 'extra_history_retention_days')) NOT VALID"
    )
    validate_constraint("workspace_addons", "check_addon_type")


def downgrade() -> None:
    # Rows of the new type would violate the narrower CHECK: zero the grant or
    # delete them before downgrading (the ADD below fails otherwise).
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        "ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type "
        "CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota', "
        "'extra_rest_quota', 'extra_public_quota', 'extra_members', 'extra_contexts', "
        "'extra_analysis_runs', 'extra_sleep_contexts', 'extra_connectors', "
        "'extra_memories_per_day'))"
    )
    op.drop_column("workspaces", "addon_history_retention_days_bonus", if_exists=True)
