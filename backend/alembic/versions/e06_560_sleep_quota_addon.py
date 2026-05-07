"""Tier-based quota for sleep-enabled contexts (#560).

Issue #560: Sleep Maintenance is a PRO-only feature with a per-workspace cap
on how many contexts can have ``sleep_mode != 'skip'``. FREE/BASIC = 0,
PRO = 3 (extendable via the ``extra_sleep_contexts`` addon).

This migration:

1. Adds ``workspaces.addon_sleep_contexts_bonus`` (INTEGER NOT NULL DEFAULT 0,
   idempotent — uses the same ``information_schema`` guard as #485 / #15).
2. Extends ``workspace_addons.check_addon_type`` to include
   ``'extra_sleep_contexts'`` so the new SKU can be sold (Phase 2 follow-up;
   no rows of this type exist yet on land day).
3. Force-skips contexts that were grandfathered into ``sleep_mode != 'skip'``
   on FREE/BASIC workspaces — these workspaces never should have been able
   to opt-in to LLM-bearing phases, but pre-#560 there was no quota gate.
   PRO is intentionally untouched (grandfather): the runtime quota check uses
   an "increase-only" rule so existing over-limit PRO workspaces keep their
   current sleep-enabled contexts but cannot enable new ones until they drop
   below the effective limit.

The force-skip data migration is one-way: ``downgrade()`` cannot restore the
original ``full`` / ``edges_only`` values because we did not snapshot them.
This is intentional — a downgrade would also revert the column, and rolling
back to a build that lacks the quota gate while restoring LLM-active rows
would re-enable platform-bearing cost on FREE/BASIC workspaces.

Revision ID: e06_560_sleep_quota_addon
Revises: e05_558_sleep_default_skip
"""

from alembic import op

revision = "e06_560_sleep_quota_addon"
down_revision = "e05_558_sleep_default_skip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add addon column (idempotent — same guard pattern as #485 / #15).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'workspaces'
                  AND column_name = 'addon_sleep_contexts_bonus'
            ) THEN
                ALTER TABLE workspaces
                  ADD COLUMN addon_sleep_contexts_bonus INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )

    # 2. Extend WorkspaceAddon.addon_type CHECK constraint with extra_sleep_contexts.
    #    Mirrors the pattern in 2f53be30e6d6 (extra_contexts).
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        """
        ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type
        CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota',
               'extra_rest_quota', 'extra_public_quota', 'extra_members',
               'extra_contexts', 'extra_analysis_runs', 'extra_sleep_contexts'))
        """
    )

    # 3. Force-skip data migration for FREE/BASIC workspaces.
    #    PRO is left alone (grandfather). Plan names use lowercase strings per
    #    workspaces.valid_plan_name CHECK constraint.
    op.execute(
        """
        UPDATE contexts
        SET sleep_mode = 'skip'
        WHERE sleep_mode != 'skip'
          AND workspace_id IN (
              SELECT id FROM workspaces WHERE plan_name IN ('free', 'basic')
          )
        """
    )


def downgrade() -> None:
    # Reverse-order: restore the CHECK constraint first (so dropped column does
    # not leave dangling references), then drop the column. The force-skip data
    # migration is one-way and intentionally NOT reversed — see module docstring.
    op.execute("ALTER TABLE workspace_addons DROP CONSTRAINT IF EXISTS check_addon_type")
    op.execute(
        """
        ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type
        CHECK (addon_type IN ('extra_storage', 'extra_memory', 'extra_mcp_quota',
               'extra_rest_quota', 'extra_public_quota', 'extra_members',
               'extra_contexts', 'extra_analysis_runs'))
        """
    )
    op.drop_column("workspaces", "addon_sleep_contexts_bonus")
