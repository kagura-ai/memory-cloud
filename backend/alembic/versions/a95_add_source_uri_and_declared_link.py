"""Add source_uri/source_type columns and declared_link edge type.

Issues #213, #215:
- Add source_uri (VARCHAR 2048) and source_type (VARCHAR 20) to memories table
- Add partial B-tree index on source_uri WHERE source_uri IS NOT NULL
- Add 'declared_link' to valid_edge_type check constraint

Revision ID: a95_source_uri_declared_link
Revises: a94_semantic_similarity_edge
"""

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "a95_source_uri_declared_link"
down_revision = "a94_semantic_similarity_edge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add source_uri, source_type columns and declared_link edge type."""
    # #213: Add source_uri and source_type columns to memories
    op.add_column("memories", sa.Column("source_uri", sa.String(2048), nullable=True))
    op.add_column("memories", sa.Column("source_type", sa.String(20), nullable=True))

    # #213: Partial B-tree index for efficient source_uri lookups
    op.create_index(
        "idx_memories_source_uri",
        "memories",
        ["source_uri"],
        postgresql_where=sa.text("source_uri IS NOT NULL"),
    )

    # #215: Add 'declared_link' to valid_edge_type check constraint
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        "edge_type IN ('neural_association', 'related_to', 'depends_on', "
        "'learned_from', 'semantic_similarity', 'declared_link')",
    )


def downgrade() -> None:
    """Remove source_uri, source_type columns and declared_link edge type."""
    # Delete declared_link edges before narrowing the constraint
    op.execute("DELETE FROM neural_memory_edges WHERE edge_type = 'declared_link'")

    # Revert edge type constraint
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        "edge_type IN ('neural_association', 'related_to', 'depends_on', "
        "'learned_from', 'semantic_similarity')",
    )

    # Remove source_uri index and columns
    op.drop_index("idx_memories_source_uri", "memories")
    op.drop_column("memories", "source_type")
    op.drop_column("memories", "source_uri")
