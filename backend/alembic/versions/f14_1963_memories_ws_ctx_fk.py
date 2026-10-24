"""#1963: a memory's (workspace_id, context_id) must match its context's.

``memories.workspace_id`` (NOT NULL since e95) and ``memories.context_id``
(FK ``contexts.id``) were independent: nothing stopped a row from naming one
workspace and a context of another. Such a "cross-rooted" row is counted
toward one workspace (``services/memory_counts.py``, by
``Memory.workspace_id``) while recall, listing and the timeline reach it
through its context's. Every write path stamps the context's workspace, so
these rows are legacy data (plus the admin context-recovery path, fixed in
the same change). This migration repairs them and adds the constraint that
keeps new ones out:

- ``ux_contexts_workspace_id_id``: UNIQUE ``contexts (workspace_id, id)``,
  the key the FK references (a plain, non-partial unique index is a valid FK
  target);
- ``fk_memories_workspace_context``: ``FOREIGN KEY (workspace_id, context_id)
  REFERENCES contexts (workspace_id, id) ON DELETE CASCADE``, MATCH SIMPLE (a
  row with ``context_id IS NULL`` stays legal), ON UPDATE NO ACTION (a
  context with memories never moves to another workspace). It sits beside the
  existing single-column ``context_id`` FK, which is left alone: both are ON
  DELETE CASCADE, so a context hard delete removes the same rows as before.

Steps, each re-runnable:

1. **Index**, ``CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS`` inside
   ``autocommit_block()`` (SHARE UPDATE EXCLUSIVE on ``contexts``, no DML
   blocked). An INVALID leftover of an interrupted build is dropped first
   (e62 guard). No lock_timeout here: CONCURRENTLY waits for older
   transactions by design.
2. **FK NOT VALID**, only when absent, in the migration transaction under
   ``SET LOCAL lock_timeout``: SHARE ROW EXCLUSIVE on ``memories`` and
   ``contexts`` for as long as the ALTER takes (no scan), released when the
   next block commits. Reads continue; writers wait at most the lock_timeout,
   and a timeout fails the migration (run it again). From here on, no writer
   (old pods included) can create a cross-rooted row, so the set below only
   shrinks.
3. **Detect** in one read: every memory with a context whose workspace
   differs, and every memory whose context row is missing. The second set
   cannot exist while the ``context_id`` FK holds; if any appear, the
   migration stops with the count and sample ids before writing a row.
4. **Repair, the context's workspace wins**, in id-ordered batches of
   BATCH_SIZE, each statement committing on its own. Rows are locked in id
   order (#1985) and not skipped (a skipped row would fail VALIDATE); a row
   wait is capped by the session lock_timeout. The UPDATE re-checks the
   mismatch, so a rerun only touches what is left. A repaired live row (the
   memory and its context not soft-deleted) is set ``embedding_status =
   'pending'``, as e95 did: its Qdrant point and chunk points carry the old
   workspace, so recall (filtered by the context's workspace) misses it until
   the pending-embedding sweep re-upserts them. ``updated_at`` is not touched
   (#1852).
5. **VALIDATE** (SHARE UPDATE EXCLUSIVE on ``memories``, ROW SHARE on
   ``contexts``; DML unaffected), skipped once validated.
6. **Audit only**: the count of ``neural_memory_edges`` whose workspace
   differs from their context's, with a pointer to
   ``backend/scripts/audit_edge_context_invariant.py``. Edges are not changed.

The log (alembic logger) gives the rows repaired and re-queued, the top
(old workspace -> new workspace) pairs, sample ids and the edge count, or says
there were no cross-rooted memories. A repaired row now counts toward its
context's workspace and can push it over its memory limit.

Blue-green: old pods already stamp the context's workspace, so their writes
pass the new FK. Only the admin context recovery with a mismatched
workspace would now fail, which its fix turns into a 400.

Downgrade drops the FK (brief ACCESS EXCLUSIVE, under lock_timeout) and then
the index CONCURRENTLY; the FK goes first because it depends on the index.
Repaired workspace ids stay (correct data, as in e95 / b03_396).

Revision ID: f14_1963_memories_ws_ctx_fk
Revises: f13_1952_api_keys_connector_id

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import logging
from collections import Counter

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f14_1963_memories_ws_ctx_fk"
down_revision: str | None = "f13_1952_api_keys_connector_id"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

_INDEX_NAME = "ux_contexts_workspace_id_id"
_INDEX_DDL = (
    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} ON contexts (workspace_id, id)"
)
_FK_NAME = "fk_memories_workspace_context"

# Rows per repair statement (each commits on its own).
BATCH_SIZE = 500

# How long the ALTER and each repair batch may wait for a lock before the
# migration fails fast (to be rerun — it resumes) instead of stalling writes.
LOCK_TIMEOUT = "5s"

# Memories whose context names another workspace, and memories whose context
# row is gone (``orphan``). One lock-free read, ordered for the batches.
DETECT_SQL = (
    "SELECT m.id, m.workspace_id AS old_ws, c.workspace_id AS new_ws, "
    "c.id IS NULL AS orphan "
    "FROM memories AS m LEFT JOIN contexts AS c ON c.id = m.context_id "
    "WHERE m.context_id IS NOT NULL "
    "AND (c.id IS NULL OR m.workspace_id <> c.workspace_id) "
    "ORDER BY m.id"
)

# One batch: lock the rows in id order (#1985), then move each still
# mismatched row to its context's workspace. A live row is re-queued for
# embedding (see the module docstring); a soft-deleted one keeps its status.
REPAIR_SQL = (
    "WITH locked AS ("
    "  SELECT id FROM memories WHERE id = ANY(CAST(:ids AS uuid[]))"
    "  ORDER BY id FOR UPDATE"
    ") "
    "UPDATE memories AS m SET workspace_id = c.workspace_id, "
    "embedding_status = CASE WHEN m.deleted_at IS NULL AND c.deleted_at IS NULL "
    "THEN 'pending' ELSE m.embedding_status END "
    "FROM locked, contexts AS c "
    "WHERE m.id = locked.id AND c.id = m.context_id "
    "AND m.workspace_id <> c.workspace_id "
    "RETURNING m.id, (m.deleted_at IS NULL AND c.deleted_at IS NULL) AS requeued"
)

EDGE_AUDIT_SQL = (
    "SELECT count(*) FROM neural_memory_edges AS e "
    "JOIN contexts AS c ON c.id = e.context_id "
    "WHERE e.workspace_id <> c.workspace_id"
)


def _index_is_invalid(name: str) -> bool:
    """True if ``name`` exists in ``pg_index`` in an INVALID state (e62 guard).

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    with ``indisvalid = false``; a retry's ``IF NOT EXISTS`` would skip it
    without rebuilding, so the leftover must be dropped first.
    """
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 "
                "FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND i.indisvalid IS FALSE"
            ),
            {"name": name},
        )
        .first()
    )
    return row is not None


def _fk_state() -> bool | None:
    """None if the FK is absent, else whether it is validated."""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conrelid = 'memories'::regclass AND conname = :name"
            ),
            {"name": _FK_NAME},
        )
        .first()
    )
    return None if row is None else bool(row[0])


def _repair(conn: sa.engine.Connection) -> None:
    """Steps 3 and 4: refuse on orphans, then move rows to their context's workspace."""
    rows = conn.execute(sa.text(DETECT_SQL)).fetchall()
    orphans = [row.id for row in rows if row.orphan]
    if orphans:
        raise RuntimeError(
            f"#1963: {len(orphans)} memories name a context that does not exist, so "
            "their workspace cannot be checked. Delete the rows or give them an existing "
            "context, then run the migration again. "
            f"Sample ids: {[str(i) for i in orphans[:10]]}"
        )
    if not rows:
        logger.info("#1963: no cross-rooted memories")
        return

    moves = {row.id: (row.old_ws, row.new_ws) for row in rows}
    ids = sorted(moves)
    repaired: list = []
    requeued = 0
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start : start + BATCH_SIZE]
        for row in conn.execute(sa.text(REPAIR_SQL), {"ids": batch}).fetchall():
            repaired.append(row.id)
            requeued += bool(row.requeued)

    pairs = Counter(moves[i] for i in repaired).most_common(20)
    logger.info(
        "#1963: moved %d cross-rooted memories to their context's workspace "
        "(%d re-queued for embedding)",
        len(repaired),
        requeued,
    )
    for (old_ws, new_ws), count in pairs:
        logger.info("#1963:   %s -> %s: %d", old_ws, new_ws, count)
    logger.info("#1963: sample ids: %s", [str(i) for i in repaired[:10]])


