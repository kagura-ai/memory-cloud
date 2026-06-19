"""Add reinforce re-ranking config to context_search_configs (#1048).

Issue #1048 (the "reinforce" half of recall -> adoption -> reinforce): a bounded,
per-context, default-OFF recall re-rank that nudges standing by the #1046
adoption signal + #888 retrieval feedback, without overriding semantic relevance.

Adds two columns to ``context_search_configs``:
- ``reinforce_enabled BOOLEAN NOT NULL DEFAULT false`` — gate (off → recall
  ranking is byte-identical to pre-#1048).
- ``reinforce_max_boost NUMERIC(3,2) NOT NULL DEFAULT 0.15`` — bound on the
  per-result multiplicative adjustment (factor stays in [1-boost, 1+boost]).

Both server defaults backfill existing rows in one statement.

Revision ID: e42_1048_reinforce_ranking
Revises: e41_1046_reference_count
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e42_1048_reinforce_ranking"
down_revision = "e41_1046_reference_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "context_search_configs",
        sa.Column("reinforce_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "context_search_configs",
        sa.Column(
            "reinforce_max_boost",
            sa.Numeric(3, 2),
            nullable=False,
            server_default="0.15",
        ),
    )


def downgrade() -> None:
    op.drop_column("context_search_configs", "reinforce_max_boost")
    op.drop_column("context_search_configs", "reinforce_enabled")
