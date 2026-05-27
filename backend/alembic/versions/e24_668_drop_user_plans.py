"""Drop the legacy ``user_plans`` table (#668).

Issue #668: the ``user_plans`` table (``models.auth.UserPlan``, created in
the baseline migration for #48) was a user-level subscription plan store.
It was never wired into Stripe billing — ``stripe_service._apply_plan_change``
only updates ``Workspace.plan_name`` — so every row stayed at the default
``'free'`` tier with fixed default quota values forever. The canonical plan
source of truth is the per-workspace ``Workspace`` model (plan_name +
``effective_*`` quota properties); user-level scope is limited to the
owned-workspace slot cap (``users.workspace_slot_bonus``, #674/#675).

With this migration the last readers/writers are gone:
- ``GET /usage/current`` now sources its ``plan`` block from the FREE plan
  tier (``config.plan_tiers``) instead of the ``user_plans`` row, preserving
  the table's always-FREE behavior.
- ``auth.roles.RoleManager._ensure_user_postgres`` no longer inserts a
  default row on user creation.
- ``account_erasure_service`` no longer deletes ``user_plans`` rows.

### Downgrade

``downgrade()`` recreates the empty table + index so the schema matches the
pre-#668 baseline. The legacy rows are NOT restored — they carried only the
fixed ``'free'`` defaults and are unrecoverable; the pre-#668 application
code would regenerate a default row per user on next login anyway.

Revision ID: e24_668_drop_user_plans
Revises: e23_799_normalize_addon_caches
"""

import sqlalchemy as sa

from alembic import op

revision = "e24_668_drop_user_plans"
down_revision = "e23_799_normalize_addon_caches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_user_plans_plan_name", table_name="user_plans")
    op.drop_table("user_plans")


def downgrade() -> None:
    op.create_table(
        "user_plans",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("plan_name", sa.String(length=50), nullable=False),
        sa.Column("memory_limit", sa.Integer(), nullable=False),
        sa.Column("daily_api_limit", sa.Integer(), nullable=False),
        sa.Column("weekly_api_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index("idx_user_plans_plan_name", "user_plans", ["plan_name"], unique=False)
