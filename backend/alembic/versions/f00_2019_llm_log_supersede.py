"""Add the supersede_verify caller to the llm_call_log ledger.

Issue #2019: the ingest-time supersede judge (#2016,
``services/supersede_verify.py``) calls the Sleep judge LLM once per new
supersede suggestion, outside any Sleep run. Its spend had no ledger row — only
a ``supersede_verify_judged`` log line, which environments without a log
platform lose within hours. It now writes one ``completion`` row per call under
``caller='supersede_verify'``, so the ``valid_llm_call_log_caller`` vocabulary
gains that value.

The CHECK literal is kept byte-identical to the ordered ``LLM_CALL_LOG_CALLERS``
tuple in ``models/llm_call_log.py`` (pinned by
``tests/test_llm_call_log_constants.py`` and ``tests/test_schema_drift.py``).
New values are APPENDED, never reordered.

``llm_call_log`` is written on the recall path, so this uses the house
zero-downtime pattern (``e65_1275`` / ``e67_1281``): an atomic
``DROP ..., ADD ... NOT VALID`` (ACCESS EXCLUSIVE, but no scan), then
``VALIDATE CONSTRAINT`` inside ``autocommit_block()``. ``alembic/env.py`` runs
an upgrade in one transaction, so a VALIDATE in that transaction would scan
under the ALTER's ACCESS EXCLUSIVE lock until commit (#2073); the block commits
the NOT VALID add first, and VALIDATE then holds only SHARE UPDATE EXCLUSIVE
(reads and writes continue). The new CHECK only widens the vocabulary, so every
existing row satisfies it.

Revision ID: f00_2019_llm_log_supersede
Revises: e99_2022_supersede_cand_null
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f00_2019_llm_log_supersede"
down_revision: str | None = "e99_2022_supersede_cand_null"
branch_labels: str | None = None
depends_on: str | None = None


_OLD_CHECK = "caller IN ('sleep', 'recall', 'rerank', 'ask', 'admin')"
_NEW_CHECK = "caller IN ('sleep', 'recall', 'rerank', 'ask', 'admin', 'supersede_verify')"


def _validate() -> None:
    """VALIDATE outside the ALTER's transaction (#2073).

    ``autocommit_block()`` commits the preceding transaction, releasing the
    NOT VALID add's ACCESS EXCLUSIVE lock, so the scan runs under SHARE UPDATE
    EXCLUSIVE only. Offline (``--sql``) it emits ``COMMIT;`` / ``BEGIN;`` —
    for the upgrade only: the downgrade's leftover probe reads the table, so
    it needs a live connection.
    """
    with op.get_context().autocommit_block():
        op.execute(
            sa.text("ALTER TABLE llm_call_log VALIDATE CONSTRAINT valid_llm_call_log_caller")
        )


def upgrade() -> None:
    """Widen the valid_llm_call_log_caller CHECK (zero-downtime).

    DROP + ADD are one ALTER TABLE so there is no window without a caller
    CHECK; VALIDATE runs after that ALTER commits (see the module docstring).
    """
    op.execute(
        sa.text(
            "ALTER TABLE llm_call_log "
            "DROP CONSTRAINT IF EXISTS valid_llm_call_log_caller, "
            f"ADD CONSTRAINT valid_llm_call_log_caller CHECK ({_NEW_CHECK}) NOT VALID"
        )
    )
    _validate()


def downgrade() -> None:
    """Restore the previous CHECK, keeping the ledger rows.

    ``llm_call_log`` is an append-only cost ledger, so ``supersede_verify``
    rows are never deleted here (the e84_1619 / e98_2007 form). The previous
    CHECK is restored ``NOT VALID`` — enforced on every new INSERT / UPDATE,
    and a downgraded server never writes this caller — and is VALIDATEd only
    when no such row exists. The check runs while the ALTER's lock is held and,
    once that commits, the NOT VALID CHECK rejects new such rows, so the
    VALIDATE that follows cannot fail on a row written in between. With rows
    present the constraint stays ``convalidated = false``; re-running the
    upgrade validates it again.
    """
    op.execute(
        sa.text(
            "ALTER TABLE llm_call_log "
            "DROP CONSTRAINT IF EXISTS valid_llm_call_log_caller, "
            f"ADD CONSTRAINT valid_llm_call_log_caller CHECK ({_OLD_CHECK}) NOT VALID"
        )
    )
    leftover = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM llm_call_log WHERE caller = 'supersede_verify' LIMIT 1"))
        .first()
    )
    if leftover is None:
        _validate()
