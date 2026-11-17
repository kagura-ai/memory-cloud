"""Opt-in ordering of canonical plan and finite integration entitlements.

Revision ID: f54_2882_ordered_billing
Revises: f53_2810_sync_activity
"""

import sqlalchemy as sa

from alembic import op

revision = "f54_2882_ordered_billing"
down_revision = "f53_2810_sync_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_integration_states",
        sa.Column("ordered_billing", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("""
      CREATE FUNCTION guard_ordered_billing_mode() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog AS $$
      BEGIN
        IF OLD.ordered_billing AND
          (NOT NEW.ordered_billing OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id)
        THEN RAISE EXCEPTION 'Ordered billing mode cannot be cleared' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END $$;
    """)
    op.execute("""CREATE TRIGGER ordered_billing_mode_guard
      BEFORE UPDATE ON workspace_integration_states
      FOR EACH ROW EXECUTE FUNCTION guard_ordered_billing_mode()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER ordered_billing_mode_guard ON workspace_integration_states")
    op.execute("DROP FUNCTION guard_ordered_billing_mode()")
    op.drop_column("workspace_integration_states", "ordered_billing")
