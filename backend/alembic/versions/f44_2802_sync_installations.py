"""Workspace-owned synchronization consent and fixed dedicated target bindings.

Revision ID: f44_2802_sync_installations
Revises: f43_2798_paid_integrations
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f44_2802_sync_installations"
down_revision = "f43_2798_paid_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_services",
        sa.Column("service_id", sa.String(64), primary_key=True),
        sa.Column("browser_client_id", sa.String(128), nullable=False),
        sa.Column("service_client_id", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("browser_client_id"),
        sa.UniqueConstraint("service_client_id"),
        sa.CheckConstraint("generation > 0", name="ck_sync_service_generation"),
        if_not_exists=True,
    )
    op.create_table(
        "sync_authorizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", sa.String(128), nullable=False),
        sa.Column(
            "service_id", sa.String(64), sa.ForeignKey("sync_services.service_id"), nullable=False
        ),
        sa.Column("service_generation", sa.Integer(), nullable=False),
        sa.Column("browser_client_id", sa.String(128), nullable=False),
        sa.Column("service_client_id", sa.String(128), nullable=False),
        sa.Column("operations", postgresql.JSONB(), nullable=False),
        sa.Column(
            "authorized_by",
            sa.String(255),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ownership_epoch", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("public_name", sa.String(200), nullable=False),
        sa.Column("public_description", sa.Text(), nullable=True),
        sa.Column("public_usage_guide", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("workspace_id", "request_id", name="uq_sync_authorization_request"),
        sa.CheckConstraint("ownership_epoch >= 0", name="ck_sync_authorization_epoch"),
        sa.CheckConstraint(
            "service_generation > 0", name="ck_sync_authorization_service_generation"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_sync_authorization_window"),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_sync_authorization_hash"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_sync_authorizations_workspace_id",
        "sync_authorizations",
        ["workspace_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_sync_authorizations_application_id",
        "sync_authorizations",
        ["application_id"],
        if_not_exists=True,
    )
    op.create_table(
        "sync_installations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id", sa.String(64), sa.ForeignKey("sync_services.service_id"), nullable=False
        ),
        sa.Column("service_client_id", sa.String(128), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", sa.String(128), nullable=False),
        sa.Column("operations", postgresql.JSONB(), nullable=False),
        sa.Column("resource_pk", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("workspace_id"),
        sa.UniqueConstraint("authorization_id"),
        sa.CheckConstraint("generation > 0", name="ck_sync_installation_generation"),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'revoked')", name="ck_sync_installation_state"
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    # Only control records are removed. Resources, contexts, search config,
    # approved publications and the established audit trail remain intact.
    op.drop_table("sync_installations", if_exists=True)
    op.drop_table("sync_authorizations", if_exists=True)
    op.drop_table("sync_services", if_exists=True)
