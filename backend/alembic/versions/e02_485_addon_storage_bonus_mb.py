"""Add addon_storage_bonus_mb column to workspaces (#485 prerequisite).

Issue #485: Platform-managed R2 object storage. Phase 1 prerequisite —
``AddonCalculatorService.recalculate_workspace_bonuses()`` already writes
``workspace.addon_storage_bonus_mb``, but the column is missing from the
``Workspace`` ORM model and the database. Any workspace with an
``extra_storage`` addon row hits ``AttributeError`` on persist, blocking
storage quota machinery.

This migration adds the column with ``NOT NULL DEFAULT 0`` so existing
rows are backfilled without table rewrite (PostgreSQL >= 11).

The ``check_addon_type`` constraint on ``workspace_addons`` already
accepts ``'extra_storage'`` (added in 2f53be30e6d6) so no constraint
change is needed here.

Revision ID: e02_485_addon_storage_mb
Revises: e01_546_cache_write_pricing
"""

from alembic import op

revision = "e02_485_addon_storage_mb"
down_revision = "e01_546_cache_write_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``addon_storage_bonus_mb`` to ``workspaces`` (idempotent)."""
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'addon_storage_bonus_mb'
            ) THEN
                ALTER TABLE workspaces
                  ADD COLUMN addon_storage_bonus_mb INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop ``addon_storage_bonus_mb`` from ``workspaces``."""
    op.drop_column("workspaces", "addon_storage_bonus_mb")
