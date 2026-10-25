"""#2026: JSON-null values in the nullable JSON columns become SQL NULL.

SQLAlchemy's ``JSON`` / ``JSONB`` default to ``none_as_null=False``, so every
Python ``None`` written to a nullable JSON column was stored as the JSON value
``null`` rather than SQL ``NULL``. The application reads both as ``None``; SQL
does not (``WHERE col IS NULL`` misses the JSON-null rows, and ``'null' || {}``
is an array). #2022 fixed ``memories.supersede_candidate`` (e99); the models
now declare every nullable JSON column with ``models/json_types.py``
(``none_as_null=True``), and this migration rewrites the rows already stored
in these tables:

- ``memories``: ``details``, ``context``, ``supersede_candidate`` (one pass;
  the last one clears what old pods wrote during the e99 deploy);
- ``sleep_reports``: the six ``*_result`` columns; ``sleep_actions.details``;
- ``measurements.details``; ``memory_analyses.quality``;
  ``bm25_idf_drift_log.top_divergent_terms``;
- ``erasure_requests.deleted_data_summary``;
- ``workspace_connectors``: ``channel_ids``, ``pii_guardrail_config``,
  ``runtime_config``.

The large or append-only logs (``audit_logs.user_metadata``,
``llm_call_log.call_metadata``, ``memory_access_events.event_metadata``,
``secret_access_log.req_meta``, ``resource_events.payload``,
``neural_memory_edges.metadata``) are left as they are: new rows store SQL
NULL, historical rows keep JSON null, and a SQL count or merge on them uses
``col IS NULL OR col::text = 'null'`` (docs/deployment.md). Two of them cannot
be rewritten anyway: an append-only trigger blocks UPDATE on
``memory_access_events`` and ``secret_access_log``.

The match is ``col::text = 'null'`` (e99): it reads the JSON null of both
``json`` and ``jsonb`` (``json`` has no equality operator). The cast runs the
type's output function, which detoasts the whole value, so it is guarded by
``pg_column_size(col) < 16``: the stored size, read without detoasting (a
JSON null takes 5 bytes as ``json``, 9 as ``jsonb``), so a large value is
never fetched from TOAST or decompressed by the scans.

Per table, in ``op.get_context().autocommit_block()`` so each statement commits
on its own (``alembic/env.py`` runs an upgrade in one transaction, which would
hold every row lock until the end):

1. **Collect** the ids of the rows with a JSON null in any listed column into a
   session temp table, in one lock-free scan.
2. **Rewrite** them in id order, BATCH_SIZE per statement: each batch locks its
   rows in id order (#1985) with ``SKIP LOCKED``, so the walk never waits on a
   row a request holds (a skipped row stays JSON null, which reads the same,
   and the rerun below fixes it), and the UPDATE repeats the predicate, so a
   value committed after the collection is never overwritten. Each listed
   column is set to NULL only where it is JSON null; ``updated_at`` is not
   touched.

``lock_timeout`` caps the wait for a table-level lock; a timeout fails the
migration, and running it again resumes (the rewritten rows no longer match).
``statement_timeout`` is 0 for the session: the collecting scan reads the
whole table. The log gives, per table, the values rewritten per column and the
rows skipped.

Cost: the ``memories`` scan reads the whole table once, and each rewritten row
is a non-HOT update (``details`` is in a partial-index predicate and
``context`` in an index expression), so it writes to every index of the table:
expect WAL volume, replica lag and autovacuum work after the deploy.

Deploy window. Until the new code serves all traffic, old pods keep writing
JSON null, and a row they write after the collection (or a row skipped because
a request held it) keeps it. That is harmless — every reader treats both forms
the same — and the WHERE clause matches exactly the rows to fix, so the
per-column statement is always safe to rerun after the rollout::

    UPDATE memories SET details = NULL WHERE details::text = 'null';

(docs/deployment.md lists one per column, with the count query). Offline
(``alembic upgrade --sql``) there are no results to drive the walk, so the
migration emits those statements instead.

Downgrade is a no-op: SQL NULL and JSON null mean the same thing to every
version of the code, so there is nothing to restore (e99).

Revision ID: f15_2026_json_none_as_null
Revises: f14_1963_memories_ws_ctx_fk

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import logging

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f15_2026_json_none_as_null"
down_revision: str | None = "f14_1963_memories_ws_ctx_fk"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Rows rewritten per statement (each commits on its own).
BATCH_SIZE = 1000

# How long a statement may wait for a table-level lock before the migration
# fails fast (to be rerun — it resumes). Row locks are never waited on.
LOCK_TIMEOUT = "5s"

# (table, primary-key type, the nullable JSON columns rewritten). Every table
# has an ``id`` primary key. Fixed identifiers only: nothing here comes from
# input.
TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("memories", "uuid", ("details", "context", "supersede_candidate")),
    (
        "sleep_reports",
        "uuid",
        (
            "edge_discovery_result",
            "dedup_result",
            "merge_retention_result",
            "importance_result",
            "consolidation_result",
            "reindex_result",
        ),
    ),
    ("sleep_actions", "bigint", ("details",)),
    ("measurements", "uuid", ("details",)),
    ("memory_analyses", "uuid", ("quality",)),
    ("bm25_idf_drift_log", "bigint", ("top_divergent_terms",)),
    ("erasure_requests", "uuid", ("deleted_data_summary",)),
    ("workspace_connectors", "uuid", ("channel_ids", "pii_guardrail_config", "runtime_config")),
)

# Session temp table holding the ids of one table's rows to rewrite.
_IDS = "f15_json_null_ids"


# Larger than a stored JSON null (5 bytes as json, 9 as jsonb), far smaller
# than any value worth detoasting.
_MAX_NULL_SIZE = 16


def _is_null(column: str, alias: str = "t") -> str:
    """The column holds the JSON value null (size first: no detoast)."""
    return (
        f"(pg_column_size({alias}.{column}) < {_MAX_NULL_SIZE} AND {alias}.{column}::text = 'null')"
    )


def _any_null(columns: tuple[str, ...], alias: str = "t") -> str:
    return "(" + " OR ".join(_is_null(c, alias) for c in columns) + ")"


def rewrite_all_sql(table: str, column: str) -> str:
    """One column's whole rewrite: the offline form and the documented rerun."""
    return (
        f"UPDATE {table} SET {column} = NULL "
        f"WHERE pg_column_size({column}) < {_MAX_NULL_SIZE} AND {column}::text = 'null'"
    )


