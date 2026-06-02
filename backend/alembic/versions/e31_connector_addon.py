"""Per-workspace ``extra_connectors`` addon (Spec 2026-06-02, Plan 6).

Lets admins grant extra ai-worker connector seats above the plan tier from the
admin /plans workspaces tab. Adds the ``addon_connector_bonus`` cache column on
``workspaces`` and extends the ``workspace_addons.addon_type`` CHECK to allow
``extra_connectors`` (1 seat per unit). Mirrors e06_560 (sleep contexts addon).

Revision ID: e31_connector_addon
Revises: e30_connector_cfg_cols
"""

from alembic import op

revision = "e31_connector_addon"
down_revision = "e30_connector_cfg_cols"
branch_labels = None
depends_on = None

_TYPES_BEFORE = (
    "'extra_storage', 'extra_memory', 'extra_mcp_quota', 'extra_rest_quota', "
    "'extra_public_quota', 'extra_members', 'extra_contexts', 'extra_analysis_runs', "
    "'extra_sleep_contexts'"
)
_TYPES_AFTER = _TYPES_BEFORE + ", 'extra_connectors'"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'addon_connector_bonus'
            ) THEN
                ALTER TABLE workspaces
                  ADD COLUMN addon_connector_bonus INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        f"ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type "
        f"CHECK (addon_type IN ({_TYPES_AFTER}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        f"ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type "
        f"CHECK (addon_type IN ({_TYPES_BEFORE}))"
    )
    # Idempotent drop: mirrors the IF NOT EXISTS guard in upgrade().
    # Note: any extra_connectors WorkspaceAddon rows are left in place (their
    # addon_type is now outside the CHECK constraint, so they will fail
    # validation on writes but remain as orphans). Operators should delete them
    # manually or zero the bonus before downgrading.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'addon_connector_bonus'
            ) THEN
                ALTER TABLE workspaces DROP COLUMN addon_connector_bonus;
            END IF;
        END $$;
        """
    )
