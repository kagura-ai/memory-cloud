"""Immutable resource management delegation and sanitized operation receipts."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f30_resource_management_grants"
down_revision = "f29_2679_observation_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resource_management_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resource_pk",
            sa.Uuid(),
            sa.ForeignKey("resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "context_id",
            sa.Uuid(),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "endpoint_id",
            sa.Uuid(),
            sa.ForeignKey("public_mcp_endpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("deployment_id", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("operations", postgresql.JSONB(), nullable=False),
        sa.Column("approved_fields", postgresql.JSONB(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("schema_hash", sa.String(64), nullable=False),
        sa.Column("quota_events_per_hour", sa.Integer(), nullable=False),
        sa.Column("max_active_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "quota_events_per_hour BETWEEN 1 AND 10000", name="ck_management_grant_quota"
        ),
        sa.CheckConstraint(
            "max_active_tokens BETWEEN 1 AND 100", name="ck_management_grant_token_cap"
        ),
    )
    op.create_index(
        "ix_resource_management_grants_workspace_id", "resource_management_grants", ["workspace_id"]
    )
    op.create_table(
        "resource_management_operations",
        sa.Column(
            "grant_id",
            sa.Uuid(),
            sa.ForeignKey("resource_management_grants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("outcome", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("resource_management_operations")
    op.drop_table("resource_management_grants")
