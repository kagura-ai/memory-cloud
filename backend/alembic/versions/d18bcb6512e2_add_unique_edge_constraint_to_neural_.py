"""add unique_edge constraint to neural_memory_edges

Revision ID: d18bcb6512e2
Revises: d001_seed
Create Date: 2026-03-29

Idempotent: checks if constraint exists before creating.
Required for ON CONFLICT ON CONSTRAINT unique_edge in edge upsert.
"""

from alembic import op


revision: str = "d18bcb6512e2"
down_revision: str = "d001_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add unique_edge constraint if missing."""
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'unique_edge'
            ) THEN
                ALTER TABLE neural_memory_edges
                ADD CONSTRAINT unique_edge UNIQUE (user_id, src_id, dst_id);
            END IF;
        END $$;
    """)


def downgrade() -> None:
    """Remove unique_edge constraint."""
    op.drop_constraint("unique_edge", "neural_memory_edges", type_="unique")
