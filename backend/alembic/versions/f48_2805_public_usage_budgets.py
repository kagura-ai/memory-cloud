"""Workspace-wide seven-day budget of anonymous public calls (#2805).

One row per workspace that has made a counted public call: the running
period's start, its count and its number. CREATE TABLE only; no data.

Downgrade drops the table: every workspace's counter and period are lost,
and each workspace starts a fresh period at its next counted call.
"""

import sqlalchemy as sa

from alembic import op

revision = "f48_2805_public_usage_budgets"
down_revision = "f47_2819_merge_sync_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_usage_budgets",
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workspaces.id", ondelete="CASCADE", name="fk_public_usage_budgets_workspace_id"
            ),
            primary_key=True,
        ),
        sa.Column("period_started_at", sa.DateTime(), nullable=False),
        sa.Column("calls_used", sa.Integer(), nullable=False),
        sa.Column("period_seq", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("calls_used >= 0", name="ck_public_usage_budgets_calls_used"),
        sa.CheckConstraint("period_seq >= 1", name="ck_public_usage_budgets_period_seq"),
    )


def downgrade() -> None:
    """Drop the table: counters and periods are lost (fresh periods afterwards)."""
    op.drop_table("public_usage_budgets")
