"""Add workspace_ownership_force_transfer_requests for dual-control (#1113).

Optional four-eyes approval for the break-glass ownership force-transfer
(#1101). When ``require_dual_control_force_transfer`` is enabled, an initiating
system admin files a ``pending`` request in this table and a second, distinct
system admin must approve it before the transfer commits. Default config keeps
the immediate single-control behavior, so this table is unused there.

``ownership_epoch_at_initiation`` snapshots the workspace epoch so approval can
reject a stale request whose workspace ownership moved since it was filed. The
partial-unique index enforces at most one ``pending`` request per workspace (a
fresh initiate supersedes any prior pending one).

Revision ID: e48_1113_dual_control
Revises: e47_1101_member_unique
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "e48_1113_dual_control"
down_revision = "e47_1101_member_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_ownership_force_transfer_requests",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("target_user_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ownership_epoch_at_initiation", sa.Integer(), nullable=False),
        sa.Column("initiated_by_user_id", sa.String(length=255), nullable=False),
        sa.Column("initiated_by_email", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("decided_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("decided_by_email", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_force_transfer_request_workspace_id",
        "workspace_ownership_force_transfer_requests",
        ["workspace_id"],
    )
    op.create_index(
        "ix_force_transfer_request_created_at",
        "workspace_ownership_force_transfer_requests",
        ["created_at"],
    )
    # At most one PENDING request per workspace (the supersede / unblock invariant).
    op.create_index(
        "uq_force_transfer_request_one_pending_per_workspace",
        "workspace_ownership_force_transfer_requests",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_force_transfer_request_one_pending_per_workspace",
        table_name="workspace_ownership_force_transfer_requests",
    )
    op.drop_index(
        "ix_force_transfer_request_created_at",
        table_name="workspace_ownership_force_transfer_requests",
    )
    op.drop_index(
        "ix_force_transfer_request_workspace_id",
        table_name="workspace_ownership_force_transfer_requests",
    )
    op.drop_table("workspace_ownership_force_transfer_requests")
