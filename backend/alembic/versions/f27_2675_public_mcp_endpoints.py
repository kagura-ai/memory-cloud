"""Anonymous read-only public MCP endpoints bound to one resource context (#2675)."""

import sqlalchemy as sa

from alembic import op

revision = "f27_2675_public_mcp_endpoints"
down_revision = "f26_2645_passkeys"
branch_labels = None
depends_on = None

# Inlined from ``utils.public_id.public_id_server_default(PublicIdPrefix.PUBLIC_MCP)``
# so the migration does not change if the helper does.
_PUBLIC_ID_DEFAULT = "'pmcp_' || substr(replace((gen_random_uuid())::text, '-', ''), 1, 22)"


def upgrade() -> None:
    op.create_table(
        "public_mcp_endpoints",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "public_id",
            sa.String(32),
            nullable=False,
            server_default=sa.text(_PUBLIC_ID_DEFAULT),
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workspaces.id", ondelete="CASCADE", name="fk_public_mcp_endpoints_workspace_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "context_id",
            sa.Uuid(),
            sa.ForeignKey(
                "contexts.id", ondelete="CASCADE", name="fk_public_mcp_endpoints_context_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "resource_pk",
            sa.Uuid(),
            sa.ForeignKey(
                "resources.id", ondelete="CASCADE", name="fk_public_mcp_endpoints_resource_pk"
            ),
            nullable=False,
        ),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("public_name", sa.String(200), nullable=False),
        sa.Column("public_description", sa.Text(), nullable=True),
        sa.Column("public_usage_guide", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(255), nullable=True),
        sa.UniqueConstraint("public_id", name="public_mcp_endpoints_public_id_key"),
    )
    op.create_index(
        "ix_public_mcp_endpoints_workspace_id", "public_mcp_endpoints", ["workspace_id"]
    )
    op.create_index(
        "ux_public_mcp_endpoints_context_active",
        "public_mcp_endpoints",
        ["context_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("public_mcp_endpoints")
