"""Per-workspace ``extra_memories_per_day`` addon (#1975).

The daily memory-creation limit (``memories_per_day``, #1549) was tier-fixed:
the only way to raise it was ``PLAN_<TIER>_MEMORIES_PER_DAY`` plus a redeploy,
which moves the whole tier. This adds the ``addon_memories_per_day_bonus``
cache column on ``workspaces`` and extends the ``workspace_addons.addon_type``
CHECK to allow ``extra_memories_per_day`` (+100 memories/day per unit, see
``ADDON_UNIT_VALUES``). Mirrors e31_connector_addon.

NOTE: the CHECK constraint is written as a plain string literal (not an
f-string) so the schema-drift detector (tests/test_schema_drift.py) — which
parses migrations with ``ast.parse`` and cannot evaluate f-strings — can read
the addon_type set and compare it against the ORM ``__table_args__``.

Revision ID: e97_1975_memories_per_day_addon
Revises: e96_1969_ws_cap_locked_since
"""

from alembic import op

revision = "e97_1975_memories_per_day_addon"
down_revision = "e96_1969_ws_cap_locked_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'addon_memories_per_day_bonus'
            ) THEN
                ALTER TABLE workspaces
                  ADD COLUMN addon_memories_per_day_bonus INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        "ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type "
        "CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota', "
        "'extra_rest_quota', 'extra_public_quota', 'extra_members', 'extra_contexts', "
        "'extra_analysis_runs', 'extra_sleep_contexts', 'extra_connectors', "
        "'extra_memories_per_day'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        "ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type "
        "CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota', "
        "'extra_rest_quota', 'extra_public_quota', 'extra_members', 'extra_contexts', "
        "'extra_analysis_runs', 'extra_sleep_contexts', 'extra_connectors'))"
    )
    # Idempotent drop: mirrors the IF NOT EXISTS guard in upgrade(). Any
    # extra_memories_per_day WorkspaceAddon rows are left in place as orphans
    # (outside the CHECK, so they fail on later writes); zero the grant or
    # delete the rows before downgrading.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'addon_memories_per_day_bonus'
            ) THEN
                ALTER TABLE workspaces DROP COLUMN addon_memories_per_day_bonus;
            END IF;
        END $$;
        """
    )
