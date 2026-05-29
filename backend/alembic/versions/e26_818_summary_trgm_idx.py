"""Add pg_trgm GIN index on memories.summary for /memory/list ?q ILIKE (#818).

Issue #580 added an optional ``q`` substring filter to ``GET /api/v1/memory/list``
implemented as ``Memory.summary.ilike(f"%{q}%", escape="\\")``. ``summary`` is a
plain ``Text`` column with no index, so the filter (and its paired ``count(*)``)
does a sequential scan per request — fine at current per-workspace sizes, but a
latency cliff as memories grow into the 10k+ range.

This migration accelerates that filter with a trigram GIN index.

### Why plain ``summary``, not ``lower(summary)``

The call site issues ``summary ILIKE '%foo%'`` (case-insensitive, on the bare
column). ``pg_trgm``'s ``gin_trgm_ops`` operator class supports the ``ILIKE``
operator directly — ``show_trgm`` lowercases before extracting trigrams, so a
GIN index on the **plain** column already serves case-insensitive matching.

The issue body floated ``gin (lower(summary) gin_trgm_ops)``, but a functional
index on ``lower(summary)`` is only consulted for predicates whose expression is
literally ``lower(summary)`` (e.g. ``lower(summary) LIKE lower(:p)``). It would
*not* be matched by the existing ``summary ILIKE :p`` call, so the index would
sit unused unless we also rewrote the query. Indexing the bare column keeps the
#580 ``q`` contract (literal substring, ``ILIKE``) byte-for-byte intact — no
query change — which is the stated goal. Verified via ``EXPLAIN (ANALYZE)``: the
plan switches from ``Seq Scan`` to ``Bitmap Index Scan on
idx_memories_summary_trgm`` for ``summary ILIKE '%foo%' ESCAPE '\'``.

### pg_trgm extension

``CREATE EXTENSION IF NOT EXISTS pg_trgm`` is idempotent. ``pg_trgm`` is a
*trusted* extension since PostgreSQL 13, so a role with ``CREATE`` on the
database (not necessarily superuser) can install it; the single-server prod
target runs the standard ``postgres`` image where it ships in ``contrib``. The
extension statement is transactional and runs before the autocommit block, so
``CREATE INDEX CONCURRENTLY`` sees a committed extension.

### Zero-downtime build

``memories`` is a high-write table. A plain transactional ``CREATE INDEX`` takes
``ACCESS EXCLUSIVE`` for the build duration, blocking writes. Following the
a96/a97/b02 pattern, the index is built with ``CREATE INDEX CONCURRENTLY IF NOT
EXISTS`` inside ``autocommit_block`` (env.py wraps migrations in a transaction;
CONCURRENTLY cannot run inside one), with an INVALID-leftover guard so a retry
after a mid-build failure rebuilds cleanly rather than skipping on the name.

### Downgrade

Drops the index ``CONCURRENTLY``. The ``pg_trgm`` extension is intentionally
**left installed** — other objects may depend on it, and dropping an extension
is a heavier, riskier operation than this migration owns.

Revision ID: e26_818_summary_trgm_idx
Revises: e25_782_widen_edge_type
Create Date: 2026-05-29

(Revision ID kept ≤ 32 chars to fit ``alembic_version.version_num
varchar(32)``; the longer descriptive name lives in the filename.)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e26_818_summary_trgm_idx"
down_revision: str | Sequence[str] | None = "e25_782_widen_edge_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "idx_memories_summary_trgm"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON memories USING gin (summary gin_trgm_ops)"
)


def _index_is_invalid(name: str) -> bool:
    """Return True if ``name`` exists in ``pg_index`` in an INVALID state.

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    with ``indisvalid = false``. A retry's ``IF NOT EXISTS`` would skip it (the
    name exists) without rebuilding, leaving the cluster permanently without a
    usable index — so the leftover must be dropped first.
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
    """Enable pg_trgm and build the trigram GIN index on summary concurrently."""
    # Transactional: committed when autocommit_block opens, so the CONCURRENTLY
    # build below sees the extension.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    invalid = _index_is_invalid(_INDEX_NAME)

    with op.get_context().autocommit_block():
        # Drop an INVALID leftover from a prior failed attempt before the
        # IF NOT EXISTS rebuild (IF NOT EXISTS alone would skip the rebuild).
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the trigram index concurrently; leave the pg_trgm extension installed."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
