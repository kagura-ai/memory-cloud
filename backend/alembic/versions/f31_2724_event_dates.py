"""Event dates: generated columns memories.event_start / event_end + partial index.

``details.event_phrase`` (a relative date as the material states it) is
resolved at write time into ``details.event_start`` / ``details.event_end``
(``YYYY-MM-DD``) by ``MemoryService._apply_event_dates``. This revision
materialises both the way f16_2330 did ``observed_at``:

1. Two ``VARCHAR(10) GENERATED ALWAYS AS (...) STORED`` columns: a value in
   the date form is copied, anything else is NULL (raw-SQL defence: never an
   INSERT error). The expressions are byte-identical to
   ``models.memory.EVENT_START_SQL`` / ``EVENT_END_SQL`` (pinned by
   tests/utils/test_event_dates.py). Ten fixed characters, so lexical order
   is chronological under any collation.
2. One partial btree ``idx_memories_event_start (context_id, event_start)
   WHERE event_start IS NOT NULL AND deleted_at IS NULL``, built CONCURRENTLY
   in an autocommit block (the f17 form). The overlap query behind
   ``event_after`` / ``event_before`` — ``event_start < :before AND event_end
   >= :after`` within one context — walks the context's ``event_start``
   range through it and checks ``event_end`` on the rows it selects; a
   context holds few rows per window, so a second index on ``event_end``
   would cost more on every write than it saves on a read.

No backfill: no row written before this revision carries these keys.

Maintenance window: adding a STORED generated column rewrites the whole
``memories`` table under an ACCESS EXCLUSIVE lock (as f16 did). The
migration ``lock_timeout`` that env.py installs on every transaction
(``KAGURA_MIGRATION_LOCK_TIMEOUT``, default 5s) caps only the wait for that
lock, not the rewrite. Both ALTERs use ``IF NOT EXISTS`` and commit before
the index build, so an interrupted run is simply run again; the INVALID-index
probe (``db.migration_helpers.index_is_invalid``) drops a leftover of an
interrupted concurrent build before the ``IF NOT EXISTS`` create. The probe
reads the database, so the revision does not run offline (``--sql``).

Downgrade drops the index (CONCURRENTLY, in its own autocommit block) and
both columns; ``details`` keeps the values.

Revision ID: f31_2724_event_dates
Revises: f30_resource_management_grants
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import index_is_invalid

# revision identifiers, used by Alembic.
revision: str = "f31_2724_event_dates"
down_revision: str | None = "f30_resource_management_grants"
branch_labels: str | None = None
depends_on: str | None = None

# Byte-identical to models.memory.EVENT_START_SQL / EVENT_END_SQL (parity test).
EVENT_START_SQL = (
    "CASE WHEN details->>'event_start' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
    "THEN details->>'event_start' ELSE NULL END"
)
EVENT_END_SQL = (
    "CASE WHEN details->>'event_end' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
    "THEN details->>'event_end' ELSE NULL END"
)

_INDEX_NAME = "idx_memories_event_start"
# Literal DDL (identifiers cannot be bound parameters).
_INDEX_DDL = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_event_start "
    "ON memories (context_id, event_start) "
    "WHERE event_start IS NOT NULL AND deleted_at IS NULL"
)
_DROP_DDL = "DROP INDEX CONCURRENTLY IF EXISTS idx_memories_event_start"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS event_start VARCHAR(10) "
        f"GENERATED ALWAYS AS ({EVENT_START_SQL}) STORED"
    )
    op.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS event_end VARCHAR(10) "
        f"GENERATED ALWAYS AS ({EVENT_END_SQL}) STORED"
    )
    # The read-only probe runs in the transaction; the block commits it (the
    # columns are in place) and builds the index outside any transaction.
    invalid = index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(sa.text(_DROP_DDL))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(_DROP_DDL))
    op.execute("ALTER TABLE memories DROP COLUMN IF EXISTS event_start")
    op.execute("ALTER TABLE memories DROP COLUMN IF EXISTS event_end")
