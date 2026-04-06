"""Add 'rolled_back' to sleep_reports status CHECK constraint.

Issue #164: Sleep Maintenance observability + rollback API.
Allows marking rolled-back reports to prevent double rollback.

Revision ID: a93_rolled_back_status
Revises: a92_context_sleep_mode
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a93_rolled_back_status"
down_revision = "a92_context_sleep_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Widen status CHECK constraint to include 'rolled_back'."""
    op.drop_constraint("valid_sleep_report_status", "sleep_reports", type_="check")
    op.create_check_constraint(
        "valid_sleep_report_status",
        "sleep_reports",
        "status IN ('running', 'completed', 'failed', 'cancelled', 'rolled_back')",
    )


def downgrade() -> None:
    """Restore original status CHECK constraint."""
    op.drop_constraint("valid_sleep_report_status", "sleep_reports", type_="check")
    op.create_check_constraint(
        "valid_sleep_report_status",
        "sleep_reports",
        "status IN ('running', 'completed', 'failed', 'cancelled')",
    )
