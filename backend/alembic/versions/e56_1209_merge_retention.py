"""Merge retention phase surfaces (#1209).

1. ``sleep_reports.merge_retention_result`` — per-phase JSON blob for the
   new merge_retention phase (mirrors the five existing ``*_result``
   columns); without it the phase's outcome is silently discarded by
   ``complete_report``.
2. Seed the ``sleep_merge_retention_days`` neural_config row so the setting
   is operable via the admin Neural Config UI / API (the PUT endpoint 404s
   on unseeded keys). Default '0' = disabled = retain merge losers forever —
   the pre-#1209 behavior; enabling the window is an explicit operator
   decision.

NOTE (merge coordination): this branch forks from main at e54. If
#1207/e55 merges first, bump ``down_revision`` to
"e55_1207_reinforce_default_on" (one line) before merging this PR — and
vice versa for e55 if this lands first.

Revision ID: e56_1209_merge_retention
Revises: e54_1183_sleep_status_degraded
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e56_1209_merge_retention"
down_revision = "e55_1207_reinforce_default_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sleep_reports",
        sa.Column("merge_retention_result", sa.JSON(), nullable=True),
    )
    op.execute("""
        INSERT INTO neural_config (key, value, value_type, category, description, min_value, max_value) VALUES
            ('sleep_merge_retention_days', '0', 'int', 'sleep', 'Merge-loser purge window in days (0 = retain forever); per-merge undo and rollback only work inside the window (#1209)', 0, 3650)
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    op.drop_column("sleep_reports", "merge_retention_result")
    op.execute("DELETE FROM neural_config WHERE key = 'sleep_merge_retention_days';")
