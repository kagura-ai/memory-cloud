"""#2026: JSON-null values in the small nullable JSON tables become SQL NULL.

SQLAlchemy's ``JSON`` / ``JSONB`` default to ``none_as_null=False``, so every
Python ``None`` written to a nullable JSON column was stored as the JSON value
``null`` rather than SQL ``NULL``. The application reads both as ``None``; SQL
does not (``WHERE col IS NULL`` misses the JSON-null rows, and ``'null' || {}``
is an array). The models now declare every nullable JSON column with
``models/json_types.py`` (``none_as_null=True``), so new writes store SQL NULL,
and this migration rewrites the rows already stored in the small tables:

- ``sleep_reports``: the six ``*_result`` columns; ``sleep_actions.details``;
- ``measurements.details``; ``memory_analyses.quality``;
  ``bm25_idf_drift_log.top_divergent_terms``;
- ``erasure_requests.deleted_data_summary``;
- ``workspace_connectors``: ``channel_ids``, ``pii_guardrail_config``,
  ``runtime_config``.

``memories`` is not rewritten. ``details`` is in a partial-index predicate and
``context`` in an index expression, so nearly every rewritten row would be a
non-HOT update into all of the table's indexes; and since ``remember()``
stored JSON null in both whenever they were omitted, that is close to the
whole table (measured: about 10 KB of WAL per row, about 5x the table's size).
``supersede_candidate`` was rewritten by e99 (#2022). docs/deployment.md has
an optional, separately run procedure for operators who want ``memories``
normalized; nothing requires it.

Left as they are, with historical JSON null: ``memories.details`` /
``context`` (and any e99 deploy-window leftovers in ``supersede_candidate``),
the large or append-only logs (``audit_logs.user_metadata``,
``llm_call_log.call_metadata``, ``memory_access_events.event_metadata``,
``secret_access_log.req_meta``, ``resource_events.payload``,
``neural_memory_edges.metadata``; an append-only trigger blocks UPDATE on two
of them), and the two exception columns that keep storing JSON null for an
explicit ``None`` (``resource_events.event_metadata``,
``indexer_state.metrics``). A SQL count or merge on them matches both forms
(docs/deployment.md gives the guarded query).

The match is ``col::text = 'null'`` (e99): it reads the JSON null of both
``json`` and ``jsonb`` (``json`` has no equality operator). SQLAlchemy writes
exactly ``null``; a ``json`` value with whitespace around the null (only
hand-written SQL produces one) is not matched and stays as it is. The cast
runs the type's output function, which detoasts the whole value, so it is
guarded by ``pg_column_size(col) < 16``: the stored size, read without
detoasting (a JSON null takes 5 bytes as ``json``, 9 as ``jsonb``), so a large
value is never fetched from TOAST or decompressed by the scans.

Per table, in ``op.get_context().autocommit_block()`` so each statement commits
on its own (``alembic/env.py`` runs an upgrade in one transaction, which would
hold every row lock until the end):

1. **Collect** the ids of the rows with a JSON null in any listed column into a
   session temp table, in one lock-free scan. The log gives the count.
2. **Rewrite** them in id order, BATCH_SIZE per statement: each batch locks its
   rows in id order (#1985) with ``FOR NO KEY UPDATE`` (the lock the UPDATE
   takes anyway; it does not conflict with the ``FOR KEY SHARE`` a foreign-key
   check on a child row takes) and ``SKIP LOCKED``, so the walk never waits on
   a row a request holds (a skipped row stays JSON null, which reads the same,
   and the rerun below fixes it), and the UPDATE repeats the predicate, so a
   value committed after the collection is never overwritten. Each listed
   column is set to NULL only where it is JSON null; ``updated_at`` is not
   touched. Progress is logged every PROGRESS_EVERY batches.

``lock_timeout`` caps the wait for a table-level lock; a timeout fails the
migration, and running it again resumes (the rewritten rows no longer match).
``statement_timeout`` is 0 for the session: the collecting scan reads the
whole table. The per-table log line (written even when the table's walk
fails) gives the values rewritten per column and the collected rows not
rewritten (skipped because a request held them, or changed or deleted since
the scan). The temp table and the ``SET`` s need a session-pinned connection
(a direct connection or the Cloud SQL proxy; not a transaction-pooling
pgbouncer).

Deploy window. Until the old image is stopped, it keeps writing JSON null
(its scheduler runs Sleep, which writes ``sleep_reports`` / ``sleep_actions``),
and a row it writes after the collection (or a row skipped because a request
held it) keeps it. That is harmless — every reader treats both forms the
same — and the WHERE clause matches exactly the rows to fix, so the per-table
statement (``rewrite_all_sql``) is always safe to rerun once the old image is
stopped; docs/deployment.md lists them. Offline (``alembic upgrade --sql``)
there are no results to drive the walk, so the migration emits those
statements instead, all in the script's one transaction.

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

# A progress line every this many batches.
PROGRESS_EVERY = 100

# (table, primary-key type, the nullable JSON columns rewritten). Every table
# has an ``id`` primary key. Fixed identifiers only: nothing here comes from
# input.
TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
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

# Session temp table holding the ids of one table's rows to rewrite
# (``pg_temp.`` so a permanent table of the same name is never touched).
_IDS = "pg_temp.f15_json_null_ids"


# Larger than a stored JSON null (5 bytes as json, 9 as jsonb), far smaller
# than any value worth detoasting.
_MAX_NULL_SIZE = 16


def _is_null(column: str, alias: str | None = "t") -> str:
    """The column holds the JSON value null (size first: no detoast)."""
    ref = f"{alias}.{column}" if alias else column
    return f"(pg_column_size({ref}) < {_MAX_NULL_SIZE} AND {ref}::text = 'null')"


def _any_null(columns: tuple[str, ...], alias: str | None = "t") -> str:
    return "(" + " OR ".join(_is_null(c, alias) for c in columns) + ")"


def rewrite_all_sql(table: str, columns: tuple[str, ...]) -> str:
    """One table's whole rewrite in one statement: the offline form and the
    documented rerun. Each column is set to NULL only where it is JSON null,
    so a row is rewritten once whichever of its columns match."""
    sets = ", ".join(f"{c} = CASE WHEN {_is_null(c, None)} THEN NULL ELSE {c} END" for c in columns)
    return f"UPDATE {table} SET {sets} WHERE {_any_null(columns, None)}"


def collect_sql(table: str, columns: tuple[str, ...]) -> str:
    return f"CREATE TEMP TABLE {_IDS} AS SELECT t.id FROM {table} AS t WHERE {_any_null(columns)}"


def batch_sql(table: str, pk_type: str, columns: tuple[str, ...], *, first: bool) -> str:
    """One batch: the next BATCH_SIZE collected ids after the previous batch's
    last one, their JSON-null rows locked in id order (skipping rows a request
    holds) and rewritten.

    Returns the batch's last id (NULL once done), the rows rewritten, how many
    of them had each column JSON null (``c0``, ``c1``, ...), and the collected
    rows not rewritten (held by a request, or changed or deleted since the
    scan).
    """
    after = "" if first else f"WHERE id > CAST(:after AS {pk_type})"
    flags = ", ".join(f"{_is_null(c)} AS c{i}" for i, c in enumerate(columns))
    sets = ", ".join(f"{c} = CASE WHEN {_is_null(c)} THEN NULL ELSE t.{c} END" for c in columns)
    returned = ", ".join(f"locked.c{i}" for i in range(len(columns)))
    counts = ", ".join(
        f"(SELECT count(*) FILTER (WHERE c{i}) FROM rewritten) AS c{i}" for i in range(len(columns))
    )
    return (
        "WITH ids AS ("
        f"  SELECT id FROM {_IDS} {after} ORDER BY id LIMIT :batch_size"
        "), locked AS ("
        f"  SELECT t.id, {flags} FROM {table} AS t JOIN ids ON ids.id = t.id"
        f"  WHERE {_any_null(columns)}"
        "  ORDER BY t.id FOR NO KEY UPDATE OF t SKIP LOCKED"
        "), rewritten AS ("
        f"  UPDATE {table} AS t SET {sets}"
        f"  FROM locked WHERE t.id = locked.id AND {_any_null(columns)}"
        f"  RETURNING t.id, {returned}"
        ") "
        "SELECT (SELECT id FROM ids ORDER BY id DESC LIMIT 1) AS last_id, "
        "(SELECT count(*) FROM rewritten) AS rewritten, "
        "(SELECT count(*) FROM ids) - (SELECT count(*) FROM rewritten) AS skipped, "
        f"{counts}"
    )


def _normalize(
    conn: sa.engine.Connection, table: str, pk_type: str, columns: tuple[str, ...]
) -> None:
    """Collect one table's JSON-null rows, then rewrite them batch by batch."""
    first_sql = batch_sql(table, pk_type, columns, first=True)
    next_sql = batch_sql(table, pk_type, columns, first=False)
    rewritten = skipped = batches = 0
    per_column = [0] * len(columns)
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_IDS}"))
    conn.execute(sa.text(collect_sql(table, columns)))
    conn.execute(sa.text(f"ALTER TABLE {_IDS} ADD PRIMARY KEY (id)"))
    collected = conn.execute(sa.text(f"SELECT count(*) FROM {_IDS}")).scalar_one()
    logger.info("#2026: %s: %d rows with a JSON null to rewrite", table, collected)
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
            batches += 1
            rewritten += row.rewritten
            skipped += row.skipped
            for i in range(len(columns)):
                per_column[i] += row._mapping[f"c{i}"]
            after = row.last_id
            if batches % PROGRESS_EVERY == 0:
                logger.info(
                    "#2026: %s: %d of %d rows rewritten so far", table, rewritten, collected
                )
    finally:
        # Also on a failure: the batches before it committed.
        detail = ", ".join(f"{c}={n}" for c, n in zip(columns, per_column, strict=True))
        logger.info(
            "#2026: %s: set JSON null to SQL NULL on %d rows (%s); "
            "%d rows not rewritten (held by a request, or changed since the scan)",
            table,
            rewritten,
            detail,
            skipped,
        )
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {_IDS}"))


def upgrade() -> None:
    """Rewrite JSON-null values to SQL NULL, table by table (see the docstring)."""
    if op.get_context().as_sql:
        for table, _pk_type, columns in TABLES:
            op.execute(sa.text(rewrite_all_sql(table, columns)))
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
