"""Add ollama to reranker_provider check constraint.

Issue #70: Add Ollama as local reranker provider.

Revision ID: a90_ollama_reranker
Revises: (auto)
"""

from alembic import op


# revision identifiers
revision = "a90_ollama_reranker"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add 'ollama' to reranker_provider check constraint."""
    op.drop_constraint("reranker_provider_check", "context_search_config", type_="check")
    op.create_check_constraint(
        "reranker_provider_check",
        "context_search_config",
        "reranker_provider IN ('voyage', 'cohere', 'ollama')",
    )


def downgrade() -> None:
    """Remove 'ollama' from reranker_provider check constraint."""
    op.drop_constraint("reranker_provider_check", "context_search_config", type_="check")
    op.create_check_constraint(
        "reranker_provider_check",
        "context_search_config",
        "reranker_provider IN ('voyage', 'cohere')",
    )
