"""Device Authorization Grant (RFC 8628) — oauth_device_codes table (Issue #536).

Revision ID: d08_536_device_code_grant
Revises: d08_496_analyses_cancellation
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, text

revision: str = "d08_536_device_code_grant"
down_revision: str | Sequence[str] | None = "d08_496_analyses_cancellation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_device_codes",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("device_code", String(255), nullable=False, unique=True),
        Column("user_code", String(8), nullable=False, unique=True),
        Column(
            "client_id",
            String(48),
            nullable=False,
        ),
        Column("user_id", String(255), nullable=True),
        Column("scope", String(255), nullable=True),
        Column("expires_at", DateTime, nullable=False),
        Column("last_polled_at", DateTime, nullable=True),
        Column("denied_at", DateTime, nullable=True),
        Column("authorized_at", DateTime, nullable=True),
        Column(
            "created_at",
            DateTime,
            nullable=False,
            server_default=text("NOW()"),
        ),
    )

    op.create_index(
        "ix_oauth_device_codes_device_code",
        "oauth_device_codes",
        ["device_code"],
    )
    op.create_index(
        "ix_oauth_device_codes_user_code",
        "oauth_device_codes",
        ["user_code"],
    )
    op.create_index(
        "ix_oauth_device_codes_expires_at",
        "oauth_device_codes",
        ["expires_at"],
    )
    op.create_index(
        "ix_oauth_device_codes_client_id",
        "oauth_device_codes",
        ["client_id"],
    )
    op.create_foreign_key(
        "fk_oauth_device_codes_client_id",
        "oauth_device_codes",
        "oauth_clients",
        ["client_id"],
        ["client_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_table("oauth_device_codes")
