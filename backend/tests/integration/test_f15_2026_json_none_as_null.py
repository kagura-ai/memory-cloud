"""Migration test for f15_2026_json_none_as_null (#2026).

Seeded at f14, then upgraded:

1. every JSON-null value in the walked columns becomes SQL NULL: the sleep
   tables (``sleep_actions`` spread over several batches of its bigint key),
   measurements, memory analyses, the BM25 drift log, erasure requests and
   workspace connectors. A row whose JSON null is in one column only has just
   that column rewritten;
2. values that only look like a null — the JSON string ``"null"``, ``[null]``,
   ``{"a": null}`` — objects, a large TOASTed object and SQL NULL are
   byte-identical afterwards (the text of the whole record, ``updated_at``
   included);
3. ``memories`` is not walked: its JSON nulls stay, and its rows are
   byte-identical. The switch-only tables (audit_logs, llm_call_log,
   memory_access_events, secret_access_log, resource_events,
   neural_memory_edges) still hold their JSON null;
4. a failure mid-walk leaves the batches before it committed (and the
   per-table log line still reports them), and running the upgrade again
   resumes; a row a request holds is skipped (the walk does not wait), stays
   JSON null, and the documented per-table rerun statement fixes it;
5. downgrade is a no-op, and a re-upgrade finds nothing to rewrite.

Not executed in the local unit run — needs the DB container (the cleanup of
the append-only tables uses ``session_replication_role``, so the test user
must be a superuser, as the CI and local containers are).
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import secrets
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
if str(_VERSIONS) not in sys.path:
    sys.path.insert(0, str(_VERSIONS))
f15 = importlib.import_module("f15_2026_json_none_as_null")

F15_REVISION = "f15_2026_json_none_as_null"
PRIOR_HEAD = "f14_1963_memories_ws_ctx_fk"

# More JSON-null sleep_actions than BATCH_SIZE x 2, so the walk takes three
# batches and a failure in the last leaves two committed.
_BULK_ROWS = 2 * f15.BATCH_SIZE + 300

_OBJ = '{"k": "v"}'
# Stored values that must never match: they only look like a null, or are
# too large to be read at all (out of line in TOAST).
_UNTOUCHED = {
    "obj": _OBJ,
    "str": '"null"',
    "arr": "[null]",
    "objnull": '{"a": null}',
    "big": json.dumps({"k": secrets.token_hex(20000)}),
}
_KINDS = ("null", "sql", "mixed", *_UNTOUCHED)

_MEMORY_INSERT = (
    "INSERT INTO memories "
    "(id, user_id, workspace_id, summary, content, type, embedding_status, importance, "
    " confidence, scope, long_term, access_count, client, source, created_at, updated_at, "
    " details, context, supersede_candidate) "
    "SELECT COALESCE(CAST(:id AS uuid), gen_random_uuid()), :user, gen_random_uuid(), "
    "'seed', 'body', 'note', 'success', 0.5, 1.0, 'working', false, 0, 'test', "
    "'mcp_remember', '2026-01-01T00:00:00', '2026-01-02T00:00:00', "
    "CAST(:details AS json), CAST(:context AS json), CAST(:cand AS json) "
    "FROM generate_series(1, :n)"
)


class _Captured(logging.Filter):
    """Records the migration's log lines (a filter survives env.py's fileConfig)."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        self.messages.append(record.getMessage())
        return True


def _memories(
    conn: Connection,
    user: str,
    details: str | None,
    context: str | None,
    cand: str | None,
    *,
    n: int = 1,
    memory_id: uuid.UUID | None = None,
) -> None:
    conn.execute(
        text(_MEMORY_INSERT),
        {
            "id": memory_id,
            "user": user,
            "details": details,
            "context": context,
            "cand": cand,
            "n": n,
        },
    )


