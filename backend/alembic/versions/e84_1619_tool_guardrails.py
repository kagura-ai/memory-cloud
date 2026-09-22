"""Tool guardrails: ``idx_memories_tool_trigger`` + ``load_guardrails`` audit op.

A memory becomes a tool guardrail by carrying ``details.tool_trigger`` (an
orthogonal marker, like ``details.location``). The deterministic read
``MemoryService.load_guardrails`` selects those rows per context, so they get
the same partial-index treatment as the pinned lane
(``idx_memories_delivery_always``, e32_886)::

    CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_tool_trigger
    ON memories (context_id)
    WHERE (details->'tool_trigger') IS NOT NULL AND deleted_at IS NULL

``details`` is PostgreSQL ``json`` (not ``jsonb``); ``->`` on ``json`` is
IMMUTABLE, so the predicate is allowed in a partial index. The predicate text
is byte-identical to the ORM ``Index(... postgresql_where=...)`` in
``models/memory.py`` (the create_all-vs-alembic drift test compares them).

The second part appends ``'load_guardrails'`` to the ``memory_access_events``
operation CHECK so the new read surface's allows and denies are auditable.
The literal is kept byte-identical to the ordered ``MAE_OPERATIONS`` tuple in
``models/memory_access_event.py`` (drift pin:
``tests/test_memory_access_event_constants.py``) and uses the e74_1401
zero-downtime pattern (``DROP ..., ADD ... NOT VALID`` then ``VALIDATE``):
the table is written synchronously on the request path, and a plain ``ADD
CONSTRAINT CHECK`` would scan it under ACCESS EXCLUSIVE.

Ordering: the CHECK first (transactional; it commits when the autocommit block
opens), then the concurrent index — the same sequencing e32 used, so a
mid-build index failure leaves the CHECK committed and the whole upgrade
re-runnable (``IF NOT EXISTS`` + the INVALID-leftover guard).

Revision ID: e84_1619_tool_guardrails
Revises: e83_1595_beta_invite_label

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e84_1619_tool_guardrails"
down_revision: str | None = "e83_1595_beta_invite_label"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX_NAME = "idx_memories_tool_trigger"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memories (context_id) "
    "WHERE (details->'tool_trigger') IS NOT NULL AND deleted_at IS NULL"
)

_OLD_CHECK = (
    "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
    "'load_pinned', 'bootstrap', 'feedback', 'explore')"
)
_NEW_CHECK = (
    "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
    "'load_pinned', 'bootstrap', 'feedback', 'explore', 'load_guardrails')"
)


def _index_is_invalid(name: str) -> bool:
    """Return True if ``name`` exists in ``pg_index`` in an INVALID state.

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    with ``indisvalid = false``; a retry's ``IF NOT EXISTS`` would skip it, so
    the leftover must be dropped first. Mirrors e32_886 / e29_619.
    """
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND i.indisvalid IS FALSE"
        ),
        {"name": name},
    ).first()
    return row is not None


def upgrade() -> None:
    # CHECK swap first (transactional). DROP + ADD are one ALTER so there is no
    # window without an operation CHECK; VALIDATE runs under SHARE UPDATE
    # EXCLUSIVE so audit writes continue. Written inline (the e74 form) so the
    # schema-drift test can read the literal out of the f-string.
    op.execute(
        sa.text(
            "ALTER TABLE memory_access_events "
            "DROP CONSTRAINT IF EXISTS valid_mae_operation, "
            f"ADD CONSTRAINT valid_mae_operation CHECK ({_NEW_CHECK}) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE memory_access_events VALIDATE CONSTRAINT valid_mae_operation"))
    # Index must be built CONCURRENTLY (no table lock) → outside the transaction.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the index and restore the previous CHECK, keeping the audit rows.

    ``memory_access_events`` is append-only, so rows with
    ``operation='load_guardrails'`` are never deleted here. The previous CHECK
    is restored ``NOT VALID`` — PostgreSQL enforces a NOT VALID CHECK on every
    new INSERT / UPDATE, and a downgraded server never writes that operation —
    and is VALIDATEd only when no such row exists. With rows present the
    constraint stays ``convalidated = false`` (the rows remain readable and
    the schema-drift guard compares the *upgraded* schema); re-running the
    upgrade validates it again.
    """
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
    op.execute(
        sa.text(
            "ALTER TABLE memory_access_events "
            "DROP CONSTRAINT IF EXISTS valid_mae_operation, "
            f"ADD CONSTRAINT valid_mae_operation CHECK ({_OLD_CHECK}) NOT VALID"
        )
    )
    leftover = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM memory_access_events WHERE operation = 'load_guardrails' LIMIT 1"
            )
        )
        .first()
    )
    if leftover is None:
        op.execute(
            sa.text("ALTER TABLE memory_access_events VALIDATE CONSTRAINT valid_mae_operation")
        )
