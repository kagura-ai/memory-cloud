"""add addon_context_bonus and extra_contexts addon type

Revision ID: 2f53be30e6d6
Revises: d18bcb6512e2
Create Date: 2026-03-29

Issue #15: Allow admins to increase workspace context limits via addon.
"""

from alembic import op

revision: str = "2f53be30e6d6"
down_revision: str = "d18bcb6512e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add addon_context_bonus column and extra_contexts addon type."""
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces' AND column_name = 'addon_context_bonus'
            ) THEN
                ALTER TABLE workspaces ADD COLUMN addon_context_bonus INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$;
    """)

    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute("""
        ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type
        CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota',
               'extra_rest_quota', 'extra_public_quota', 'extra_members', 'extra_contexts'))
    """)


def downgrade() -> None:
    """Remove addon_context_bonus and extra_contexts."""
    op.drop_column("workspaces", "addon_context_bonus")
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute("""
        ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type
        CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota',
               'extra_rest_quota', 'extra_public_quota', 'extra_members'))
    """)