def _memory_rows(conn: Connection, user: str) -> dict[uuid.UUID, str]:
    """Each seeded memory as the text of the whole record (every column)."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            text("SELECT m.id, m::text FROM memories AS m WHERE m.user_id = :u"), {"u": user}
        )
    }


def _json_null_memories(conn: Connection, prefix: str) -> int:
    return conn.execute(
        text(
            "SELECT count(*) FROM memories WHERE user_id LIKE :p AND ("
            "details::text = 'null' OR context::text = 'null' "
            "OR supersede_candidate::text = 'null')"
        ),
        {"p": f"{prefix}%"},
    ).scalar_one()


def _version(conn: Connection) -> str:
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


class _Other:
    """Rows in the other tables: per table, one id per kind (``null`` = JSON null
    in every column, ``sql`` = SQL NULL, ``mixed`` = JSON null in the first
    column and an object in the rest, and the ``_UNTOUCHED`` values)."""

    def __init__(self, conn: Connection, prefix: str) -> None:
        self.prefix = prefix
        self.owner = f"{prefix}owner"
        self.ws = uuid.uuid4()
        self.ctx = uuid.uuid4()
        self.ids: dict[str, dict[str, object]] = {}
        conn.execute(
            text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :n, :o)"),
            {"id": self.ws, "n": f"f15-{self.ws.hex[:8]}", "o": self.owner},
        )
        conn.execute(
            text(
                "INSERT INTO contexts (id, workspace_id, name, created_by) "
                "VALUES (:id, :ws, :n, :o)"
            ),
            {"id": self.ctx, "ws": self.ws, "n": f"f15-{self.ctx.hex[:8]}", "o": self.owner},
        )
        self.report = self._row(conn, "sleep_reports", "sql")  # the actions' parent

    # (table, insert columns, insert values) per walked or switch-only table.
    def _spec(self, table: str) -> tuple[str, str]:
        o, ws, ctx = ":owner", ":ws", ":ctx"
        return {
            "sleep_reports": ("user_id, context_id", f"{o}, {ctx}"),
            "sleep_actions": ("report_id, phase, action_type", ":report, 'dedup', 'merge'"),
            "measurements": ("context_id, metric, measured_at, value", f"{ctx}, 'w', now(), 1"),
            "memory_analyses": (
                "workspace_id, context_id, triggered_by, model_snapshot, embedding_model, "
                "params, input_count, status",
                f"{ws}, {ctx}, {o}, '{{}}', 'e', '{{}}', 0, 'succeeded'",
            ),
            "bm25_idf_drift_log": (
                "context_id, psi_status, m_memory_points, r_resource_points, num_terms",
                f"{ctx}, 'insufficient_data', 0, 0, 0",
            ),
            "erasure_requests": (
                "user_id, user_email_hash, initiated_by, is_self_service, reason_code",
                ":hash, repeat('0', 64), 'admin', false, 'user_request_via_support'",
            ),
            "workspace_connectors": (
                "resource_pk, workspace_id, connector_type",
                f":resource, {ws}, 'slack'",
            ),
            "audit_logs": (
                "user_email, user_id, action, resource",
                f"'f15@test.example', {o}, 'test', 'test'",
            ),
            "llm_call_log": (
                "occurred_at, caller, call_type, provider, model",
                "now(), 'sleep', 'completion', 'p', 'm'",
            ),
            "memory_access_events": (
                "workspace_id, user_id, principal_type, surface, operation, outcome",
                f"{ws}, {o}, 'session', 'rest', 'recall', 'success'",
            ),
            "secret_access_log": (
                "workspace_id, actor_user_id, action, result, prev_hash, entry_hash",
                f"{ws}, {o}, 'get', 'ok', :hash, :hash",
            ),
            "resource_events": (
                "resource_id, resource_pk, op, doc_id",
                ":slug, :resource, 'delete', :hash",
            ),
            "neural_memory_edges": (
                "user_id, src_id, dst_id, workspace_id, context_id, edge_type, weight, confidence",
                f"{o}, gen_random_uuid(), gen_random_uuid(), {ws}, {ctx}, 'related_to', 1, 1",
            ),
        }[table]

    def _row(self, conn: Connection, table: str, kind: str) -> object:
        columns = self.columns(table)
        if kind == "mixed":
            per_column = ["null"] + [_OBJ] * (len(columns) - 1)
        else:
            value = {"null": "null", "sql": None, **_UNTOUCHED}[kind]
            per_column = [value] * len(columns)
        names, values = self._spec(table)
        params: dict[str, object] = {
            "owner": self.owner,
            "ws": self.ws,
            "ctx": self.ctx,
            "report": getattr(self, "report", None),
            "hash": uuid.uuid4().hex + uuid.uuid4().hex,
            **{f"v{i}": v for i, v in enumerate(per_column)},
        }
        if table in ("workspace_connectors", "resource_events"):
            resource = uuid.uuid4()
            slug = f"{self.prefix}{resource.hex[:8]}"
            conn.execute(
                text("INSERT INTO resources (id, workspace_id, resource_id) VALUES (:id, :ws, :s)"),
                {"id": resource, "ws": self.ws, "s": slug},
            )
            params.update(resource=resource, slug=slug)
        casts = ", ".join(f"CAST(:v{i} AS {self.json_type(table)})" for i in range(len(columns)))
        row_id = conn.execute(
            text(
                f"INSERT INTO {table} ({names}, {', '.join(columns)}) "
                f"VALUES ({values}, {casts}) RETURNING id"
            ),
            params,
        ).scalar_one()
        self.ids.setdefault(table, {})[kind] = row_id
        return row_id

    @staticmethod
    def columns(table: str) -> tuple[str, ...]:
        walked = {t: cols for t, _pk, cols in f15.TABLES}
        return (
            walked.get(table)
            or {
                "audit_logs": ("user_metadata",),
                "llm_call_log": ("call_metadata",),
                "memory_access_events": ("event_metadata",),
                "secret_access_log": ("req_meta",),
                "resource_events": ("payload",),
                "neural_memory_edges": ("metadata",),
            }[table]
        )

    @staticmethod
    def json_type(table: str) -> str:
        json_tables = {"sleep_reports", "sleep_actions", "audit_logs", "neural_memory_edges"}
        return "json" if table in json_tables else "jsonb"

    def seed(self, conn: Connection, tables: tuple[str, ...], kinds: tuple[str, ...]) -> None:
        for table in tables:
            for kind in kinds:
                if (table, kind) != ("sleep_reports", "sql"):  # the parent row is that one
                    self._row(conn, table, kind)
        self.ids["sleep_reports"]["sql"] = self.report

    def values(self, conn: Connection, table: str, kind: str) -> tuple[str | None, ...]:
        cols = ", ".join(f'"{c}"::text' for c in self.columns(table))
        return tuple(
            conn.execute(
                text(f"SELECT {cols} FROM {table} WHERE id = :id"), {"id": self.ids[table][kind]}
            ).one()
        )

    def record(self, conn: Connection, table: str, kind: str) -> str:
        return conn.execute(
            text(f"SELECT t::text FROM {table} AS t WHERE id = :id"),
            {"id": self.ids[table][kind]},
        ).scalar_one()


_WALKED = tuple(t for t, _pk, _cols in f15.TABLES)
_SWITCH_ONLY = (
    "audit_logs",
    "llm_call_log",
    "memory_access_events",
    "secret_access_log",
    "resource_events",
    "neural_memory_edges",
)


def _cleanup(prefix: str, other: _Other | None) -> None:
    engine = _sync_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS f15_test_fail ON sleep_actions"))
            conn.execute(text("DROP FUNCTION IF EXISTS f15_test_fail()"))
            conn.execute(text("DELETE FROM memories WHERE user_id LIKE :p"), {"p": f"{prefix}%"})
            if other is not None:
                # The append-only tables' triggers block DELETE: off for this
                # transaction only.
                conn.execute(text("SET LOCAL session_replication_role = replica"))
                for table in ("memory_access_events", "secret_access_log"):
                    for row_id in other.ids.get(table, {}).values():
                        conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
        if other is not None:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM sleep_actions WHERE report_id = :r"), {"r": other.report}
                )
                for table in ("llm_call_log", "erasure_requests", "audit_logs", "sleep_reports"):
                    for row_id in other.ids.get(table, {}).values():
                        conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
                conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": other.ws})
    finally:
        engine.dispose()


def _sleep_actions_json_null(conn: Connection, report: object) -> int:
    return conn.execute(
        text("SELECT count(*) FROM sleep_actions WHERE report_id = :r AND details::text = 'null'"),
        {"r": report},
    ).scalar_one()


def _lines(captured: _Captured, table: str) -> list[str]:
    return [m for m in captured.messages if m.startswith(f"#2026: {table}:")]


def test_f15_rewrites_json_null_only_and_resumes() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    prefix = f"f15-{uuid.uuid4().hex[:8]}-"
    mem_null, mem_objects = f"{prefix}memnull", f"{prefix}memobj"
    other: _Other | None = None
    captured = _Captured()
    migration_logger = logging.getLogger("alembic.runtime.migration")
    migration_logger.addFilter(captured)
    holder = None
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            _memories(conn, mem_null, "null", "null", "null", n=3)
            _memories(conn, mem_objects, _OBJ, '{"c": 1}', None, n=2)
            other = _Other(conn, prefix)
            other.seed(conn, _WALKED, _KINDS)
            other.seed(conn, _SWITCH_ONLY, ("null",))
            # The bulk, then the held row, then the failing one: bigserial ids
            # in that order, so the last two land in the last batch.
            conn.execute(
                text(
                    "INSERT INTO sleep_actions (report_id, phase, action_type, details) "
                    "SELECT :r, 'dedup', 'merge', 'null'::json FROM generate_series(1, :n)"
                ),
                {"r": other.report, "n": _BULK_ROWS},
            )
            held_id, last_id = (
                conn.execute(
                    text(
                        "INSERT INTO sleep_actions (report_id, phase, action_type, details) "
                        "VALUES (:r, 'dedup', 'merge', 'null'::json) RETURNING id"
                    ),
                    {"r": other.report},
                ).scalar_one()
                for _ in range(2)
            )
            memories_before = {u: _memory_rows(conn, u) for u in (mem_null, mem_objects)}
            untouched = ("sql", *_UNTOUCHED)
            other_before = {(t, k): other.record(conn, t, k) for t in _WALKED for k in untouched}
            mixed_before = {t: other.values(conn, t, "mixed") for t in _WALKED}
            # The "null" and "mixed" rows (one column: both JSON null), the
            # bulk, the held row and the failing one.
            collected = 2 + _BULK_ROWS + 2
            assert _sleep_actions_json_null(conn, other.report) == collected
            # A failure in the last sleep_actions batch.
            conn.execute(
                text(
                    "CREATE FUNCTION f15_test_fail() RETURNS trigger AS $$ BEGIN "
                    f"IF NEW.id = {int(last_id)} THEN "
                    "RAISE EXCEPTION 'f15 forced failure'; END IF; RETURN NEW; END $$ "
                    "LANGUAGE plpgsql"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER f15_test_fail BEFORE UPDATE ON sleep_actions "
                    "FOR EACH ROW EXECUTE FUNCTION f15_test_fail()"
                )
            )

        with pytest.raises(DBAPIError, match="f15 forced failure"):
            _upgrade_to(F15_REVISION)

        with engine.begin() as conn:
            assert _version(conn) == PRIOR_HEAD
            # sleep_reports (walked first) is done; two sleep_actions batches
            # committed, the last one, with the failing row, rolled back.
            assert other.values(conn, "sleep_reports", "null") == (None,) * 6
            assert _sleep_actions_json_null(conn, other.report) == collected - 2000
            assert other.values(conn, "measurements", "null") == ("null",)
            conn.execute(text("DROP TRIGGER f15_test_fail ON sleep_actions"))
        # The per-table line reports the committed batches of the failed walk.
        assert _lines(captured, "sleep_actions") == [
            f"#2026: sleep_actions: {collected} rows with a JSON null to rewrite",
            "#2026: sleep_actions: set JSON null to SQL NULL on 2000 rows (details=2000); "
            "0 rows not rewritten (held by a request, or changed since the scan)",
        ]

        # A request holds one JSON-null row for the whole rerun.
        holder = engine.connect()
        holder.execute(
            text("SELECT id FROM sleep_actions WHERE id = :id FOR UPDATE"), {"id": held_id}
        ).all()
        captured.messages.clear()
        _upgrade_to(F15_REVISION)

        with engine.begin() as conn:
            assert _version(conn) == F15_REVISION
            # Only the held row is left.
            assert _sleep_actions_json_null(conn, other.report) == 1
            for table in _WALKED:
                columns = other.columns(table)
                assert other.values(conn, table, "null") == (None,) * len(columns), table
                # Only the JSON-null column of a mixed row was rewritten.
                assert mixed_before[table] == ("null",) + (_OBJ,) * (len(columns) - 1), table
                mixed_after = other.values(conn, table, "mixed")
                assert mixed_after == (None,) + (_OBJ,) * (len(columns) - 1), table
                for kind in untouched:
                    assert other.record(conn, table, kind) == other_before[(table, kind)], (
                        table,
                        kind,
                    )
            for table in _SWITCH_ONLY:
                assert other.values(conn, table, "null") == ("null",), table
            # memories is not walked.
            for user, rows in memories_before.items():
                assert _memory_rows(conn, user) == rows
            assert _json_null_memories(conn, prefix) == 3
        assert _lines(captured, "sleep_actions") == [
            f"#2026: sleep_actions: {collected - 2000} rows with a JSON null to rewrite",
            f"#2026: sleep_actions: set JSON null to SQL NULL on {collected - 2001} rows "
            f"(details={collected - 2001}); "
            "1 rows not rewritten (held by a request, or changed since the scan)",
        ]
        assert _lines(captured, "memories") == []
        assert (
            "#2026: workspace_connectors: set JSON null to SQL NULL on 2 rows "
            "(channel_ids=2, pii_guardrail_config=1, runtime_config=1); "
            "0 rows not rewritten (held by a request, or changed since the scan)"
        ) in _lines(captured, "workspace_connectors")

        holder.rollback()
        holder.close()
        holder = None
        # The documented per-table rerun fixes what the walk skipped.
        with engine.begin() as conn:
            for table, _pk_type, columns in f15.TABLES:
                conn.execute(text(f15.rewrite_all_sql(table, columns)))
            assert _sleep_actions_json_null(conn, other.report) == 0
            for table in _WALKED:
                for kind in untouched:
                    assert other.record(conn, table, kind) == other_before[(table, kind)]

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert other.values(conn, "erasure_requests", "null") == (None,)
        captured.messages.clear()
        _upgrade_to(F15_REVISION)
        assert _lines(captured, "sleep_actions") == [
            "#2026: sleep_actions: 0 rows with a JSON null to rewrite",
            "#2026: sleep_actions: set JSON null to SQL NULL on 0 rows (details=0); "
            "0 rows not rewritten (held by a request, or changed since the scan)",
        ]
    finally:
        migration_logger.removeFilter(captured)
        try:
            if holder is not None:
                holder.close()
            _upgrade_to("head")
        finally:
            try:
                _cleanup(prefix, other)
            finally:
                engine.dispose()


def test_untouched_seeds_are_what_they_claim() -> None:
    """The seed values the test above relies on: none is a JSON null, and the
    large one is well past the TOAST threshold."""
    assert all(json.loads(v) is not None for v in _UNTOUCHED.values())
    assert json.loads(_UNTOUCHED["str"]) == "null"
    assert len(_UNTOUCHED["big"]) > 40_000


_DOCS = Path(__file__).resolve().parents[3] / "docs" / "deployment.md"


def _memories_procedure() -> list[str]:
    """The optional memories procedure from docs/deployment.md, statement by
    statement (each runs on its own, as psql in autocommit runs them)."""
    docs = _DOCS.read_text()
    section = docs[docs.index("**Optional: normalizing `memories`.**") :]
    block = section[section.index("```sql\nSET lock_timeout") + len("```sql\n") :]
    block = block[: block.index("```")]
    statements = re.findall(
        r"^SET [^;]+;|^DO \$\$.*?^END \$\$;|^VACUUM [^;]+;", block, flags=re.M | re.S
    )
    assert len(statements) == 4, statements
    return statements


def test_the_documented_memories_procedure_rewrites_json_null_only() -> None:
    engine = _sync_engine()
    prefix = f"f15p-{uuid.uuid4().hex[:8]}-"
    nulls, mixed, kept = f"{prefix}null", f"{prefix}mixed", f"{prefix}kept"
    try:
        _upgrade_to("head")
        with engine.begin() as conn:
            # More rows than one 1000-row batch.
            _memories(conn, nulls, "null", "null", "null", n=f15.BATCH_SIZE + 200)
            _memories(conn, mixed, '{"a": 1}', "null", None, n=3)
            for value in _UNTOUCHED.values():
                _memories(conn, kept, value, value, value)
            _memories(conn, kept, None, None, None)
            kept_before = _memory_rows(conn, kept)
        # A raw cursor without parameters: the DO block's RAISE NOTICE uses ``%``.
        raw = engine.raw_connection()
        try:
            driver = raw.driver_connection
            driver.autocommit = True
            with driver.cursor() as cursor:
                for statement in _memories_procedure():
                    cursor.execute(statement)
        finally:
            raw.close()
        with engine.begin() as conn:
            assert _json_null_memories(conn, prefix) == 0
            rows = conn.execute(
                text("SELECT details::text, context::text FROM memories WHERE user_id = :u"),
                {"u": mixed},
            ).all()
            assert {tuple(r) for r in rows} == {('{"a": 1}', None)}
            assert _memory_rows(conn, kept) == kept_before
    finally:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM memories WHERE user_id LIKE :p"), {"p": f"{prefix}%"}
                )
        finally:
            engine.dispose()
