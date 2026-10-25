"""#2330: WHEN-axis generated column memories.observed_at + partial index.

``details.observed_at`` is the time a fact held or an event happened (a date
``YYYY-MM-DD`` or a UTC datetime ``YYYY-MM-DDTHH:MM:SSZ``, validated by
``MemoryService._apply_observed_at``). This revision materialises it the way
e30_877 (trigger_from) and e69_1331 (location_lat / location_lon) do:

1. **Normalise legacy values.** Rows whose ``details.observed_at`` is present
   but in neither stored form (written before the contract, for example a
   ``+09:00`` datetime) are rewritten to the stored form when the value
   parses; the count is logged. Values that do not parse are left as they
   are (the column is NULL for them) and counted in the log. No
   ``updated_at`` stamp and no revision row: this is maintenance, like f15.
   The UPDATE repeats the old value in its WHERE, so a value written since
   the scan is never overwritten. The ``::jsonb`` round trip re-renders the
   few rewritten rows' JSON (key order, numerics such as ``5e-05``); the
   location regex accepts both numeric spellings (#1344).
2. **Generated column**, ``VARCHAR(32) GENERATED ALWAYS AS (...) STORED``: a
   date is padded to ``T00:00:00Z``, a UTC datetime is copied, anything else
   is NULL (raw-SQL defence: never an INSERT error). The expression is
   byte-identical to ``models.memory.OBSERVED_AT_SQL`` (pinned by
   tests/utils/test_observed_at.py).
3. **Partial btree** ``idx_memories_observed_at (context_id, observed_at)
   WHERE observed_at IS NOT NULL AND deleted_at IS NULL``.

NOTE — maintenance window: adding a STORED generated column **rewrites the
whole ``memories`` table** under an ACCESS EXCLUSIVE lock held until the
migration commits, so reads and writes of ``memories`` wait for the rewrite
and the index build (e30_877 and e69_1331 had the same shape). Check the
table's size first and apply it in a maintenance window. ``SET LOCAL
lock_timeout`` caps only the wait *for* the lock: if a long transaction holds
a conflicting lock, the migration fails fast instead of queueing every later
query behind it, and the operator runs it again (nothing is kept). It does
not shorten the rewrite itself.

Qdrant points written before this revision carry no ``observed_at`` payload;
``python -m cli.observed_at_backfill`` adds it (docs/deployment.md).

Downgrade drops the index and the column (``details`` keeps the values).

Revision ID: f16_2330_observed_at
Revises: f15_2026_json_none_as_null

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import logging
from datetime import UTC, date, datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f16_2330_observed_at"
down_revision: str | None = "f15_2026_json_none_as_null"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

# How long the ALTER may wait for its ACCESS EXCLUSIVE lock before the
# migration fails fast (to be rerun) instead of queueing the whole API.
LOCK_TIMEOUT = "5s"

# Byte-identical to models.memory.OBSERVED_AT_SQL (parity test).
OBSERVED_AT_SQL = (
    "CASE WHEN details->>'observed_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
    "THEN (details->>'observed_at') || 'T00:00:00Z' "
    "WHEN details->>'observed_at' ~ "
    "'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' "
    "THEN details->>'observed_at' "
    "ELSE NULL END"
)

LEGACY_SQL = (
    "SELECT id, details->>'observed_at' AS value FROM memories "
    "WHERE details->>'observed_at' IS NOT NULL "
    "AND details->>'observed_at' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
    "AND details->>'observed_at' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' "
    "ORDER BY id"
)

REWRITE_SQL = (
    "UPDATE memories "
    "SET details = (details::jsonb || jsonb_build_object('observed_at', CAST(:value AS text)))::json "
    "WHERE id = :id AND details->>'observed_at' = :old"
)

_MAX_INPUT_CHARS = 40


def _canonical(value: str) -> str | None:
    """The stored form of a legacy value, or None when it does not parse.

    A local copy of ``utils.observed_at.canonical_observed_at`` on purpose: a
    migration must not change behaviour when the application util does.
    """
    text = value.strip()
    if not text or len(text) > _MAX_INPUT_CHARS:
        return None
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return f"{day.year:04d}-{day.month:02d}-{day.day:02d}"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        else:
            parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    return (
        f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"
        f"T{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}Z"
    )


def _normalise_legacy_values() -> None:
    """Step 1: rewrite parseable non-canonical values to the stored form."""
    if op.get_context().as_sql:
        # Offline mode has no rows to read; the column simply stays NULL for
        # any legacy value, as it does for an unparseable one.
        logger.info("#2330: offline mode, legacy details.observed_at values not normalised")
        return
    conn = op.get_bind()
    rows = conn.execute(sa.text(LEGACY_SQL)).all()
    rewritten = 0
    unparseable = 0
    for row in rows:
        canonical = _canonical(row.value)
        if canonical is None:
            unparseable += 1
            continue
        rewritten += conn.execute(
            sa.text(REWRITE_SQL), {"id": row.id, "value": canonical, "old": row.value}
        ).rowcount
    logger.info(
        "#2330: details.observed_at legacy values: %d found, %d normalised, %d left (unparseable)",
        len(rows),
        rewritten,
        unparseable,
    )


def upgrade() -> None:
    _normalise_legacy_values()

    # Bound the wait for the ACCESS EXCLUSIVE lock; a timeout fails the
    # transaction (nothing above is kept) and the operator reruns the upgrade.
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    op.execute(
        "ALTER TABLE memories ADD COLUMN observed_at VARCHAR(32) "
        f"GENERATED ALWAYS AS ({OBSERVED_AT_SQL}) STORED"
    )
    op.create_index(
        "idx_memories_observed_at",
        "memories",
        ["context_id", "observed_at"],
        postgresql_where=sa.text("observed_at IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    op.drop_index("idx_memories_observed_at", table_name="memories")
    op.drop_column("memories", "observed_at")
