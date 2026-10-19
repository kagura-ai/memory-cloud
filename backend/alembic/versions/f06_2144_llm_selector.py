"""#2144: add context_search_configs.llm_selector_enabled and llm_selector_enabled_at.

``llm_selector_enabled``: a context's opt-in to the recall LLM selector.
``BOOLEAN NOT NULL DEFAULT false`` — a constant default, so ADD COLUMN is
metadata-only on PG 11+ (no table rewrite) and every existing row reads off.

``llm_selector_enabled_at``: when the flag was last turned on, the
earliest-enabled rank under a plan's cap (the #2103 shape). Nullable, no
default, no constraint.

No backfill: nothing is on yet.

Revision ID: f06_2144_llm_selector
Revises: f05_2121_rollback_window
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "f06_2144_llm_selector"
down_revision = "f05_2121_rollback_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add llm_selector_enabled (default false) and llm_selector_enabled_at."""
    op.add_column(
        "context_search_configs",
        sa.Column(
            "llm_selector_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "context_search_configs",
        sa.Column("llm_selector_enabled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop both columns."""
    op.drop_column("context_search_configs", "llm_selector_enabled_at")
    op.drop_column("context_search_configs", "llm_selector_enabled")
