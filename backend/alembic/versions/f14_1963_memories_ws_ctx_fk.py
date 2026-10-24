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
3. **Refuse on orphans**: a memory whose context row is missing cannot exist
   while the ``context_id`` FK holds; if any appear, the migration stops with
   the count and sample ids before writing a row.
4. **Repair memories, the context's workspace wins**: a keyset walk over the
   cross-rooted rows in id order, BATCH_SIZE per statement, each committing
   on its own. Rows are locked in id order (#1985) and not skipped (a skipped
   row would fail VALIDATE); a row wait is capped by the session
   lock_timeout. The UPDATE re-checks the mismatch, so a rerun only touches
   what is left. A repaired live row (the memory and its context not
   soft-deleted) is set ``embedding_status = 'pending'``, as e95 did: its
   Qdrant point and chunk points carry the old workspace, so recall (filtered
   by the context's workspace) misses it until the pending-embedding sweep
   re-upserts them. ``updated_at`` is not touched (#1852).
5. **VALIDATE** (SHARE UPDATE EXCLUSIVE on ``memories``, ROW SHARE on
   ``contexts``; DML unaffected), skipped once validated.
6. **Repair edges the same way**: a ``neural_memory_edges`` row whose
   workspace differs from its context's is moved to the context's workspace.
   An edge carries its endpoints' workspace (the write invariant in
   ``repositories/neural_edge.py``), so the edges of the memories step 4
   moved would otherwise be left in the old workspace, where no graph read
   finds them and ``scripts/audit_edge_context_invariant.py --fix`` would
   delete them. Same keyset walk (bigint id); ``unique_edge`` does not
   include the workspace, so the move cannot collide.

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

# Memories whose context row is gone. The total rides along on each sample.
ORPHAN_SQL = (
    "SELECT m.id, count(*) OVER () AS total FROM memories AS m "
    "WHERE m.context_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM contexts AS c WHERE c.id = m.context_id) "
    "ORDER BY m.id LIMIT 10"
)

# The busiest (old workspace -> context's workspace) moves, for the log.
PAIRS_SQL = (
    "SELECT m.workspace_id AS old_ws, c.workspace_id AS new_ws, count(*) AS n "
    "FROM memories AS m JOIN contexts AS c ON c.id = m.context_id "
    "WHERE m.workspace_id <> c.workspace_id "
    "GROUP BY m.workspace_id, c.workspace_id ORDER BY n DESC LIMIT 20"
)

# One keyset step over the cross-rooted memories after the previous step's
# last id (``{after}``): lock the next BATCH_SIZE in id order (#1985), move
# each still mismatched one to its context's workspace, re-queue the live ones
# (see the module docstring). Returns the step's last id (NULL once the walk
# is done), the rows moved and re-queued, and up to ten of their ids.
_MEMORY_BATCH_SQL = (
    "WITH batch AS ("
    "  SELECT m.id FROM memories AS m JOIN contexts AS c ON c.id = m.context_id"
    "  WHERE m.workspace_id <> c.workspace_id {after}"
    "  ORDER BY m.id LIMIT :batch_size FOR UPDATE OF m"
    "), moved AS ("
    "  UPDATE memories AS m SET workspace_id = c.workspace_id,"
    "  embedding_status = CASE WHEN m.deleted_at IS NULL AND c.deleted_at IS NULL"
    "  THEN 'pending' ELSE m.embedding_status END"
    "  FROM batch, contexts AS c"
    "  WHERE m.id = batch.id AND c.id = m.context_id AND m.workspace_id <> c.workspace_id"
    "  RETURNING m.id, (m.deleted_at IS NULL AND c.deleted_at IS NULL) AS requeued"
    ") "
    "SELECT (SELECT id FROM batch ORDER BY id DESC LIMIT 1) AS last_id, "
    "(SELECT count(*) FROM moved) AS moved, "
    "(SELECT count(*) FROM moved WHERE requeued) AS requeued, "
    "ARRAY(SELECT id FROM moved ORDER BY id LIMIT 10) AS sample"
)
FIRST_MEMORY_BATCH_SQL = _MEMORY_BATCH_SQL.format(after="")
NEXT_MEMORY_BATCH_SQL = _MEMORY_BATCH_SQL.format(after="AND m.id > CAST(:after AS uuid)")

# The same walk over neural_memory_edges (bigint id).
_EDGE_BATCH_SQL = (
    "WITH batch AS ("
    "  SELECT e.id FROM neural_memory_edges AS e JOIN contexts AS c ON c.id = e.context_id"
    "  WHERE e.workspace_id <> c.workspace_id {after}"
    "  ORDER BY e.id LIMIT :batch_size FOR UPDATE OF e"
    "), moved AS ("
    "  UPDATE neural_memory_edges AS e SET workspace_id = c.workspace_id"
    "  FROM batch, contexts AS c"
    "  WHERE e.id = batch.id AND c.id = e.context_id AND e.workspace_id <> c.workspace_id"
    "  RETURNING e.id"
    ") "
    "SELECT (SELECT id FROM batch ORDER BY id DESC LIMIT 1) AS last_id, "
    "(SELECT count(*) FROM moved) AS moved"
)
FIRST_EDGE_BATCH_SQL = _EDGE_BATCH_SQL.format(after="")
NEXT_EDGE_BATCH_SQL = _EDGE_BATCH_SQL.format(after="AND e.id > :after")


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
    """Run a keyset walk to its end; return each step's result row."""
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
    """Steps 3 and 4: refuse on orphans, then move rows to their context's workspace."""
    orphans = conn.execute(sa.text(ORPHAN_SQL)).fetchall()
    if orphans:
        raise RuntimeError(
            f"#1963: {orphans[0].total} memories name a context that does not exist, so "
            "their workspace cannot be checked. Delete the rows or give them an existing "
            "context, then run the migration again. "
            f"Sample ids: {[str(row.id) for row in orphans]}"
        )

    pairs = conn.execute(sa.text(PAIRS_SQL)).fetchall()
    steps = _walk(conn, FIRST_MEMORY_BATCH_SQL, NEXT_MEMORY_BATCH_SQL)
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
    """Step 6: move edges to their context's workspace."""
    moved = sum(step.moved for step in _walk(conn, FIRST_EDGE_BATCH_SQL, NEXT_EDGE_BATCH_SQL))
    if moved:
        logger.info("#1963: moved %d neural_memory_edges to their context's workspace", moved)


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
            _repair_memories(conn)
            if fk_state is not True:
                conn.execute(sa.text(f"ALTER TABLE memories VALIDATE CONSTRAINT {_FK_NAME}"))
            _repair_edges(conn)
        finally:
            conn.execute(sa.text("RESET lock_timeout"))


def downgrade() -> None:
    """Drop the FK, then its index. Repaired workspace ids stay."""
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    op.execute(sa.text(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {_FK_NAME}"))
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
