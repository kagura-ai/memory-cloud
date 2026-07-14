"""Add agent_context_bindings table — subtractive agent scoping (RFC-0002 P0-2, #1275).

Purely subtractive per-context scoping for registered agents: the effective
permission for an agent-bound request is existing RBAC decision ∩ binding
(design contract docs/design/agent-registry-and-bindings.md). Pure additive
migration (class 1): no existing table is touched, blue-green safe. The
``api_keys`` ALTERs ship separately as e65 (migration class 2).

CHECK literal must stay byte-identical to ``_ALL_BINDING_WRITE_POLICIES`` in
``models/agent.py`` (drift pinned by tests/test_agent_constants.py).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e64_1275_agent_bindings"
down_revision = "e63_1274_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_context_bindings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "context_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("can_read", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "write_policy",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'deny'"),
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("allowed_memory_types", postgresql.ARRAY(sa.String(length=50)), nullable=True),
        sa.Column("allowed_source_types", postgresql.ARRAY(sa.String(length=20)), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "write_policy IN ('deny', 'direct')",
            name="valid_binding_write_policy",
        ),
    )
    op.create_index(
        "uq_agent_ctx_binding",
        "agent_context_bindings",
        ["agent_id", "context_id"],
        unique=True,
    )
    # At most one bootstrap default binding per agent.
    op.create_index(
        "uq_agent_ctx_binding_default",
        "agent_context_bindings",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "idx_agent_ctx_binding_context",
        "agent_context_bindings",
        ["context_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_ctx_binding_context", table_name="agent_context_bindings")
    op.drop_index("uq_agent_ctx_binding_default", table_name="agent_context_bindings")
    op.drop_index("uq_agent_ctx_binding", table_name="agent_context_bindings")
    op.drop_table("agent_context_bindings")
