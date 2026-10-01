"""#1769: known-device table for new-device sign-in alerts.

``user_known_devices`` holds, per account, the keyed HMAC of each browser's
device cookie with ``first_seen`` / ``last_seen``. A sign-in whose hash is not
in the table emails the owner. Rows cascade with the account; the daily
retention job deletes rows whose ``last_seen`` is older than
``known_device_retention_days`` (``ix_user_known_devices_last_seen``).

Downgrade drops the table.

Revision ID: e89_1769_known_devices
Revises: e88_1752_verified_backfill

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e89_1769_known_devices"
down_revision: str | None = "e88_1752_verified_backfill"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "user_known_devices",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.String(255),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_hash", sa.CHAR(64), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "device_hash", name="user_known_devices_user_device_key"),
    )
    op.create_index("ix_user_known_devices_last_seen", "user_known_devices", ["last_seen"])


def downgrade() -> None:
    op.drop_index("ix_user_known_devices_last_seen", table_name="user_known_devices")
    op.drop_table("user_known_devices")
