"""Add (workspace_id, context_id) composite indexes on neural_memory_edges (#383).

Issue #383: graph reads move from ``user_id == caller`` hardcoded filter to
``PermissionService``-driven visibility. The new primary access pattern is
``WHERE workspace_id = :ws AND context_id = :ctx (AND src_id|dst_id = :node)``
— the existing ``idx_edges_user_src`` / ``idx_edges_user_dst`` composite
indexes are ``user_id``-leading and therefore inert for shared-context reads
under PostgreSQL's leftmost-prefix rule.

This migration adds two composite indexes matching the new access pattern:
``(workspace_id, context_id, src_id)`` and ``(workspace_id, context_id, dst_id)``.

Old indexes are intentionally **retained** in this revision — they still
serve the private-context read path (caller-scoped edges) and internal
admin paths (decay, consolidation) that filter by ``user_id``. A follow-up
migration may remove them once production query plans confirm they are
unused.

ZERO-DOWNTIME INDEX BUILDS
--------------------------
``neural_memory_edges`` is a high-write table (every new memory + sleep
consolidation cycle inserts rows). A plain transactional ``op.create_index``
would take ``ACCESS EXCLUSIVE`` on the table for the duration of the build,
blocking writes. This migration follows the same zero-downtime pattern
a96/a97 established: ``autocommit_block`` + ``CREATE INDEX CONCURRENTLY
IF NOT EXISTS`` so writes continue through the build, plus invalid-index
guards on retry after a mid-build failure.

Downgrade mirrors the pattern with ``DROP INDEX CONCURRENTLY IF EXISTS``.

Revision ID: b02_383_edges_ws_ctx_idx
Revises: b01_resource_pk_ph2
Create Date: 2026-04-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b02_383_edges_ws_ctx_idx"
down_revision: str | Sequence[str] | None = "b01_resource_pk_ph2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONCURRENT_INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_edges_ws_ctx_src", "neural_memory_edges (workspace_id, context_id, src_id)"),
    ("idx_edges_ws_ctx_dst", "neural_memory_edges (workspace_id, context_id, dst_id)"),
)


def _invalid_concurrent_indexes(name_iter: Sequence[str]) -> set[str]:
    """Return concurrent indexes from ``name_iter`` that exist in an INVALID state.

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    in ``pg_index`` with ``indisvalid = false``. Re-running ``CREATE INDEX
    CONCURRENTLY IF NOT EXISTS`` would skip (the name exists) without retrying,
    leaving the cluster permanently without a usable index. This helper
    enumerates names that need to be dropped first so the retry rebuilds cleanly.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT c.relname "
            "FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
            "WHERE c.relname = ANY(:names) AND i.indisvalid IS FALSE"
        ),
        {"names": list(name_iter)},
    ).fetchall()
    return {row[0] for row in rows}


def upgrade() -> None:
    """Create composite indexes concurrently for the new shared-context graph read path."""
    names = [name for name, _ in _CONCURRENT_INDEXES]
    invalid = _invalid_concurrent_indexes(names)

    with op.get_context().autocommit_block():
        # Drop any INVALID leftovers from a prior failed attempt before the
        # IF NOT EXISTS rebuild (IF NOT EXISTS alone would skip the rebuild).
        for name in names:
            if name in invalid:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))

        for name, table_and_cols in _CONCURRENT_INDEXES:
            op.execute(
                sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table_and_cols}")
            )


def downgrade() -> None:
    """Drop the composite indexes concurrently, mirroring the upgrade pattern."""
    with op.get_context().autocommit_block():
        for name, _ in _CONCURRENT_INDEXES:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
