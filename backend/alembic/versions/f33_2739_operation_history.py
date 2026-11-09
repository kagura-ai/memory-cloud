"""Workspace operation history (#2739), without historical backfill.

Revision ID: f33_2739_operation_history
Revises: f31_2724_event_dates
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f33_2739_operation_history"
down_revision = "f31_2724_event_dates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_operation_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_id", postgresql.UUID(as_uuid=True)),
        sa.Column("context_private", sa.Boolean(), nullable=False),
        sa.Column("context_owner_id", sa.String(255)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.String(255)),
        sa.Column("subject_user_id", sa.String(255)),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("surface", sa.String(10), nullable=False),
        sa.Column("operation", sa.String(40), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(255)),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("count > 0", name="operation_history_positive_count"),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure', 'denied')", name="operation_history_outcome"
        ),
        sa.CheckConstraint(
            "surface IN ('web', 'api', 'mcp', 'system', 'unknown')",
            name="operation_history_surface",
        ),
    )
    for name, columns in (
        ("workspace_time", ["workspace_id", "occurred_at", "id"]),
        ("context", ["workspace_id", "context_id", "occurred_at"]),
        ("actor", ["actor_user_id"]),
        ("subject", ["subject_user_id"]),
        ("context_owner", ["context_owner_id"]),
    ):
        op.create_index("idx_operation_history_" + name, "workspace_operation_history", columns)


def downgrade() -> None:
    op.drop_table("workspace_operation_history")