def _audit_edges(conn: sa.engine.Connection) -> None:
    """Step 6: count edges outside their context's workspace (audit only, never fatal)."""
    try:
        count = conn.execute(sa.text(EDGE_AUDIT_SQL)).scalar()
    except DBAPIError as exc:
        logger.warning("#1963: edge audit skipped: %s", exc)
        return
    if count:
        logger.warning(
            "#1963: %d neural_memory_edges name a workspace other than their context's; "
            "review them with backend/scripts/audit_edge_context_invariant.py",
            count,
        )


def upgrade() -> None:
    """Index, FK NOT VALID, repair, VALIDATE (see the module docstring)."""
    invalid = _index_is_invalid(_INDEX_NAME)
    fk_state = _fk_state()

    # Step 1: the referenced unique key, built without blocking DML.
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))

    # Step 2: the FK, NOT VALID (no scan). Its lock is released when the
    # block below commits this transaction.
    if fk_state is None:
        op.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        op.execute(
            sa.text(
                f"ALTER TABLE memories ADD CONSTRAINT {_FK_NAME} "
                "FOREIGN KEY (workspace_id, context_id) "
                "REFERENCES contexts (workspace_id, id) ON DELETE CASCADE NOT VALID"
            )
        )

    # Steps 3-6, each statement committing on its own.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(sa.text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        try:
            _repair(conn)
            if fk_state is not True:
                conn.execute(sa.text(f"ALTER TABLE memories VALIDATE CONSTRAINT {_FK_NAME}"))
            _audit_edges(conn)
        finally:
            conn.execute(sa.text("RESET lock_timeout"))


def downgrade() -> None:
    """Drop the FK, then its index. Repaired workspace ids stay."""
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    op.execute(sa.text(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {_FK_NAME}"))
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
