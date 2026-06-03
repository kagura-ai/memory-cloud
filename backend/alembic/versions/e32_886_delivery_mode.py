"""Add delivery_mode column + CHECK + partial always-load index (#886).

``delivery_mode`` is an orthogonal delivery attribute on Memory (design memory
242cb28a), the sibling of ``scope`` — NOT a new memory ``type``. Default
'on_recall' = current probabilistic-only behavior, so existing rows are
unaffected (NOT NULL is safe with the server-side DEFAULT backfilling them in
the same ALTER; PG11+ applies a constant default as metadata-only, no rewrite).
The CHECK literal is byte-identical (modulo the drift detector's whitespace +
IN-list normalization) to the ORM ``valid_delivery_mode`` CheckConstraint
f-string in models/memory.py.

The partial index backs the deterministic always-load read path
(MemoryService.load_pinned): ``WHERE context_id = ? AND delivery_mode = 'always'
AND deleted_at IS NULL``. It is built ``CONCURRENTLY`` inside an autocommit_block
(mirroring e29_619) so the build does not take an ACCESS EXCLUSIVE lock on the
large ``memories`` table during deploy.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e32_886_delivery_mode"
down_revision = "e31_connector_addon"
branch_labels = None
depends_on = None

_INDEX_NAME = "idx_memories_delivery_always"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memories (context_id) "
    "WHERE delivery_mode = 'always' AND deleted_at IS NULL"
)


def _index_is_invalid(name: str) -> bool:
    """Return True if ``name`` exists in ``pg_index`` in an INVALID state.

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    with ``indisvalid = false``; a retry's ``IF NOT EXISTS`` would skip it, so
    the leftover must be dropped first. Mirrors e29_619.
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


def _constraint_exists(name: str) -> bool:
    """Return True if a constraint named ``name`` already exists."""
    bind = op.get_bind()
    return (
        bind.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).first()
        is not None
    )


def upgrade() -> None:
    # Retry-safety: the autocommit_block below COMMITS the column + CHECK before
    # the CONCURRENTLY index runs, so a mid-build index failure leaves the column
    # committed while alembic_version is NOT bumped. The whole upgrade must
    # therefore be re-runnable: ADD COLUMN IF NOT EXISTS + a guarded CHECK make
    # the transactional part idempotent (the index part already is, via e29_619's
    # INVALID-leftover pattern). Column + CHECK are metadata-only on PG11+
    # (constant DEFAULT, no table rewrite).
    op.execute(
        "ALTER TABLE memories "
        "ADD COLUMN IF NOT EXISTS delivery_mode VARCHAR(20) NOT NULL DEFAULT 'on_recall'"
    )
    if not _constraint_exists("valid_delivery_mode"):
        op.create_check_constraint(
            "valid_delivery_mode",
            "memories",
            "delivery_mode IN ('always', 'on_recall', 'on_trigger')",
        )
    # Index must be built CONCURRENTLY (no table lock) → outside the transaction.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
    op.drop_constraint("valid_delivery_mode", "memories", type_="check")
    op.drop_column("memories", "delivery_mode")
