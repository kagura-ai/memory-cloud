"""Drop the vestigial workspaces.stripe_* columns (#1096).

The in-process Stripe path is retired (#1096) and the backend is Stripe-agnostic.
``stripe_customer_id`` / ``stripe_subscription_id`` were written only by the
removed in-process checkout path (``BILLING_ENABLED`` has been false in prod
throughout), so they are always NULL — no data is lost. The external payment
service now owns the Stripe customer lifecycle, so memory-cloud has no reason to
store Stripe identifiers again; dropping the columns (rather than the phased
keep-nullable) removes the dead half-state.

Revision ID: e49_1096_drop_stripe_cols
Revises: e48_1113_dual_control
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e49_1096_drop_stripe_cols"
down_revision = "e48_1113_dual_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("workspaces", "stripe_subscription_id")
    op.drop_column("workspaces", "stripe_customer_id")


def downgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("stripe_subscription_id", sa.String(length=255), nullable=True),
    )
