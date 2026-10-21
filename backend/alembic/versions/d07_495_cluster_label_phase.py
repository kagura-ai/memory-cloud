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

The drop-and-recreate uses a single ``ALTER TABLE ... DROP
CONSTRAINT ..., ADD CONSTRAINT ... NOT VALID`` statement so the
catalog mutation is atomic — there is no window where the table has
no phase CHECK. The follow-up ``VALIDATE CONSTRAINT`` is necessarily
a separate statement (Postgres does not allow combining VALIDATE
with other actions in the same ALTER TABLE). Existing rows already
satisfy the new (broader) CHECK by definition, so VALIDATE only scans.

``alembic/env.py`` runs an upgrade in one transaction, so a VALIDATE in
that transaction would scan ``sleep_report_llm_usage`` under the ALTER's
ACCESS EXCLUSIVE lock until commit (#2079, the #2073 form). VALIDATE
therefore runs inside ``autocommit_block()``: the NOT VALID add commits
first, and VALIDATE then holds only SHARE UPDATE EXCLUSIVE (reads and
writes continue). The add is ``DROP CONSTRAINT IF EXISTS ..., ADD``, so a
re-run after that commit gets past it.

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


def _validate() -> None:
    """VALIDATE outside the ALTER's transaction (#2079).

    ``autocommit_block()`` commits the preceding transaction, releasing the
    NOT VALID add's ACCESS EXCLUSIVE lock, so the scan runs under SHARE UPDATE
    EXCLUSIVE only. Offline (``--sql``) it emits ``COMMIT;`` / ``BEGIN;`` —
    for the upgrade only: the downgrade's leftover probe reads the table, so
    it needs a live connection.
    """
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                "ALTER TABLE sleep_report_llm_usage "
                "VALIDATE CONSTRAINT valid_sleep_report_llm_usage_phase"
            )
        )


def upgrade() -> None:
    """Drop the old CHECK and add the broader one as NOT VALID + VALIDATE.

    DROP + ADD are combined in one ALTER TABLE so the catalog mutation
    is atomic (no window where the table has no phase CHECK). VALIDATE
    must be a separate statement per Postgres semantics, and runs after
    that ALTER commits (see the module docstring).
    """
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "DROP CONSTRAINT IF EXISTS valid_sleep_report_llm_usage_phase, "
            "ADD CONSTRAINT valid_sleep_report_llm_usage_phase "
            f"CHECK ({_NEW_CHECK}) NOT VALID"
        )
    )
    _validate()


def downgrade() -> None:
    """Restore the original CHECK (rejects 'cluster_labeling'), keeping the rows.

    Symmetric to ``upgrade``: DROP + ADD are atomic, VALIDATE is separate.
    ``sleep_report_llm_usage`` is a cost ledger, so ``cluster_labeling`` rows
    are never deleted here (the f00_2019 form). The previous CHECK is
    restored ``NOT VALID`` — enforced on every new INSERT / UPDATE, and a
    downgraded server never writes this phase — and is VALIDATEd only when
    no such row exists. The check runs while the ALTER's lock is held and,
    once that commits, the NOT VALID CHECK rejects new such rows, so the
    VALIDATE that follows cannot fail on a row written in between. With rows
    present the constraint stays ``convalidated = false``; re-running the
    upgrade validates it again.
    """
    op.execute(
        sa.text(
            "ALTER TABLE sleep_report_llm_usage "
            "DROP CONSTRAINT IF EXISTS valid_sleep_report_llm_usage_phase, "
            "ADD CONSTRAINT valid_sleep_report_llm_usage_phase "
            f"CHECK ({_OLD_CHECK}) NOT VALID"
        )
    )
    leftover = (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM sleep_report_llm_usage WHERE phase = 'cluster_labeling' LIMIT 1")
        )
        .first()
    )
    if leftover is None:
        _validate()
