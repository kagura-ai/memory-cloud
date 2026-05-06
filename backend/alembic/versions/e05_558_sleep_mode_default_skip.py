"""Change Context.sleep_mode column default from 'full' to 'skip' (#558).

Issue #558: New contexts default to opt-in Sleep Maintenance.

The existing column default ``"full"`` causes every new context to run all
Sleep Maintenance phases (including LLM calls) until the user explicitly
opts out. This migration changes the column default to ``"skip"`` so new
contexts are silent until the owner opts in via the Settings UI (Issue #504).

Existing rows are not modified — this is a metadata-only ``ALTER TABLE``
that changes the default applied to subsequent ``INSERT`` statements that
omit the column. Rows already populated with ``"full"`` / ``"edges_only"`` /
``"skip"`` keep their values.

Revision ID: e05_558_sleep_default_skip
Revises: e04_552_gc_index
"""

from alembic import op

revision = "e05_558_sleep_default_skip"
down_revision = "e04_552_gc_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("contexts", "sleep_mode", server_default="skip")


def downgrade() -> None:
    op.alter_column("contexts", "sleep_mode", server_default="full")
