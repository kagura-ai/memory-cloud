"""Extend sleep_report_llm_usage.phase CHECK with 'cluster_labeling' (#495).

Issue #495 broadlistening pipeline (B2 of #493 umbrella) emits per-call
LLM usage rows into ``sleep_report_llm_usage`` so cost-aggregation API
(#472) can ``GROUP BY (provider, model)`` for analysis runs the same
way it does for sleep runs. The pre-existing CHECK constraint
``valid_sleep_report_llm_usage_phase`` (added by ``c02_471``) limits
``phase`` to the five sleep-side phase names; an analysis insert with
``phase='cluster_labeling'`` would fail the constraint and roll back
the entire all-or-nothing Stage [J] persist transaction.

This migration extends the CHECK to also accept ``'cluster_labeling'``
(the analysis labeler stage). It is a metadata-only catalog change on
PostgreSQL >= 11; no table rewrite occurs even on a populated table.

The drop-and-recreate is performed inside a single ALTER TABLE so it
runs as one atomic catalog operation. We use the zero-downtime
``NOT VALID`` then ``VALIDATE CONSTRAINT`` pair (matches ``d05_523``
precedent) so the validation scan runs under SHARE UPDATE EXCLUSIVE
rather than ACCESS EXCLUSIVE — production reads/writes continue
during the migration. Existing rows already satisfy the new (broader)
CHECK by definition, so VALIDATE is effectively a no-op scan.

Revision ID: d07_495_cluster_label_phase
Revises: d06_494_memory_analyses

NOTE: Revision IDs are capped at 32 chars because
``alembic_version.version_num`` is ``VARCHAR(32)`` (asyncpg raises
``StringDataRightTruncationError`` otherwise). This revision uses
``d07_495_cluster_label_phase`` (28 chars) — within cap.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d07_495_cluster_label_phase"
down_revision: str | Sequence[str] | None = "d06_494_memory_analyses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_CHECK = (
    "phase IN ('edge_discovery', 'dedup_merge', 'importance_reeval', 'consolidation', 'reindex')"
)
_NEW_CHECK = (
    "phase IN ('edge_discovery', 'dedup_merge', 'importance_reeval', "
    "'consolidation', 'reindex', 'cluster_labeling')"
)


def upgrade() -> None:
    """Drop the old CHECK and add the broader one as NOT VALID + VALIDATE."""
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "DROP CONSTRAINT IF EXISTS valid_sleep_report_llm_usage_phase"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "ADD CONSTRAINT valid_sleep_report_llm_usage_phase "
            f"CHECK ({_NEW_CHECK}) NOT VALID"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "VALIDATE CONSTRAINT valid_sleep_report_llm_usage_phase"
        )
    )


def downgrade() -> None:
    """Restore the original CHECK (rejects 'cluster_labeling')."""
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "DROP CONSTRAINT IF EXISTS valid_sleep_report_llm_usage_phase"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "ADD CONSTRAINT valid_sleep_report_llm_usage_phase "
            f"CHECK ({_OLD_CHECK}) NOT VALID"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "VALIDATE CONSTRAINT valid_sleep_report_llm_usage_phase"
        )
    )
