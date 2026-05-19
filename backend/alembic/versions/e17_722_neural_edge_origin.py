"""Add origin discriminator to neural_memory_edges (Issue #722).

Adds an ``origin`` column to distinguish how each edge was created:

* ``hebbian``  — runtime co-activation trace (subject to decay/prune)
* ``semantic`` — sleep edge_discovery's cosine-similarity find (decay-exempt)
* ``declared`` — user-asserted link (decay-exempt)

Default ``'hebbian'`` correctly classifies every existing row (all pre-issue
edges are co-activation traces), so no backfill is needed. The downstream
decay carve-out lands in a subsequent task in the same PR.

Revision string length: 26 chars (well within ``alembic_version.version_num``
VARCHAR(32)).

Revision ID: e17_722_neural_edge_origin
Revises: e16_709_embedding_spend_cap
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa

revision = "e17_722_neural_edge_origin"
down_revision = "e16_709_embedding_spend_cap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "neural_memory_edges",
        sa.Column(
            "origin",
            sa.String(length=20),
            nullable=False,
            server_default="hebbian",
        ),
    )
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE neural_memory_edges
                ADD CONSTRAINT valid_edge_origin
                CHECK (origin IN ('hebbian', 'semantic', 'declared'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.create_index(
        "idx_edges_origin",
        "neural_memory_edges",
        ["origin"],
    )


def downgrade() -> None:
    op.drop_index("idx_edges_origin", table_name="neural_memory_edges")
    op.execute("ALTER TABLE neural_memory_edges DROP CONSTRAINT IF EXISTS valid_edge_origin")
    op.drop_column("neural_memory_edges", "origin")
