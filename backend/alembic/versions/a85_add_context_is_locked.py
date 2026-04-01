"""Add is_locked column to contexts table.

Issue #85: Context lock to prevent accidental deletion.
When is_locked=true, DELETE operations are blocked until unlocked.

Revision ID: a85_context_lock
"""

import sqlalchemy as sa
from alembic import op

revision = "a85_context_lock"
down_revision = "a76_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contexts",
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("contexts", "is_locked")
