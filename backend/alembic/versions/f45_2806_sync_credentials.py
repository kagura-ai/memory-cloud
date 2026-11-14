"""Installation-bound service authentication and durable assertion replay receipts.

Revision ID: f45_2806_sync_credentials
Revises: f44_2802_sync_installations
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "f45_2806_sync_credentials"
down_revision = "f44_2802_sync_installations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_credential_clients",
        sa.Column("client_id", sa.String(128), primary_key=True),
        sa.Column(
            "service_id",
            sa.String(64),
            sa.ForeignKey("sync_services.service_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("service_id"),
        sa.CheckConstraint("generation > 0", name="ck_sync_client_generation"),
        if_not_exists=True,
    )
    op.create_table(
        "sync_signing_keys",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(128),
            sa.ForeignKey("sync_credential_clients.client_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kid", sa.String(128), nullable=False),
        sa.Column("public_jwk", pg.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.UniqueConstraint("client_id", "kid", name="uq_sync_key_client_kid"),
        sa.UniqueConstraint("client_id", "fingerprint", name="uq_sync_key_client_material"),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_sync_key_fingerprint"),
        if_not_exists=True,
    )
    op.create_table(
        "sync_assertion_uses",
        sa.Column(
            "client_id",
            sa.String(128),
            sa.ForeignKey("sync_credential_clients.client_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("jti_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(jti_hash) = 64", name="ck_sync_assertion_hash"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_sync_assertion_uses_expires_at",
        "sync_assertion_uses",
        ["expires_at"],
        if_not_exists=True,
    )
    op.create_table(
        "sync_access_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "installation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("sync_installations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            sa.String(64),
            sa.ForeignKey("sync_services.service_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.String(128),
            sa.ForeignKey("sync_credential_clients.client_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "key_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("sync_signing_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column("service_generation", sa.Integer(), nullable=False),
        sa.Column("client_generation", sa.Integer(), nullable=False),
        sa.Column("installation_generation", sa.Integer(), nullable=False),
        sa.Column("resource_pk", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("context_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("operations", pg.JSONB(), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("resource", sa.String(640), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False, server_default="resource.sync"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_sync_access_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_sync_access_window"),
        sa.CheckConstraint(
            "service_generation > 0 AND client_generation > 0 AND installation_generation > 0",
            name="ck_sync_access_generations",
        ),
        sa.CheckConstraint("scope = 'resource.sync'", name="ck_sync_access_scope"),
        if_not_exists=True,
    )
    for column in ("installation_id", "workspace_id", "expires_at"):
        op.create_index(
            "ix_sync_access_tokens_" + column, "sync_access_tokens", [column], if_not_exists=True
        )


def downgrade() -> None:
    for table in (
        "sync_access_tokens",
        "sync_assertion_uses",
        "sync_signing_keys",
        "sync_credential_clients",
    ):
        op.drop_table(table, if_exists=True)
