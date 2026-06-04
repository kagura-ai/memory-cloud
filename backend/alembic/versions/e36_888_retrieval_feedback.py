"""Add retrieval_feedback table — retrieval feedback signal (#888).

A dedicated append-only event log of "was this recalled memory useful for this
query" signals, kept out of ``memories`` so it never pollutes the knowledge
recall space (separate table, never embedded). No unique constraint — feedback
is a time series. Both FKs cascade on delete so feedback is erased with its
context or memory (GDPR/APPI erasure). The (context_id, memory_id) index backs
net-helpful aggregation reads.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e36_888_retrieval_feedback"
down_revision = "e35_889_agent_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retrieval_feedback",
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
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query", sa.String(length=1024), nullable=True),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_retrieval_feedback_context_memory",
        "retrieval_feedback",
        ["context_id", "memory_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_retrieval_feedback_context_memory", table_name="retrieval_feedback")
    op.drop_table("retrieval_feedback")