def collect_sql(table: str, columns: tuple[str, ...]) -> str:
    return f"CREATE TEMP TABLE {_IDS} AS SELECT t.id FROM {table} AS t WHERE {_any_null(columns)}"


def batch_sql(table: str, pk_type: str, columns: tuple[str, ...], *, first: bool) -> str:
    """One batch: the next BATCH_SIZE collected ids after the previous batch's
    last one, their JSON-null rows locked in id order (skipping rows a request
    holds) and rewritten.

    Returns the batch's last id (NULL once done), the rows rewritten, how many
    of them had each column JSON null (``c0``, ``c1``, ...), and the rows that
    still matched but were skipped.
    """
    after = "" if first else f"WHERE id > CAST(:after AS {pk_type})"
    flags = ", ".join(f"{_is_null(c)} AS c{i}" for i, c in enumerate(columns))
    sets = ", ".join(f"{c} = CASE WHEN {_is_null(c)} THEN NULL ELSE t.{c} END" for c in columns)
    counts = ", ".join(
        f"(SELECT count(*) FROM locked JOIN rewritten USING (id) WHERE locked.c{i}) AS c{i}"
        for i in range(len(columns))
    )
    return (
        "WITH ids AS ("
        f"  SELECT id FROM {_IDS} {after} ORDER BY id LIMIT :batch_size"
        "), matching AS ("
        f"  SELECT t.id FROM {table} AS t JOIN ids ON ids.id = t.id WHERE {_any_null(columns)}"
        "), locked AS ("
        f"  SELECT t.id, {flags} FROM {table} AS t JOIN ids ON ids.id = t.id"
        f"  WHERE {_any_null(columns)}"
        "  ORDER BY t.id FOR UPDATE OF t SKIP LOCKED"
        "), rewritten AS ("
        f"  UPDATE {table} AS t SET {sets}"
        f"  FROM locked WHERE t.id = locked.id AND {_any_null(columns)}"
        "  RETURNING t.id"
        ") "
        "SELECT (SELECT id FROM ids ORDER BY id DESC LIMIT 1) AS last_id, "
        "(SELECT count(*) FROM rewritten) AS rewritten, "
        "(SELECT count(*) FROM matching) - (SELECT count(*) FROM locked) AS skipped, "
        f"{counts}"
    )


def _normalize(conn: sa.engine.Connection, table: str, pk_type: str, columns: tuple) -> None:
    """Collect one table's JSON-null rows, then rewrite them batch by batch."""
    first_sql = batch_sql(table, pk_type, columns, first=True)
    next_sql = batch_sql(table, pk_type, columns, first=False)
    rewritten = skipped = 0
    per_column = [0] * len(columns)
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_IDS}"))
    conn.execute(sa.text(collect_sql(table, columns)))
    conn.execute(sa.text(f"ALTER TABLE {_IDS} ADD PRIMARY KEY (id)"))
    try:
        after = None
        while True:
            if after is None:
                row = conn.execute(sa.text(first_sql), {"batch_size": BATCH_SIZE}).one()
            else:
                row = conn.execute(
                    sa.text(next_sql), {"after": after, "batch_size": BATCH_SIZE}
                ).one()
            if row.last_id is None:
                break
            rewritten += row.rewritten
            skipped += max(row.skipped, 0)
            for i in range(len(columns)):
                per_column[i] += row._mapping[f"c{i}"]
            after = row.last_id
    finally:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {_IDS}"))

    detail = ", ".join(f"{c}={n}" for c, n in zip(columns, per_column, strict=True))
    logger.info(
        "#2026: %s: set JSON null to SQL NULL on %d rows (%s); %d rows skipped (held by a request)",
        table,
        rewritten,
        detail,
        skipped,
    )


def upgrade() -> None:
    """Rewrite JSON-null values to SQL NULL, table by table (see the docstring)."""
    if op.get_context().as_sql:
        for table, _pk_type, columns in TABLES:
            for column in columns:
                op.execute(sa.text(rewrite_all_sql(table, column)))
        return

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(sa.text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        conn.execute(sa.text("SET statement_timeout = 0"))
        try:
            for table, pk_type, columns in TABLES:
                _normalize(conn, table, pk_type, columns)
        finally:
            conn.execute(sa.text("RESET lock_timeout"))
            conn.execute(sa.text("RESET statement_timeout"))


def downgrade() -> None:
    """No-op: SQL NULL and JSON null read the same (see the module docstring)."""
