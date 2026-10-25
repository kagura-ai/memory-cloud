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
3. **Collect** in one lock-free hash-join pass over ``memories`` into a
   session temp table: every memory whose context names another workspace,
   and every memory whose context row is missing. The second set cannot
   exist while the ``context_id`` FK holds; if any appear, the migration stops
   with the count and sample ids before writing a row.
4. **Repair memories, the context's workspace wins**, over the collected ids
   in id order, BATCH_SIZE per statement, each committing on its own. Rows
   are locked in id order (#1985) only for their own batch and not skipped (a
   skipped row would fail VALIDATE); a row wait is capped by the session
   lock_timeout. The UPDATE re-checks the mismatch, so a rerun (which
   collects again) only touches what is left. A repaired live row (the memory
   and its context not soft-deleted) is set ``embedding_status = 'pending'``
   with a fresh retry budget (``embedding_retry_count = 0``,
   ``embedding_error`` cleared, as ``context_restore.py`` does), as e95 did:
   its Qdrant point and chunk points carry the old workspace, so recall
   (filtered by the context's workspace) misses it until the pending-embedding
   sweep re-upserts them. ``updated_at`` is not touched (#1852).
5. **VALIDATE** (SHARE UPDATE EXCLUSIVE on ``memories``, ROW SHARE on
   ``contexts``; DML unaffected). Once the FK is validated (a rerun after
   step 6 failed), steps 3-5 are skipped: no memory is left to move.
6. **Repair edges the same way**: a ``neural_memory_edges`` row whose
   workspace differs from its context's is moved to the context's workspace.
   An edge carries its endpoints' workspace (the write invariant in
   ``repositories/neural_edge.py``), so the edges of the memories step 4
   moved would otherwise be left in the old workspace, where no graph read
   finds them and ``scripts/audit_edge_context_invariant.py --fix`` would
   delete them. Same collect-then-batch shape (bigint id); ``unique_edge``
   does not include the workspace, so the move cannot collide. The batches
   take row locks only, not the ``hebbian:<user_id>`` advisory lock: a
   collision with a Sleep or batch-task write fails one side, and the
   migration can be rerun.

``statement_timeout`` is set to 0 for the migration's session in both
autocommit blocks: the index build, the collecting scans and VALIDATE read
whole tables, and a build a role-level timeout killed would leave an INVALID
index for every rerun to rebuild again.

The log (alembic logger) gives the memories moved and re-queued, the top
(old workspace -> new workspace) pairs, sample ids and the edges moved, or
says there was nothing to move. A repaired memory now counts toward its
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

import sqlalchemy as sa

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

# Session-scoped tables holding the rows to move, collected in one lock-free
# pass each (the set can only shrink once the FK is in place, step 2).
_MEMORY_MOVES = "f14_memory_moves"
_EDGE_MOVES = "f14_edge_moves"

# Every memory whose context names another workspace, and every memory whose
# context row is gone (``orphan``): one hash-join pass over ``memories``.
COLLECT_MEMORIES_SQL = (
    f"CREATE TEMP TABLE {_MEMORY_MOVES} AS "
    "SELECT m.id, m.workspace_id AS old_ws, c.workspace_id AS new_ws, "
    "c.id IS NULL AS orphan "
    "FROM memories AS m LEFT JOIN contexts AS c ON c.id = m.context_id "
    "WHERE m.context_id IS NOT NULL "
    "AND (c.id IS NULL OR m.workspace_id <> c.workspace_id)"
)
ORPHAN_SQL = (
    f"SELECT id, count(*) OVER () AS total FROM {_MEMORY_MOVES} WHERE orphan ORDER BY id LIMIT 10"
)
# The busiest (old workspace -> context's workspace) moves, for the log.
PAIRS_SQL = (
    f"SELECT old_ws, new_ws, count(*) AS n FROM {_MEMORY_MOVES} WHERE NOT orphan "
    "GROUP BY old_ws, new_ws ORDER BY n DESC LIMIT 20"
)

# One batch: the next BATCH_SIZE collected ids after the previous batch's
# last one (``{after}``), locked in id order (#1985) and not skipped, each
# still mismatched one moved to its context's workspace. A live one is
# re-queued for embedding with a fresh retry budget (as context_restore.py
# does, #979). Returns the batch's last id (NULL once done), the rows moved
# and re-queued, and up to ten of their ids.
_MEMORY_BATCH_SQL = (
    "WITH ids AS ("
    f"  SELECT id FROM {_MEMORY_MOVES} WHERE NOT orphan {{after}}"
    "  ORDER BY id LIMIT :batch_size"
    "), locked AS ("
    "  SELECT m.id FROM memories AS m JOIN ids ON ids.id = m.id"
    "  ORDER BY m.id FOR UPDATE OF m"
    "), moved AS ("
    "  UPDATE memories AS m SET workspace_id = c.workspace_id,"
    "  embedding_status = CASE WHEN m.deleted_at IS NULL AND c.deleted_at IS NULL"
    "  THEN 'pending' ELSE m.embedding_status END,"
    "  embedding_retry_count = CASE WHEN m.deleted_at IS NULL AND c.deleted_at IS NULL"
    "  THEN 0 ELSE m.embedding_retry_count END,"
    "  embedding_error = CASE WHEN m.deleted_at IS NULL AND c.deleted_at IS NULL"
    "  THEN NULL ELSE m.embedding_error END"
    "  FROM locked, contexts AS c"
    "  WHERE m.id = locked.id AND c.id = m.context_id AND m.workspace_id <> c.workspace_id"
    "  RETURNING m.id, (m.deleted_at IS NULL AND c.deleted_at IS NULL) AS requeued"
    ") "
    "SELECT (SELECT id FROM ids ORDER BY id DESC LIMIT 1) AS last_id, "
    "(SELECT count(*) FROM moved) AS moved, "
    "(SELECT count(*) FROM moved WHERE requeued) AS requeued, "
    "ARRAY(SELECT id FROM moved ORDER BY id LIMIT 10) AS sample"
)
FIRST_MEMORY_BATCH_SQL = _MEMORY_BATCH_SQL.format(after="")
NEXT_MEMORY_BATCH_SQL = _MEMORY_BATCH_SQL.format(after="AND id > CAST(:after AS uuid)")

# The same for neural_memory_edges (bigint id).
COLLECT_EDGES_SQL = (
    f"CREATE TEMP TABLE {_EDGE_MOVES} AS "
    "SELECT e.id FROM neural_memory_edges AS e JOIN contexts AS c ON c.id = e.context_id "
    "WHERE e.workspace_id <> c.workspace_id"
)
_EDGE_BATCH_SQL = (
    "WITH ids AS ("
    f"  SELECT id FROM {_EDGE_MOVES} {{after}} ORDER BY id LIMIT :batch_size"
    "), locked AS ("
    "  SELECT e.id FROM neural_memory_edges AS e JOIN ids ON ids.id = e.id"
    "  ORDER BY e.id FOR UPDATE OF e"
    "), moved AS ("
    "  UPDATE neural_memory_edges AS e SET workspace_id = c.workspace_id"
    "  FROM locked, contexts AS c"
    "  WHERE e.id = locked.id AND c.id = e.context_id AND e.workspace_id <> c.workspace_id"
    "  RETURNING e.id"
    ") "
    "SELECT (SELECT id FROM ids ORDER BY id DESC LIMIT 1) AS last_id, "
    "(SELECT count(*) FROM moved) AS moved"
)
FIRST_EDGE_BATCH_SQL = _EDGE_BATCH_SQL.format(after="")
NEXT_EDGE_BATCH_SQL = _EDGE_BATCH_SQL.format(after="WHERE id > :after")


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


def _walk(conn: sa.engine.Connection, first_sql: str, next_sql: str) -> list:
    """Run the batches over a collected id table to its end; return each batch's row."""
    steps = []
    after = None
    while True:
        if after is None:
            row = conn.execute(sa.text(first_sql), {"batch_size": BATCH_SIZE}).first()
        else:
            row = conn.execute(
                sa.text(next_sql), {"after": after, "batch_size": BATCH_SIZE}
            ).first()
        if row is None or row.last_id is None:
            return steps
        steps.append(row)
        after = row.last_id


def _repair_memories(conn: sa.engine.Connection) -> None:
    """Steps 3 and 4: collect, refuse on orphans, then move rows to their context's workspace."""
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_MEMORY_MOVES}"))
    conn.execute(sa.text(COLLECT_MEMORIES_SQL))
    conn.execute(sa.text(f"ALTER TABLE {_MEMORY_MOVES} ADD PRIMARY KEY (id)"))
    try:
        orphans = conn.execute(sa.text(ORPHAN_SQL)).fetchall()
        if orphans:
            raise RuntimeError(
                f"#1963: {orphans[0].total} memories name a context that does not exist, "
                "so their workspace cannot be checked. Delete the rows or give them an "
                "existing context, then run the migration again. "
                f"Sample ids: {[str(row.id) for row in orphans]}"
            )
        pairs = conn.execute(sa.text(PAIRS_SQL)).fetchall()
        steps = _walk(conn, FIRST_MEMORY_BATCH_SQL, NEXT_MEMORY_BATCH_SQL)
    finally:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {_MEMORY_MOVES}"))

    moved = sum(step.moved for step in steps)
    if not moved:
        logger.info("#1963: no cross-rooted memories")
        return
    logger.info(
        "#1963: moved %d cross-rooted memories to their context's workspace "
        "(%d re-queued for embedding)",
        moved,
        sum(step.requeued for step in steps),
    )
    for pair in pairs:
        logger.info("#1963:   %s -> %s: %d", pair.old_ws, pair.new_ws, pair.n)
    sample = [str(i) for step in steps for i in step.sample][:10]
    logger.info("#1963: sample ids: %s", sample)


