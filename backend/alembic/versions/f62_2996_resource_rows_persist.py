"""#2996: rows the resource indexer owns become persistent, as the indexer now writes them.

The indexer used to create its rows with ``scope='working'``. The source owns
their lifecycle, so since v0.181.0 the indexer writes ``scope='persistent'``
and ``promoted_at`` (the two columns the PROMOTE statement sets) on a row it
creates and on a working row it takes over. This data migration moves the
rows left from before: every live working row that is resource-owned,

    scope = 'working' AND deleted_at IS NULL
    AND resource_owned_predicate() (models/memory.py: the full natural key,
    and a ``summary_embedding_id`` other than the row's own id)

whoever wrote it (the indexer's own rows, and ``remember(external_id)`` rows
it took over and re-pointed), gets ``scope='persistent'`` and ``promoted_at``
= the time of the update (UTC). Nothing else changes: not ``version`` (the
canonical-index check of a sync target requires 1), not ``updated_at``, not
the vector point (a resource point's payload carries neither ``scope`` nor
``promoted_at``). Tombstones, rows that name their own point (other writers'
rows, and the copies a context merge makes of indexer rows, whose point is
built like any memory's and does carry ``scope``) stay as they are.

The walk runs in ``op.get_context().autocommit_block()`` so each batch commits
on its own (``alembic/env.py`` runs an upgrade in one transaction, which would
hold every row lock until the end): BATCH_SIZE rows per statement, in id order
from the primary key, each batch locking its rows with ``FOR NO KEY UPDATE``
(the lock the UPDATE takes anyway) and ``SKIP LOCKED``, so the walk never
waits on a row a request holds; the UPDATE repeats the predicate. A skipped
row stays working; the final log line counts the rows left, and
``RERUN_SQL`` (docs/deployment.md) moves them when run again.

Locks: ``ROW EXCLUSIVE`` on ``memories`` (the lock of any UPDATE; it conflicts
with no DML) and row locks on the updated rows, held for their batch only. No
other table is touched. The session ``lock_timeout`` is the migration's
(``KAGURA_MIGRATION_LOCK_TIMEOUT``, default 5s).

Order: either way round. The previous release keeps creating working rows
until it stops; run ``RERUN_SQL`` once it is stopped (it matches exactly the
rows to move, so it is always safe to run again). A sync resume preview opened
before the migration no longer matches (its snapshot hash covers ``scope`` and
``promoted_at``): its confirmation is refused with "Resume diff changed;
obtain a fresh preview", and a fresh preview is prepared.

Offline (``alembic upgrade --sql``) there are no results to drive the walk, so
the migration emits ``RERUN_SQL`` in the script's one transaction.

Downgrade is a no-op: a migrated row cannot be told from one the new code
created persistent, and no version of the code needs these rows working.

Revision ID: f62_2996_resource_rows_persist
Revises: f61_2984_keyword_lengths
"""

import logging

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import migration_lock_timeout, session_lock_timeout

revision: str = "f62_2996_resource_rows_persist"
down_revision: str | None = "f61_2984_keyword_lengths"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Rows updated per statement (each commits on its own).
BATCH_SIZE = 1000

# A progress line every this many batches.
PROGRESS_EVERY = 100

# The rows to move, on the alias ``m``, written out in each statement below
# (fixed SQL, nothing comes from input; the tests run every statement):
#
#   m.scope = 'working' AND m.deleted_at IS NULL
#   AND (resource_owned_predicate() of models/memory.py:)
#   m.resource_id IS NOT NULL AND m.resource_doc_id IS NOT NULL
#   AND m.resource_version IS NOT NULL AND m.summary_embedding_id IS NOT NULL
#   AND m.summary_embedding_id <> m.id
#
# ``promoted_at`` is the time PROMOTE stamps (``utcnow()``): naive UTC.

