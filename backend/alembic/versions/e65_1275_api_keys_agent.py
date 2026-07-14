"""Add api_keys.agent_id — agent-bound member keys (RFC-0002 P0-2, #1275).

Migration class 2 of docs/design/agent-registry-and-bindings.md: ALTERs on
the hottest auth table, engineered so it never holds a long ACCESS EXCLUSIVE
lock. Blue-green argument: old app versions always write ``agent_id`` NULL,
which satisfies the new constraint.

- nullable ``agent_id`` FK (ADD COLUMN of a nullable column = brief metadata
  lock only);
- ``ck_api_keys_agent_public_exclusion`` CHECK added ``NOT VALID`` (no table
  scan) then validated as a separate statement (SHARE UPDATE EXCLUSIVE — does
  not block reads/writes);
- ``idx_api_keys_agent`` partial index built ``CONCURRENTLY``.

Rerun safety (#655 hardening pattern): ``CONCURRENTLY`` and ``VALIDATE``
run in an autocommit block; every statement is guarded (``IF NOT EXISTS`` /
``duplicate_object`` DO-block / INVALID-leftover rebuild per e62) so a
partial failure is re-runnable.
"""

import sqlalchemy as sa

from alembic import op

revision = "e65_1275_api_keys_agent"
down_revision = "e64_1275_agent_bindings"
branch_labels = None
depends_on = None

_INDEX_NAME = "idx_api_keys_agent"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON api_keys (agent_id) WHERE agent_id IS NOT NULL"
)
_CHECK_NAME = "ck_api_keys_agent_public_exclusion"


def _index_is_invalid(name: str) -> bool:
    """True if ``name`` exists in ``pg_index`` in an INVALID state (e62 guard).

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    with ``indisvalid = false``; a retry's ``IF NOT EXISTS`` would skip it
    without rebuilding, so the leftover must be dropped first.
    """
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 "
            "FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND i.indisvalid IS FALSE"
        ),
        {"name": name},
    ).first()
    return row is not None


def upgrade() -> None:
    # Step 1: nullable FK column. ADD COLUMN IF NOT EXISTS makes the step
    # rerun-safe; a nullable column with no default is a metadata-only change.
    # The FK constraint is named explicitly to match the ORM naming convention
    # (``fk_api_keys_agent_id``) so the create_all/alembic drift gate sees one
    # identical constraint instead of a PG-default ``api_keys_agent_id_fkey``.
    op.execute(
        sa.text(
            "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS agent_id UUID NULL "
            "CONSTRAINT fk_api_keys_agent_id "
            "REFERENCES agents(id) ON DELETE CASCADE"
        )
    )

    # Step 2: mutual-exclusion CHECK, added NOT VALID so existing rows are not
    # scanned under lock. DO-block guard because PG has no ADD CONSTRAINT IF
    # NOT EXISTS for CHECK constraints (#655 pattern).
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"  ALTER TABLE api_keys ADD CONSTRAINT {_CHECK_NAME} "
            "    CHECK (agent_id IS NULL OR bound_context_id IS NULL) NOT VALID; "
            "EXCEPTION WHEN duplicate_object THEN NULL; "
            "END $$"
        )
    )

    # Step 3: validate + build the partial index outside a transaction.
    # VALIDATE CONSTRAINT takes SHARE UPDATE EXCLUSIVE (no read/write block);
    # re-running VALIDATE on an already-validated constraint is a no-op.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"ALTER TABLE api_keys VALIDATE CONSTRAINT {_CHECK_NAME}"))
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    # Reverse dependency order: index → constraint → column.
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
    op.execute(sa.text(f"ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS {_CHECK_NAME}"))
    op.execute(sa.text("ALTER TABLE api_keys DROP COLUMN IF EXISTS agent_id"))
