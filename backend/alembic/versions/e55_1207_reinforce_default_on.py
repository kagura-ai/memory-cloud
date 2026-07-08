"""Flip reinforce_enabled column default to true (#1207).

The pre-registered kagura-memory-eval program attributed the
update-correctness headline (+0.36 conditional lift over vanilla RAG,
BCa 95% [0.24, 0.50]) entirely to the bounded reinforce recency re-rank
(#1048). New contexts should get the eval-proven update kernel without
discovering a flag, so the per-context default flips to ON.

This migration changes ONLY the DDL default (``ALTER COLUMN … SET
DEFAULT``). It deliberately does NOT rewrite existing rows:

- contexts with an explicit ``reinforce_enabled=false`` keep their opt-out;
- contexts enabled during the Phase-C graduation keep ``true``;
- legacy contexts without a ``context_search_configs`` row adopt the new
  default lazily — the search path materializes the row via
  ``create_or_get`` on their next recall — so effectively every context
  without a stored ``false`` converges to ON. This lazy adoption is the
  recorded #1207 decision (only an explicit stored opt-out is honored).

Blue-green safe: the previous app version inserts rows with an explicit
Python-side ``default=False`` value, so it is unaffected by the DDL
default; the new version sends ``true`` explicitly. The server default
only governs raw-SQL inserts that omit the column.

Revision ID: e55_1207_reinforce_default_on
Revises: e54_1183_sleep_status_degraded
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e55_1207_reinforce_default_on"
down_revision = "e54_1183_sleep_status_degraded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "context_search_configs",
        "reinforce_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("true"),
    )


def downgrade() -> None:
    op.alter_column(
        "context_search_configs",
        "reinforce_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("false"),
    )