def _repair_edges(conn: sa.engine.Connection) -> None:
    """Step 6: collect, then move edges to their context's workspace."""
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_EDGE_MOVES}"))
    conn.execute(sa.text(COLLECT_EDGES_SQL))
    conn.execute(sa.text(f"ALTER TABLE {_EDGE_MOVES} ADD PRIMARY KEY (id)"))
    try:
        steps = _walk(conn, FIRST_EDGE_BATCH_SQL, NEXT_EDGE_BATCH_SQL)
    finally:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {_EDGE_MOVES}"))
    moved = sum(step.moved for step in steps)
    if moved:
        logger.info("#1963: moved %d neural_memory_edges to their context's workspace", moved)


def upgrade() -> None:
    """Index, FK NOT VALID, repair, VALIDATE (see the module docstring)."""
    invalid = _index_is_invalid(_INDEX_NAME)
    fk_state = _fk_state()

    # Step 1: the referenced unique key, built without blocking DML. No
    # statement_timeout: a build it killed would leave an INVALID index for
    # every rerun to drop and rebuild again.
    with op.get_context().autocommit_block():
        op.execute(sa.text("SET statement_timeout = 0"))
        try:
            if invalid:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
            op.execute(sa.text(_INDEX_DDL))
        finally:
            op.execute(sa.text("RESET statement_timeout"))

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

    # Steps 3-6, each statement committing on its own. The collecting scans
    # and VALIDATE read whole tables, so no statement_timeout either.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(sa.text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        conn.execute(sa.text("SET statement_timeout = 0"))
        try:
            # A validated FK (a rerun after the edge step failed) means no
            # memory is left to move.
            if fk_state is not True:
                _repair_memories(conn)
                conn.execute(sa.text(f"ALTER TABLE memories VALIDATE CONSTRAINT {_FK_NAME}"))
            _repair_edges(conn)
        finally:
            conn.execute(sa.text("RESET lock_timeout"))
            conn.execute(sa.text("RESET statement_timeout"))


def downgrade() -> None:
    """Drop the FK, then its index. Repaired workspace ids stay."""
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    op.execute(sa.text(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {_FK_NAME}"))
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
