"""Isolated first-party OIDC clients, login grants and hashed credentials.

Revision ID: f36_2781_oidc_provider
Revises: f35_1759_channel_routes
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f36_2781_oidc_provider"
down_revision = "f35_1759_channel_routes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create a credential namespace independent of MCP OAuth storage."""
    op.create_table(
        "oidc_clients",
        sa.Column("client_id", sa.String(255), primary_key=True),
        sa.Column("client_secret_hash", sa.String(255), nullable=False),
        sa.Column("client_name", sa.String(255), nullable=False),
        sa.Column("redirect_uris", postgresql.JSONB(), nullable=False),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "oidc_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(255),
            sa.ForeignKey(
                "oidc_clients.client_id", name="fk_oidc_grants_client_id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(255),
            sa.ForeignKey("users.user_id", name="fk_oidc_grants_user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("nonce", sa.String(255), nullable=False),
        sa.Column("auth_time", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("client_id", "nonce", name="uq_oidc_grants_client_nonce"),
        sa.CheckConstraint("auth_time >= 0", name="ck_oidc_grants_auth_time"),
        sa.CheckConstraint("expires_at > created_at", name="ck_oidc_grants_lifetime"),
    )
    op.create_index("ix_oidc_grants_user_id", "oidc_grants", ["user_id"])
    op.create_index("ix_oidc_grants_expires_at", "oidc_grants", ["expires_at"])
    op.create_table(
        "oidc_authorization_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column(
            "grant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "oidc_grants.id", name="fk_oidc_authorization_codes_grant_id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("code_challenge_method", sa.String(10), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("length(code_hash) = 64", name="ck_oidc_codes_hash_length"),
        sa.CheckConstraint("code_challenge_method = 'S256'", name="ck_oidc_codes_pkce_s256"),
    )
    op.create_index(
        "ix_oidc_authorization_codes_grant_id", "oidc_authorization_codes", ["grant_id"]
    )
    op.create_index(
        "ix_oidc_authorization_codes_expires_at", "oidc_authorization_codes", ["expires_at"]
    )
    op.create_table(
        "oidc_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "grant_id",
            sa.Uuid(),
            sa.ForeignKey("oidc_grants.id", name="fk_oidc_tokens_grant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("access_token_hash", sa.String(64), nullable=False),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(512), nullable=False),
        sa.Column("issued_at", sa.Integer(), nullable=False),
        sa.Column("expires_in", sa.Integer(), nullable=False),
        sa.Column("refresh_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("access_token_hash", name="oidc_tokens_access_token_hash_key"),
        sa.UniqueConstraint("refresh_token_hash", name="oidc_tokens_refresh_token_hash_key"),
        sa.CheckConstraint(
            "length(access_token_hash) = 64", name="ck_oidc_tokens_access_hash_length"
        ),
        sa.CheckConstraint(
            "length(refresh_token_hash) = 64", name="ck_oidc_tokens_refresh_hash_length"
        ),
        sa.CheckConstraint("issued_at >= 0", name="ck_oidc_tokens_issued_at"),
        sa.CheckConstraint("expires_in > 0", name="ck_oidc_tokens_expires_in"),
    )
    op.create_index("ix_oidc_tokens_grant_id", "oidc_tokens", ["grant_id"])


def downgrade() -> None:
    """Remove only OIDC credentials, preserving all legacy MCP OAuth rows."""
    op.drop_table("oidc_tokens")
    op.drop_table("oidc_authorization_codes")
    op.drop_table("oidc_grants")
    op.drop_table("oidc_clients")
