"""add password and MFA columns to users

Revision ID: a51_password_mfa
Revises: 2f53be30e6d6
Create Date: 2026-03-30

Issue #51: Password + MFA login for initial admin.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a51_password_mfa"
down_revision: str | Sequence[str] | None = "2f53be30e6d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add password authentication and MFA columns to users table."""
    op.add_column("users", sa.Column("login_id", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("auth_method", sa.String(20), nullable=False, server_default="oauth"),
    )
    op.add_column("users", sa.Column("totp_secret", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.create_index("ix_users_login_id", "users", ["login_id"], unique=True)
    op.create_index("ix_users_auth_method", "users", ["auth_method"])

    op.create_check_constraint("valid_auth_method", "users", "auth_method IN ('password', 'oauth')")


def downgrade() -> None:
    """Remove password authentication and MFA columns."""
    op.drop_constraint("valid_auth_method", "users", type_="check")
    op.drop_index("ix_users_auth_method", table_name="users")
    op.drop_index("ix_users_login_id", table_name="users")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "auth_method")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "login_id")
