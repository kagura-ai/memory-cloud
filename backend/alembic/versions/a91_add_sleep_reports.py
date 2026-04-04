"""Add sleep_reports and sleep_actions tables + sleep config seed data.

Issue #101: Sleep Maintenance Foundation.

Revision ID: a91_sleep_reports
Revises: d18bcb6512e2
"""

from alembic import op

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "a91_sleep_reports"
down_revision = "a120_neural_gating"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create sleep maintenance tables and seed config."""

    # ================================================================
    # sleep_reports: execution report per sleep maintenance run
    # ================================================================
    op.create_table(
        "sleep_reports",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("user_id", sa.String(255), nullable=False, index=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("started_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        # Per-phase results
        sa.Column("edge_discovery_result", sa.JSON, nullable=True),
        sa.Column("dedup_result", sa.JSON, nullable=True),
        sa.Column("importance_result", sa.JSON, nullable=True),
        sa.Column("consolidation_result", sa.JSON, nullable=True),
        sa.Column("reindex_result", sa.JSON, nullable=True),
        # Cost tracking
        sa.Column("llm_calls_made", sa.Integer, nullable=False, server_default="0"),
        sa.Column("llm_tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("embedding_calls_made", sa.Integer, nullable=False, server_default="0"),
        # Activity counters
        sa.Column("memories_processed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("edges_created", sa.Integer, nullable=False, server_default="0"),
        sa.Column("memories_merged", sa.Integer, nullable=False, server_default="0"),
        sa.Column("memories_promoted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("memories_flagged", sa.Integer, nullable=False, server_default="0"),
        # Error
        sa.Column("error_message", sa.Text, nullable=True),
        # Constraints
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="valid_sleep_report_status",
        ),
    )

    op.create_index("idx_sleep_reports_user_status", "sleep_reports", ["user_id", "status"])
    op.create_index("idx_sleep_reports_started_at", "sleep_reports", ["started_at"])

    # ================================================================
    # sleep_actions: audit log for individual sleep maintenance actions
    # ================================================================
    op.create_table(
        "sleep_actions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "report_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sleep_reports.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("action_type", sa.String(30), nullable=False),
        sa.Column("memory_id", UUID(as_uuid=True), nullable=True),
        sa.Column("target_id", UUID(as_uuid=True), nullable=True),
        sa.Column("details", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    op.create_index("idx_sleep_actions_report_phase", "sleep_actions", ["report_id", "phase"])

    # ================================================================
    # Seed sleep config into neural_config (idempotent)
    # ================================================================
    op.execute("""
        INSERT INTO neural_config (key, value, value_type, category, description, min_value, max_value) VALUES
            ('sleep_llm_provider', 'openai', 'string', 'sleep', 'LLM provider for sleep maintenance (openai/ollama)', NULL, NULL),
            ('sleep_llm_model', 'gpt-5-nano', 'string', 'sleep', 'LLM model for sleep maintenance', NULL, NULL),
            ('sleep_max_memories_per_run', '200', 'int', 'sleep', 'Max memories processed per sleep run', 10, 5000),
            ('sleep_max_llm_calls_per_run', '50', 'int', 'sleep', 'Max LLM API calls per sleep run', 1, 500),
            ('sleep_dedup_enabled', 'true', 'bool', 'sleep', 'Enable dedup/merge phase', NULL, NULL),
            ('sleep_dedup_similarity_threshold', '0.92', 'float', 'sleep', 'Cosine similarity threshold for duplicate detection', 0.5, 1.0),
            ('sleep_edge_discovery_enabled', 'true', 'bool', 'sleep', 'Enable edge discovery phase', NULL, NULL),
            ('sleep_edge_discovery_sample_size', '30', 'int', 'sleep', 'Number of memories to sample per edge discovery run', 5, 200),
            ('sleep_importance_reeval_enabled', 'true', 'bool', 'sleep', 'Enable importance re-evaluation phase', NULL, NULL)
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    """Drop sleep maintenance tables and config."""
    op.drop_table("sleep_actions")
    op.drop_table("sleep_reports")

    op.execute("""
        DELETE FROM neural_config WHERE category = 'sleep';
    """)
