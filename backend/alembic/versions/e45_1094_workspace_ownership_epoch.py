"""Add workspaces.ownership_epoch (#1094).

Issue #1094: owner-initiated ownership transfer needs a monotonic per-workspace
version that bumps on each transfer, so external sessions / tokens bound to a
previous owner can be invalidated by a downstream consumer. NOT NULL with a
``0`` server_default so every existing row backfills to epoch 0 without a data
migration.

Revision ID: e45_1094_ownership_epoch
Revises: e44_1065_host_arbitration
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e45_1094_ownership_epoch"
down_revision = "e44_1065_host_arbitration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("ownership_epoch", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "ownership_epoch")
