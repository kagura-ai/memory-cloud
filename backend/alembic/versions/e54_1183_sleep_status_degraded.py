"""Sleep run health grading: 'degraded' status + llm_call_failures column (#1183).

Issue #1183: ``sleep_reports.status='completed'`` masked a fully
non-functional judge LLM — a run where every judge call raised still
reported completed (week1-derisk Day-5: llm_call_failures=5/5 under
ok=true). The reporter now grades runs:

  failed    — judge calls attempted, ALL raised
  degraded  — some raised, some succeeded
  completed — no judge failures

This migration:
1. Widens the ``valid_sleep_report_status`` CHECK with 'degraded'
   (drop + recreate — same pattern as e53's reranker CHECK).
2. Adds ``llm_call_failures`` (int, NOT NULL, default 0) so dashboards can
   aggregate judge health without parsing per-phase JSON blobs.

Blue-green safe: the CHECK is widened (old writers never emit 'degraded')
and the new column has a server_default, so the previous app version keeps
inserting successfully while both colors run.

Revision ID: e54_1183_sleep_status_degraded
Revises: e53_1160_self_hosted_provider
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "e54_1183_sleep_status_degraded"
down_revision = "e53_1160_self_hosted_provider"
branch_labels = None
depends_on = None

_OLD_CHECK = "status IN ('running', 'completed', 'failed', 'cancelled', 'rolled_back')"
_NEW_CHECK = "status IN ('running', 'completed', 'degraded', 'failed', 'cancelled', 'rolled_back')"


def upgrade() -> None:
    op.drop_constraint("valid_sleep_report_status", "sleep_reports", type_="check")
    op.create_check_constraint(
        "valid_sleep_report_status",
        "sleep_reports",
        _NEW_CHECK,
    )
    op.add_column(
        "sleep_reports",
        sa.Column(
            "llm_call_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("sleep_reports", "llm_call_failures")
    # Re-grade 'degraded' rows as 'completed' BEFORE narrowing the CHECK —
    # recreating the old constraint with 'degraded' rows present would fail.
    op.execute("UPDATE sleep_reports SET status = 'completed' WHERE status = 'degraded'")
    op.drop_constraint("valid_sleep_report_status", "sleep_reports", type_="check")
    op.create_check_constraint(
        "valid_sleep_report_status",
        "sleep_reports",
        _OLD_CHECK,
    )
