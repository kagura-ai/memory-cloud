"""Add neural memory gating config parameters.

Issue #118: semantic gating (min_similarity_for_edge)
Issue #120: activation cap (max_assoc_score), top-k co-activation limit

Revision ID: a120_neural_gating
Revises: (auto)
"""

from alembic import op


# revision identifiers
revision = "a120_neural_gating"
down_revision = "a90_ollama_reranker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add neural gating config parameters."""
    op.execute("""
        INSERT INTO neural_config (key, value, value_type, category, description, min_value, max_value) VALUES
            ('min_similarity_for_edge', '0.5', 'float', 'coactivation', 'Minimum cosine similarity for edge creation (semantic gating)', 0.0, 1.0),
            ('max_assoc_score', '0.5', 'float', 'scoring', 'Maximum graph association score per node (activation cap)', 0.01, 1.0),
            ('top_k_coactivation', '3', 'int', 'coactivation', 'Only co-activate top-k results per recall (reduces noise edges)', 1, 50)
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    """Remove neural gating config parameters."""
    op.execute(
        "DELETE FROM neural_config WHERE key IN "
        "('min_similarity_for_edge', 'max_assoc_score', 'top_k_coactivation')"
    )
