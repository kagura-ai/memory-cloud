"""Add cache_write_tokens and pricing_model columns (#546).

Phase 3 of Issue #546 adds two cost-grade schema corrections:

1. ``cache_write_tokens`` on ``sleep_report_llm_usage`` — captures
   Anthropic's ``cache_creation_input_tokens`` so cache-seed costs are
   no longer silently excluded from aggregation.

2. ``pricing_model`` on ``llm_pricing`` — distinguishes per-token
   billing (OpenAI, Anthropic, Google) from subscription models (Ollama
   Cloud) so the cost-aggregation SQL can exclude subscription rows and
   surface them as "cost unknown" rather than ``$0.00``.

Both columns are ``NOT NULL`` with ``server_default`` so existing rows
are backfilled without a table rewrite on PostgreSQL >= 11.

Revision ID: e01_546_cache_write_pricing
Revises: d08_536_device_code_grant
"""

import sqlalchemy as sa

from alembic import op

revision = "e01_546_cache_write_pricing"
down_revision = "d08_536_device_code_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add cache_write_tokens to sleep_report_llm_usage and pricing_model to llm_pricing."""
    # 1. cache_write_tokens on sleep_report_llm_usage
    op.add_column(
        "sleep_report_llm_usage",
        sa.Column(
            "cache_write_tokens",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # 2. pricing_model on llm_pricing
    op.add_column(
        "llm_pricing",
        sa.Column(
            "pricing_model",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'per_token'"),
        ),
    )

    # 3. CHECK constraint on llm_pricing.pricing_model
    op.execute(
        sa.text(
            "ALTER TABLE llm_pricing "
            "ADD CONSTRAINT valid_llm_pricing_model "
            "CHECK (pricing_model IN ('per_token', 'subscription', 'hybrid')) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE llm_pricing VALIDATE CONSTRAINT valid_llm_pricing_model"))


def downgrade() -> None:
    """Drop columns and constraint in reverse order."""
    op.drop_constraint("valid_llm_pricing_model", "llm_pricing", type_="check")
    op.drop_column("llm_pricing", "pricing_model")
    op.drop_column("sleep_report_llm_usage", "cache_write_tokens")
