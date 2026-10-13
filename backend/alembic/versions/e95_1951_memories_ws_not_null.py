"""#1951: ``memories.workspace_id`` becomes NOT NULL — backfill from ``contexts`` first.

Migration 063 added ``workspace_id`` / ``context_id`` to ``memories`` as nullable
columns and backfilled what it could. Every write path has required
``workspace_id`` since (``MemoryService`` raises on an empty one), so a NULL is
legacy data only — but #1946 made ``Memory.workspace_id == ws AND deleted_at IS
NULL`` the one definition of a workspace's live memory count, and a NULL row is
reachable through its context (recall, listing) while escaping the memory quota,
the plan page, the capacity-over lock (#1941) and the downgrade check.

Three steps, in the shape of ``b03_396`` (the same change on ``neural_memory_edges``):

1. **Refuse to run while an unrecoverable row exists.** A row with neither
   ``workspace_id`` nor ``context_id`` cannot be filled from anywhere. Edges were
   orphan-deleted in b03_396; a memory is user content, so this migration stops
   with the count and sample ids instead and leaves the decision (restore a
   context, or delete the rows) to the operator. With ``context_id`` present the
   context row exists — ``memories.context_id`` is ``ON DELETE CASCADE`` — so
   every other NULL is fillable.
2. **Backfill only NULLs.** ``UPDATE ... WHERE m.workspace_id IS NULL`` — a row that
   already carries a workspace is never rewritten, even when it disagrees with
   its context's (no ``COALESCE`` over existing values; see the b03_396 lesson).
   Such disagreements are counted into the migration log for a later audit.
3. **Enforce without a long lock.** ``CHECK ... NOT VALID`` → ``VALIDATE CONSTRAINT``
   (``SHARE UPDATE EXCLUSIVE``, does not block reads or writes) → ``SET NOT NULL``
   (PG 12+ uses the validated CHECK as proof and skips the table scan). The
   scaffolding CHECK is dropped afterwards: the column constraint is the
   invariant, and ``MemoryService`` guards the write path.

Revision ID: e95_1951_memories_ws_not_null
Revises: e94_1869_summary_emb_idx

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import logging

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e95_1951_memories_ws_not_null"
down_revision: str | None = "e94_1869_summary_emb_idx"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

_CHECK_CONSTRAINT = "ck_memories_workspace_id_not_null"

# Rows nothing can fill: no workspace and no context to read one from.
ORPHAN_COUNT_SQL = "SELECT count(*) FROM memories WHERE workspace_id IS NULL AND context_id IS NULL"
ORPHAN_SAMPLE_SQL = (
    "SELECT id FROM memories WHERE workspace_id IS NULL AND context_id IS NULL "
    "ORDER BY created_at LIMIT 10"
)

# Only NULLs are written; an existing value is never overwritten.
BACKFILL_SQL = (
    "UPDATE memories AS m SET workspace_id = c.workspace_id "
    "FROM contexts AS c "
    "WHERE m.context_id = c.id AND m.workspace_id IS NULL"
)

# Audit only: rows whose stored workspace disagrees with their context's.
MISMATCH_COUNT_SQL = (
    "SELECT count(*) FROM memories AS m JOIN contexts AS c ON c.id = m.context_id "
    "WHERE m.workspace_id IS NOT NULL AND m.workspace_id <> c.workspace_id"
)


def upgrade() -> None:
    conn = op.get_bind()

    orphans = conn.execute(sa.text(ORPHAN_COUNT_SQL)).scalar_one()
    if orphans:
        sample = [str(row) for row in conn.execute(sa.text(ORPHAN_SAMPLE_SQL)).scalars()]
        raise RuntimeError(
            f"#1951: {orphans} memories have neither workspace_id nor context_id and "
            "cannot be backfilled; resolve them (restore their context or delete the rows) "
            f"before applying this migration. Sample ids: {sample}"
        )

    mismatched = conn.execute(sa.text(MISMATCH_COUNT_SQL)).scalar_one()
    if mismatched:
        logger.warning(
            "#1951: %d memories carry a workspace_id different from their context's; "
            "left untouched (audit separately)",
            mismatched,
        )

    result = conn.execute(sa.text(BACKFILL_SQL))
    logger.info("#1951: backfilled workspace_id on %d memories from contexts", result.rowcount)

    op.execute(
        sa.text(
            f"ALTER TABLE memories ADD CONSTRAINT {_CHECK_CONSTRAINT} "
            "CHECK (workspace_id IS NOT NULL) NOT VALID"
        )
    )
    op.execute(sa.text(f"ALTER TABLE memories VALIDATE CONSTRAINT {_CHECK_CONSTRAINT}"))
    op.alter_column(
        "memories",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_constraint(_CHECK_CONSTRAINT, "memories", type_="check")


def downgrade() -> None:
    op.alter_column(
        "memories",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