# The whole change in one statement: the offline form and the documented rerun.
RERUN_SQL = """
UPDATE memories AS m SET scope = 'persistent', promoted_at = timezone('UTC', now())
WHERE m.scope = 'working' AND m.deleted_at IS NULL
  AND m.resource_id IS NOT NULL AND m.resource_doc_id IS NOT NULL
  AND m.resource_version IS NOT NULL AND m.summary_embedding_id IS NOT NULL
  AND m.summary_embedding_id <> m.id
"""

COUNT_SQL = """
SELECT count(*) FROM memories AS m
WHERE m.scope = 'working' AND m.deleted_at IS NULL
  AND m.resource_id IS NOT NULL AND m.resource_doc_id IS NOT NULL
  AND m.resource_version IS NOT NULL AND m.summary_embedding_id IS NOT NULL
  AND m.summary_embedding_id <> m.id
"""

# One batch: the next BATCH_SIZE rows to move after the previous batch's last
# id (``:after``; the first batch starts at the nil uuid, which no row has),
# locked in id order skipping rows a request holds, and updated; the UPDATE
# repeats the predicate. Returns the batch's last id (NULL once done) and the
# rows updated.
BATCH_SQL = """
WITH batch AS (
  SELECT m.id FROM memories AS m
  WHERE m.scope = 'working' AND m.deleted_at IS NULL
    AND m.resource_id IS NOT NULL AND m.resource_doc_id IS NOT NULL
    AND m.resource_version IS NOT NULL AND m.summary_embedding_id IS NOT NULL
    AND m.summary_embedding_id <> m.id
    AND m.id > CAST(:after AS uuid)
  ORDER BY m.id LIMIT :batch_size
  FOR NO KEY UPDATE SKIP LOCKED
), moved AS (
  UPDATE memories AS m SET scope = 'persistent', promoted_at = timezone('UTC', now())
  FROM batch
  WHERE m.id = batch.id
    AND m.scope = 'working' AND m.deleted_at IS NULL
    AND m.resource_id IS NOT NULL AND m.resource_doc_id IS NOT NULL
    AND m.resource_version IS NOT NULL AND m.summary_embedding_id IS NOT NULL
    AND m.summary_embedding_id <> m.id
  RETURNING m.id
)
SELECT (SELECT id FROM batch ORDER BY id DESC LIMIT 1) AS last_id,
       (SELECT count(*) FROM moved) AS moved
"""

_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def persist_resource_rows(conn: sa.engine.Connection, *, batch_size: int = BATCH_SIZE) -> int:
    """Move the rows batch by batch; return how many moved.

    Each statement commits on its own in an autocommit block; inside a
    transaction (a test) they all belong to it.
    """
    batch = sa.text(BATCH_SQL)
    moved = batches = 0
    after = _NIL_UUID
    try:
        while True:
            row = conn.execute(batch, {"after": str(after), "batch_size": batch_size}).one()
            if row.last_id is None:
                break
            batches += 1
            moved += row.moved
            after = row.last_id
            if batches % PROGRESS_EVERY == 0:
                logger.info("#2996: %d resource rows made persistent so far", moved)
    except Exception:
        # The batches before the failure committed. No further statement here:
        # the connection may be unusable, and the original error is what fails
        # the migration (a rerun resumes).
        logger.info(
            "#2996: %d resource rows made persistent before the walk failed; "
            "running the migration again resumes",
            moved,
        )
        raise
    left = conn.execute(sa.text(COUNT_SQL)).scalar_one()
    logger.info(
        "#2996: %d resource rows made persistent; %d left working "
        "(held by a request, or written since: run RERUN_SQL, docs/deployment.md)",
        moved,
        left,
    )
    return moved


def upgrade() -> None:
    """Make the indexer's working rows persistent (see the module docstring)."""
    if op.get_context().as_sql:
        op.execute(sa.text(RERUN_SQL))
        return

    with op.get_context().autocommit_block():
        with session_lock_timeout(migration_lock_timeout()):
            persist_resource_rows(op.get_bind())


def downgrade() -> None:
    """No-op: a migrated row cannot be told from a new one (see the module docstring)."""
