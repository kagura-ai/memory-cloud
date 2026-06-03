"""Add agent_states table — agent session-state lane (#889).

A dedicated TTL-bounded key/value store for ephemeral agent run-state, kept
out of ``memories`` so it never pollutes the knowledge recall space. One value
per (context_id, key); the unique index backs the set_state upsert. The partial
expires_at index supports the lazy TTL reap.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e35_889_agent_state"
# Re-chained onto e35_887 (was e34) so the two independently-branched #887/#889
# migrations form a single linear chain instead of two heads needing a merge
# revision (a merge revision breaks `alembic downgrade -1`). The two are
# independent — #887 alters memories/contexts, #889 adds agent_states — so the
# order is arbitrary; neither is deployed yet.
down_revision = "e35_887_provenance_trust_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_states",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "context_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_agent_states_context_key",
        "agent_states",
        ["context_id", "key"],
        unique=True,
    )
    op.create_index(
        "idx_agent_states_expires",
        "agent_states",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_agent_states_expires", table_name="agent_states")
    op.drop_index("uq_agent_states_context_key", table_name="agent_states")
    op.drop_table("agent_states")
