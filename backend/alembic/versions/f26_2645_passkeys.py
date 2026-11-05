"""Discoverable WebAuthn credentials owned by an account (#2645)."""

import sqlalchemy as sa

from alembic import op

revision = "f26_2645_passkeys"
down_revision = "f25_2644_mfa_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(255),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("aaguid", sa.String(36), nullable=False),
        sa.Column("backup_eligible", sa.Boolean(), nullable=False),
        sa.Column("backup_state", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_webauthn_credentials_user_id", "webauthn_credentials", ["user_id"])


def downgrade() -> None:
    op.drop_table("webauthn_credentials")
