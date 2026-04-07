"""Add semantic_similarity to valid_edge_type constraint.

Issue #221: k-NN cold-start seeding creates `semantic_similarity` edges
from new memories to their nearest neighbors in Qdrant. The existing
CheckConstraint only allowed 4 edge types; extend it to 5.

Revision ID: a94_semantic_similarity_edge
Revises: a93_rolled_back_status
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "a94_semantic_similarity_edge"
down_revision = "a93_rolled_back_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add 'semantic_similarity' to valid_edge_type check constraint."""
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        "edge_type IN ('neural_association', 'related_to', 'depends_on', "
        "'learned_from', 'semantic_similarity')",
    )


def downgrade() -> None:
    """Remove 'semantic_similarity' from valid_edge_type check constraint.

    Note: This will fail if any edges with edge_type='semantic_similarity'
    exist. Delete those first:
        DELETE FROM neural_memory_edges WHERE edge_type = 'semantic_similarity';
    """
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        "edge_type IN ('neural_association', 'related_to', 'depends_on', 'learned_from')",
    )
