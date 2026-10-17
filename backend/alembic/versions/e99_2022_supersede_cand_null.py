"""#2022: an empty ``memories.supersede_candidate`` becomes SQL NULL.

``supersede_candidate`` (e75_1403) is a Postgres ``json`` column. The model
declared it ``JSON`` with SQLAlchemy's default ``none_as_null=False``, so every
Python ``None`` — each ``remember()`` passes one explicitly, and accepting a
suggestion clears the column with one — was stored as the JSON value ``null``
rather than SQL ``NULL``. Nothing in the application tells the two apart (the
resolvers test ``isinstance(value, dict)``), but an operator counting
``WHERE supersede_candidate IS NULL`` saw almost nothing. The model now declares
``JSON(none_as_null=True)``; this migration rewrites the rows already stored.

The match is ``supersede_candidate::text = 'null'``. e75 creates the column as
``json``, where ``supersede_candidate = 'null'::jsonb`` raises (``json`` has no
equality operator) and only ``json_typeof`` works, while a deployment whose
column is ``jsonb`` needs ``jsonb_typeof``; the text cast reads ``null`` for the
JSON null of either type (SQLAlchemy writes exactly ``null``), so one predicate
fits both. Only the JSON-null rows are written; a candidate, a dismissal
tombstone or a verdict (all JSON objects) and the rows that are already SQL
NULL are left alone.

Batched, outside the migration transaction. ``memories`` is large and written
on the request path, and ``alembic/env.py`` runs an upgrade in one
transaction, so one ``UPDATE`` — or batches inside that transaction — would
hold every touched row's lock until the commit. The rewrite therefore runs in
``op.get_context().autocommit_block()`` (the a97 / b02 pattern), where each
batch statement commits on its own: a keyset walk over the primary key,
BATCH_SIZE ids at a time. Each step reads the next ids from the PK index, takes
the lowest and highest of them, and locks only the JSON-null rows inside that
id window — the bounded range keeps the planner on the PK index instead of
rescanning the rewritten prefix of the table on every step. The rows are locked
in id order (``ORDER BY id FOR UPDATE``, the #1985 house rule for multi-row
UPDATEs) so the walk cannot deadlock with a concurrent bulk write, and with
``SKIP LOCKED``: a row a live request holds is skipped, not waited on, so the
walk never blocks a write or the deploy. A skipped row stays JSON null (see
the deploy window below). The UPDATE repeats the JSON-null predicate, so a
candidate committed after the row was picked is never overwritten. A session
``lock_timeout`` still caps the wait for the table-level lock; a timeout fails
the migration, and running it again resumes — the rows already rewritten no
longer match the WHERE.

Deploy window. Until the new code is serving, the old code keeps writing JSON
null, and a row it writes behind the walk's cursor — or a row skipped because a
request held it — is left as JSON null. That is harmless (both forms read the
same everywhere in the application) and self-contained: the WHERE clause
matches exactly the rows to fix, so rerunning the statement below after the
rollout finishes, or a failed upgrade, is always safe::

    UPDATE memories SET supersede_candidate = NULL
    WHERE supersede_candidate::text = 'null';

``SELECT count(*) FROM memories WHERE supersede_candidate::text = 'null'``
is 0 once it is done. Offline (``alembic upgrade --sql``) there are no results
to drive the walk, so the migration emits that one statement instead.

Downgrade is a no-op: SQL NULL and JSON null mean the same thing to every
version of the code (the older model reads both as ``None``), so there is
nothing to restore, and turning rows back into JSON null would only rebuild
the operator trap.

Revision ID: e99_2022_supersede_cand_null
Revises: e98_2007_mae_list_changes_since

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import logging

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "e99_2022_supersede_cand_null"
down_revision: str | None = "e98_2007_mae_list_changes_since"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Ids walked per statement (each commits on its own).
BATCH_SIZE = 5000

# How long a batch may wait for the table-level lock before the migration
# fails fast (to be rerun — it resumes) instead of stalling writes. Row locks
# are never waited on (SKIP LOCKED).
LOCK_TIMEOUT = "5s"

# The whole rewrite in one statement: the offline (``--sql``) form, and the
# manual re-run the module docstring gives.
REWRITE_ALL_SQL = (
    "UPDATE memories SET supersede_candidate = NULL WHERE supersede_candidate::text = 'null'"
)

# One keyset step: the next BATCH_SIZE ids (after the previous step's last id,
# ``{after}``), and the JSON-null rows among them set to SQL NULL.
#
# - ``bounds``: the step's lowest and highest id, so ``locked`` reads an id
#   range off the PK index instead of scanning from the start of it.
# - ``locked``: the JSON-null rows in that range, locked in id order (#1985)
#   and skipping rows a live request holds.
# - ``rewritten``: the UPDATE repeats the predicate, so a row whose candidate
#   was committed concurrently is never cleared.
#
# Returns the step's last id (NULL once the walk is past the end) and how many
# rows it rewrote.
_BATCH_SQL = (
    "WITH batch AS ("
    "  SELECT id FROM memories {after}"
    "  ORDER BY id LIMIT :batch_size"
    "), bounds AS ("
    "  SELECT (SELECT id FROM batch ORDER BY id LIMIT 1) AS lo,"
    "         (SELECT id FROM batch ORDER BY id DESC LIMIT 1) AS hi"
    "), locked AS ("
    "  SELECT m.id FROM memories AS m, bounds"
    "  WHERE m.id >= bounds.lo AND m.id <= bounds.hi"
    "  AND m.supersede_candidate::text = 'null'"
    "  ORDER BY m.id FOR UPDATE OF m SKIP LOCKED"
    "), rewritten AS ("
    "  UPDATE memories AS m SET supersede_candidate = NULL"
    "  FROM locked WHERE m.id = locked.id"
    "  AND m.supersede_candidate::text = 'null'"
    "  RETURNING 1"
    ") "
    "SELECT (SELECT hi FROM bounds) AS last_id, "
    "(SELECT count(*) FROM rewritten) AS rewritten"
)
FIRST_BATCH_SQL = _BATCH_SQL.format(after="")
NEXT_BATCH_SQL = _BATCH_SQL.format(after="WHERE id > CAST(:after AS uuid)")


def upgrade() -> None:
    """Rewrite JSON-null ``supersede_candidate`` values to SQL NULL in batches."""
    if context.is_offline_mode():
        op.execute(sa.text(REWRITE_ALL_SQL))
        return

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(sa.text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        try:
            after: str | None = None
            total = 0
            while True:
                if after is None:
                    step = conn.execute(sa.text(FIRST_BATCH_SQL), {"batch_size": BATCH_SIZE})
                else:
                    step = conn.execute(
                        sa.text(NEXT_BATCH_SQL), {"after": after, "batch_size": BATCH_SIZE}
                    )
                row = step.one()
                if row.last_id is None:
                    break
                total += row.rewritten
                after = str(row.last_id)
        finally:
            conn.execute(sa.text("RESET lock_timeout"))
    logger.info("#2022: set supersede_candidate to SQL NULL on %d JSON-null memories", total)


def downgrade() -> None:
    """No-op: SQL NULL and JSON null read the same (see the module docstring)."""
