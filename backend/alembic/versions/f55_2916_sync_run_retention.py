"""#2916: keep a run's change count apart from its diff; index the run sweeps.

1. ``sync_runs.change_count`` (``INTEGER NULL``): the length of the run's
   diff, kept when the diff of an aborted, expired or superseded run is
   cleared (``services.sync_feed_runs.clear_diff``). Adding a nullable column
   without a default changes only the catalog: a brief ACCESS EXCLUSIVE lock
   on ``sync_runs``, no rewrite, under the migration ``lock_timeout``.
2. Backfill, after that ALTER commits, in one statement of its own (an
   autocommit block, the migration ``lock_timeout`` set for the session):
   every row with a stored diff gets its count, and the rows that can no
   longer commit (``aborted``, ``expired``) drop their diff, as the
   application now does when it ends a run. A diff an older image writes
   during the deploy has no count; every reader then counts the diff itself.
   The statement takes row locks only (ROW EXCLUSIVE on the table):
   readers go on, a request that locks one of those rows without waiting
   gets the usual retryable 409 meanwhile. A lock wait past the timeout fails
   the upgrade; it is run again and resumes (a row with a count no longer
   matches).
3. Three indexes, each built CONCURRENTLY in an autocommit block (the f17
   form; writes continue during the scan, an INVALID leftover of an
   interrupted build is dropped first):

   - ``ix_sync_runs_state_expires_at (state, expires_at)``: the expiry sweep
     and the deletion of never-committed runs past retention select by
     state and age every five minutes;
   - ``ix_sync_runs_committed_diff (committed_at) WHERE state = 'committed'
     AND diff IS NOT NULL``: the superseded-diff pass of the retention sweep
     reads only the committed runs that still hold a diff;
   - ``ix_sync_index_outbox_undelivered (resource_pk, context_id,
     last_event_id) WHERE delivered_at IS NULL``: the outbox replay and the
     live batch's receipt lock read only undelivered receipts.

The INVALID-index probes read the database, so the revision does not run
offline (``--sql``).

Downgrade drops the indexes (CONCURRENTLY) and the column. A diff cleared
before it stays NULL: the older code reads it as an empty diff, so such a
run then reports zero changes.

Revision ID: f55_2916_sync_run_retention
Revises: f54_2882_ordered_billing
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import index_is_invalid, migration_lock_timeout, session_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "f55_2916_sync_run_retention"
down_revision: str | None = "f54_2882_ordered_billing"
branch_labels: str | None = None
depends_on: str | None = None

BACKFILL = (
    "UPDATE sync_runs SET change_count = jsonb_array_length(diff), "
    "diff = CASE WHEN state IN ('aborted', 'expired') THEN NULL ELSE diff END "
    "WHERE change_count IS NULL AND jsonb_typeof(diff) = 'array'"
)

_RUNS_STATE = "ix_sync_runs_state_expires_at"
_RUNS_DIFF = "ix_sync_runs_committed_diff"
_OUTBOX = "ix_sync_index_outbox_undelivered"
# Literal DDL (identifiers cannot be bound parameters).
_CREATE_RUNS_STATE = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sync_runs_state_expires_at "
    "ON sync_runs (state, expires_at)"
)
_DROP_RUNS_STATE = "DROP INDEX CONCURRENTLY IF EXISTS ix_sync_runs_state_expires_at"
_CREATE_RUNS_DIFF = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sync_runs_committed_diff "
    "ON sync_runs (committed_at) WHERE state = 'committed' AND diff IS NOT NULL"
)
_DROP_RUNS_DIFF = "DROP INDEX CONCURRENTLY IF EXISTS ix_sync_runs_committed_diff"
_CREATE_OUTBOX = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sync_index_outbox_undelivered "
    "ON sync_index_outbox (resource_pk, context_id, last_event_id) "
    "WHERE delivered_at IS NULL"
)
_DROP_OUTBOX = "DROP INDEX CONCURRENTLY IF EXISTS ix_sync_index_outbox_undelivered"


def upgrade() -> None:
    op.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS change_count INTEGER")
    # The read-only probes run in the transaction; the block commits it (the
    # column is in place) and runs the rest outside any transaction.
    invalid = {name: index_is_invalid(name) for name in (_RUNS_STATE, _RUNS_DIFF, _OUTBOX)}
    with op.get_context().autocommit_block():
        with session_lock_timeout(migration_lock_timeout()):
            op.execute(sa.text(BACKFILL))
        if invalid[_RUNS_STATE]:
            op.execute(sa.text(_DROP_RUNS_STATE))
        op.execute(sa.text(_CREATE_RUNS_STATE))
        if invalid[_RUNS_DIFF]:
            op.execute(sa.text(_DROP_RUNS_DIFF))
        op.execute(sa.text(_CREATE_RUNS_DIFF))
        if invalid[_OUTBOX]:
            op.execute(sa.text(_DROP_OUTBOX))
        op.execute(sa.text(_CREATE_OUTBOX))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(_DROP_OUTBOX))
        op.execute(sa.text(_DROP_RUNS_DIFF))
        op.execute(sa.text(_DROP_RUNS_STATE))
    op.execute("ALTER TABLE sync_runs DROP COLUMN IF EXISTS change_count")
