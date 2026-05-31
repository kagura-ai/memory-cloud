"""Drop the vestigial ``workspaces.memory_limit`` column (#805).

Issue #805: ``Workspace.memory_limit`` (``server_default '1000'``) was a
denormalized mirror of ``plan_tier.memory_limit`` with **no live consumer**.
The canonical effective calc (``Workspace.effective_memory_limit``) reads the
plan tier, not the column. The column was only written on plan change (4 paths)
and read for audit-log "old value" snapshots (3 paths). Of the base quota
dimensions, ``memory_limit`` was the lone one mirrored onto a per-workspace
column, and it drifted in prod (2026-05-23: a PRO workspace carrying the stale
FREE value 1000). SSoT is ``plan_tier.memory_limit``.

By this migration the last readers/writers are gone (#801 fixed the admin base
display; #805 removed the 4 writers and re-sourced the 3 audit "old" values from
``get_plan_tier(old_plan).memory_limit`` captured before ``plan_name`` mutates).
``PlanChange.old_memory_limit`` / ``new_memory_limit`` are immutable historical
records and are NOT touched.

``daily_api_limit`` / ``weekly_api_limit`` are a separate "legacy API limit"
concern (they have an ``effective_daily_api_limit`` computation) and are
intentionally left in place — out of #805 scope.

### Downgrade

``downgrade()`` re-adds the column with its original ``server_default '1000'``
and backfills each row from its plan tier (``basic`` → 10000, ``pro`` → 100000,
everything else → 1000) so the restored column is correct rather than uniformly
stale. This is the reversible "re-add + backfill from plan_tier" path.

Revision ID: e27_805_drop_ws_memory_limit
Revises: e26_818_summary_trgm_idx
"""

import sqlalchemy as sa

from alembic import op

# NOTE: revision id kept <= 32 chars — alembic_version.version_num is VARCHAR(32).
revision = "e27_805_drop_ws_memory_limit"
down_revision = "e26_818_summary_trgm_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("workspaces", "memory_limit")


def downgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "memory_limit",
            sa.Integer(),
            nullable=False,
            server_default="1000",
        ),
    )
    # Backfill from the plan tier so the restored column is correct, not
    # uniformly the FREE default. Canonical tier memory_limits: free=1000,
    # basic=10000, pro=100000 (config.plan_tiers).
    op.execute(
        """
        UPDATE workspaces
        SET memory_limit = CASE plan_name
            WHEN 'basic' THEN 10000
            WHEN 'pro' THEN 100000
            ELSE 1000
        END
        """
    )
