"""#1951: ``memories.workspace_id`` becomes NOT NULL — backfill from ``contexts`` first.

Migration 063 added ``workspace_id`` / ``context_id`` to ``memories`` as nullable
columns and backfilled what it could. Every write path has required
``workspace_id`` since (``MemoryService`` raises on an empty one), so a NULL is
legacy data only — but #1946 made ``Memory.workspace_id == ws AND deleted_at IS
NULL`` the one definition of a workspace's live memory count, and a NULL row is
reachable through its context (recall, listing) while escaping the memory quota,
the plan page, the capacity-over lock (#1941) and the downgrade check.

Three steps, in the shape of ``b03_396`` (the same change on ``neural_memory_edges``):

1. **Purge tombstoned orphans, refuse on live ones.** A row with neither
   ``workspace_id`` nor ``context_id`` cannot be filled from anywhere. One that is
   already soft-deleted is not user-visible content and would only ever be
   hard-deleted by the tombstone sweep, so it is removed here (count logged).
   A *live* one is user content: the migration stops with the count and sample
   ids and leaves the decision (set the ids by hand after looking at
   ``user_id``, or delete the rows) to the operator. With ``context_id`` present
   the context row exists — ``memories.context_id`` is ``ON DELETE CASCADE`` — so
   every other NULL is fillable.
2. **Backfill only NULLs.** ``UPDATE ... WHERE m.workspace_id IS NULL`` — a row that
   already carries a workspace is never rewritten, even when it disagrees with
   its context's (no ``COALESCE`` over existing values; see the b03_396 lesson).
   Rows that disagree are an audit matter, not a migration one: the pre-check
   SQL in ``docs/deployment.md`` counts them, so no extra scan runs here.
3. **Enforce.** ``SET NOT NULL`` scans the table once to prove there is no NULL.

Locking: ``alembic/env.py`` runs the whole upgrade in one transaction, so the
``ACCESS EXCLUSIVE`` lock the ``ALTER`` takes is held from that statement to
commit, and a ``CHECK ... NOT VALID`` / ``VALIDATE`` detour would not shorten it
(the b03_396 docstring overstates that). The backfill touches legacy rows only,
and the scan is a single pass over ``memories``; expect the table to be
unavailable for about the duration of that pass. A refusal in step 1 rolls the
transaction back, so nothing is written.

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

# Rows nothing can fill: no workspace and no context to read one from.
# Already soft-deleted ones are purged; live ones stop the migration.
TOMBSTONED_ORPHAN_PURGE_SQL = (
    "DELETE FROM memories "
    "WHERE workspace_id IS NULL AND context_id IS NULL AND deleted_at IS NOT NULL"
)
ORPHAN_COUNT_SQL = (
    "SELECT count(*) FROM memories "
    "WHERE workspace_id IS NULL AND context_id IS NULL AND deleted_at IS NULL"
)
ORPHAN_SAMPLE_SQL = (
    "SELECT id FROM memories "
    "WHERE workspace_id IS NULL AND context_id IS NULL AND deleted_at IS NULL "
    "ORDER BY created_at LIMIT 10"
)

# Only NULLs are written; an existing value is never overwritten.
BACKFILL_SQL = (
    "UPDATE memories AS m SET workspace_id = c.workspace_id "
    "FROM contexts AS c "
    "WHERE m.context_id = c.id AND m.workspace_id IS NULL"
)


def upgrade() -> None:
    conn = op.get_bind()

    purged = conn.execute(sa.text(TOMBSTONED_ORPHAN_PURGE_SQL)).rowcount
    if purged:
        logger.info("#1951: purged %d soft-deleted memories with no workspace or context", purged)

    orphans = conn.execute(sa.text(ORPHAN_COUNT_SQL)).scalar_one()
    if orphans:
        sample = [str(row) for row in conn.execute(sa.text(ORPHAN_SAMPLE_SQL)).scalars()]
        raise RuntimeError(
            f"#1951: {orphans} live memories have neither workspace_id nor context_id and "
            "cannot be backfilled. Give each a workspace_id (and context_id) by hand after "
            "checking its user_id, or delete the rows, then run the migration again. "
            f"Sample ids: {sample}"
        )

    result = conn.execute(sa.text(BACKFILL_SQL))
    logger.info("#1951: backfilled workspace_id on %d memories from contexts", result.rowcount)

    op.alter_column(
        "memories",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )


def downgrade() -> None:
    # The column becomes nullable again; the values the backfill wrote stay
    # (they are correct data, not something to undo — same as b03_396).
    op.alter_column(
        "memories",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
