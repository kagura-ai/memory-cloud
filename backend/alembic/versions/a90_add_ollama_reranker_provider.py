"""Add ollama to reranker_provider check constraint.

Issue #70: Add Ollama as local reranker provider.

Revision ID: a90_ollama_reranker
Revises: (auto)
"""

from alembic import op


# revision identifiers
revision = "a90_ollama_reranker"
down_revision = "a85_context_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add 'ollama' to reranker_provider check constraint."""
    op.drop_constraint("reranker_provider_check", "context_search_configs", type_="check")
    op.create_check_constraint(
        "reranker_provider_check",
        "context_search_configs",
        "reranker_provider IN ('voyage', 'cohere', 'ollama')",
    )


def downgrade() -> None:
    """Remove 'ollama' from reranker_provider check constraint."""
    op.drop_constraint("reranker_provider_check", "context_search_configs", type_="check")
    op.create_check_constraint(
        "reranker_provider_check",
        "context_search_configs",
        "reranker_provider IN ('voyage', 'cohere')",
    )
