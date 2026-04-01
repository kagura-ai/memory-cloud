"""Add 'processing' to embedding_status check constraint.

Issue #76: async remember uses 'processing' as an intermediate status
to prevent race conditions between create_task and sweep.

Revision ID: a76_processing
"""

from alembic import op

revision = "a76_processing"
down_revision = "a51_password_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("valid_embedding_status", "memories", type_="check")
    op.create_check_constraint(
        "valid_embedding_status",
        "memories",
        "embedding_status IN ('pending', 'processing', 'success', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("valid_embedding_status", "memories", type_="check")
    op.create_check_constraint(
        "valid_embedding_status",
        "memories",
        "embedding_status IN ('pending', 'success', 'failed')",
    )
