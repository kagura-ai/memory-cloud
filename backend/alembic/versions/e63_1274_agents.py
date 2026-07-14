"""Add agents table — Agent Registry (RFC-0002 P0-1, #1274).

Workspace-scoped registry of AI agents; the anchor for P0-2..P0-5 (bindings,
agent-bound keys, bootstrap, correlation, access events). Pure additive
migration (migration class 1 of docs/design/agent-registry-and-bindings.md):
no existing table is touched, blue-green safe by the house definition. The
``api_keys`` ALTERs are deliberately NOT here — they ship with P0-2 (#1275)
as migration class 2.

CHECK literals must stay byte-identical to the module-level tuples in
``models/agent.py`` (the valid_delivery_mode drift-pin pattern, #886);
drift is pinned by tests/test_agent_constants.py.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e63_1274_agents"
down_revision = "e62_1245_assign_mem_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.String(length=255), nullable=False),
        sa.Column("framework", sa.String(length=100), nullable=True),
        sa.Column("environment", sa.String(length=100), nullable=True),
        sa.Column("version", sa.String(length=100), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "enforcement_mode",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'enforce'"),
        ),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'retired')",
            name="valid_agent_status",
        ),
        sa.CheckConstraint(
            "enforcement_mode IN ('shadow', 'enforce')",
            name="valid_agent_enforcement",
        ),
    )
    op.create_index(
        "uq_agents_workspace_name",
        "agents",
        ["workspace_id", "name"],
        unique=True,
    )
    op.create_index(
        "idx_agents_workspace",
        "agents",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_agents_workspace", table_name="agents")
    op.drop_index("uq_agents_workspace_name", table_name="agents")
    op.drop_table("agents")
