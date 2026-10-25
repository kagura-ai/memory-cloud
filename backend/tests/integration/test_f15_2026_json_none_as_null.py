"""Migration test for f15_2026_json_none_as_null (#2026).

Seeded at f14, then upgraded:

1. every JSON-null value in the walked columns becomes SQL NULL: memories
   (``details``, ``context``, ``supersede_candidate``, spread over several
   batches), the sleep tables, measurements, memory analyses, the BM25 drift
   log, erasure requests and workspace connectors. A row whose JSON null is in
   one column only has just that column rewritten;
2. rows holding objects and rows already SQL NULL are byte-identical
   afterwards (the text of the whole record, ``updated_at`` included);
3. the switch-only tables (audit_logs, llm_call_log, memory_access_events,
   secret_access_log, resource_events, neural_memory_edges) still hold their
   JSON null;
4. a failure mid-walk leaves the batches before it committed, and running the
   upgrade again resumes; a row a request holds is skipped (the walk does not
   wait), stays JSON null, and the documented rerun statement fixes it;
5. downgrade is a no-op, and a re-upgrade finds nothing to rewrite.

Not executed in the local unit run — needs the DB container (the cleanup of
the append-only tables uses ``session_replication_role``, so the test user
must be a superuser, as the CI and local containers are).
"""

from __future__ import annotations

import importlib
import json
import logging
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

# More JSON-null memories than BATCH_SIZE x 2, so the walk takes three batches
# and a failure in the last leaves two committed.
_BULK_ROWS = 2 * f15.BATCH_SIZE + 300
# Sort after every random uuid, so they land in the last batch; the mixed
# rows sort first, so they land in the first.
_LAST_ID = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
_HELD_ID = uuid.UUID("fffffff0-ffff-4fff-bfff-ffffffffffff")
_MIXED_IDS = [uuid.UUID(f"00000000-0000-4000-8000-00000000000{i}") for i in (1, 2, 3)]

_OBJ = '{"k": "v"}'

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


def _memory_values(conn: Connection, user: str) -> set[tuple[str | None, ...]]:
    return {
        tuple(row)
        for row in conn.execute(
            text(
                "SELECT details::text, context::text, supersede_candidate::text "
                "FROM memories WHERE user_id = :u"
            ),
            {"u": user},
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
    in every walked column, ``obj`` = objects, ``sql`` = SQL NULL)."""

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
        value = {"null": "null", "obj": _OBJ, "sql": None}[kind]
        names, values = self._spec(table)
        params: dict[str, object] = {
            "owner": self.owner,
            "ws": self.ws,
            "ctx": self.ctx,
            "report": getattr(self, "report", None),
            "hash": uuid.uuid4().hex + uuid.uuid4().hex,
            "v": value,
        }
        if table in ("workspace_connectors", "resource_events"):
            resource = uuid.uuid4()
            slug = f"{self.prefix}{resource.hex[:8]}"
            conn.execute(
                text("INSERT INTO resources (id, workspace_id, resource_id) VALUES (:id, :ws, :s)"),
                {"id": resource, "ws": self.ws, "s": slug},
            )
            params.update(resource=resource, slug=slug)
        casts = ", ".join(f"CAST(:v AS {self.json_type(table)})" for _ in columns)
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


_WALKED = tuple(t for t, _pk, _cols in f15.TABLES if t != "memories")
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
            conn.execute(text("DROP TRIGGER IF EXISTS f15_test_fail ON memories"))
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
                for table in ("llm_call_log", "erasure_requests", "audit_logs"):
                    for row_id in other.ids.get(table, {}).values():
                        conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
                conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": other.ws})
    finally:
        engine.dispose()


def test_f15_rewrites_json_null_only_and_resumes() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    prefix = f"f15-{uuid.uuid4().hex[:8]}-"
    bulk, mixed, objects, sql_null, held, last = (
        f"{prefix}{kind}" for kind in ("bulk", "mixed", "objects", "sqlnull", "held", "last")
    )
    other: _Other | None = None
    captured = _Captured()
    migration_logger = logging.getLogger("alembic.runtime.migration")
    migration_logger.addFilter(captured)
    holder = None
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            _memories(conn, bulk, "null", "null", "null", n=_BULK_ROWS)
            for memory_id in _MIXED_IDS:
                _memories(
                    conn, mixed, "null", '{"a": 1}', '{"memory_id": "x"}', memory_id=memory_id
                )
            _memories(conn, objects, _OBJ, '{"c": 1}', '{"dismissed": {"memory_id": "*"}}', n=3)
            _memories(conn, sql_null, None, None, None, n=2)
            _memories(conn, held, "null", "null", "null", memory_id=_HELD_ID)
            _memories(conn, last, "null", None, None, memory_id=_LAST_ID)
            other = _Other(conn, prefix)
            other.seed(conn, _WALKED, ("null", "obj", "sql"))
            other.seed(conn, _SWITCH_ONLY, ("null",))
            before = {user: _memory_rows(conn, user) for user in (objects, sql_null)}
            mixed_before = _memory_values(conn, mixed)
            other_before = {
                (t, k): other.record(conn, t, k) for t in _WALKED for k in ("obj", "sql")
            }
            assert _json_null_memories(conn, prefix) == _BULK_ROWS + 3 + 1 + 1
            # A failure in the last memories batch.
            conn.execute(
                text(
                    "CREATE FUNCTION f15_test_fail() RETURNS trigger AS $$ BEGIN "
                    "IF NEW.id = 'ffffffff-ffff-4fff-bfff-ffffffffffff' THEN "
                    "RAISE EXCEPTION 'f15 forced failure'; END IF; RETURN NEW; END $$ "
                    "LANGUAGE plpgsql"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER f15_test_fail BEFORE UPDATE ON memories "
                    "FOR EACH ROW EXECUTE FUNCTION f15_test_fail()"
                )
            )

        with pytest.raises(DBAPIError, match="f15 forced failure"):
            _upgrade_to(F15_REVISION)

        with engine.begin() as conn:
            assert _version(conn) == PRIOR_HEAD
            # Two batches (2000 rows: the mixed ones and 1997 bulk) committed;
            # the last one, with the failing row, rolled back. The other
            # tables come after memories.
            assert _json_null_memories(conn, prefix) == _BULK_ROWS + 5 - 2000
            assert _memory_values(conn, mixed) == {(None, '{"a": 1}', '{"memory_id": "x"}')}
            assert other.values(conn, "sleep_actions", "null") == ("null",)
            conn.execute(text("DROP TRIGGER f15_test_fail ON memories"))

        # A request holds one JSON-null row for the whole rerun.
        holder = engine.connect()
        holder.execute(
            text("SELECT id FROM memories WHERE user_id = :u FOR UPDATE"), {"u": held}
        ).all()
        captured.messages.clear()
        _upgrade_to(F15_REVISION)

        with engine.begin() as conn:
            assert _version(conn) == F15_REVISION
            assert _memory_values(conn, bulk) == {(None, None, None)}
            assert _memory_values(conn, last) == {(None, None, None)}
            # Only the JSON-null column of a mixed row was rewritten.
            assert mixed_before == {("null", '{"a": 1}', '{"memory_id": "x"}')}
            assert _memory_values(conn, mixed) == {(None, '{"a": 1}', '{"memory_id": "x"}')}
            for user, rows in before.items():
                assert _memory_rows(conn, user) == rows
            # The held row was skipped, not waited on.
            assert _memory_values(conn, held) == {("null", "null", "null")}
            for table in _WALKED:
                null_after = other.values(conn, table, "null")
                assert null_after == (None,) * len(other.columns(table)), table
                for kind in ("obj", "sql"):
                    assert other.record(conn, table, kind) == other_before[(table, kind)], table
            for table in _SWITCH_ONLY:
                assert other.values(conn, table, "null") == ("null",), table
        memory_lines = [m for m in captured.messages if m.startswith("#2026: memories:")]
        assert memory_lines == [
            "#2026: memories: set JSON null to SQL NULL on 304 rows "
            "(details=304, context=303, supersede_candidate=303); "
            "1 rows skipped (held by a request)"
        ]
        connector_line = next(
            m for m in captured.messages if m.startswith("#2026: workspace_connectors:")
        )
        assert "on 1 rows (channel_ids=1, pii_guardrail_config=1, runtime_config=1)" in (
            connector_line
        )

        holder.rollback()
        holder.close()
        holder = None
        # The documented rerun fixes what the walk skipped.
        with engine.begin() as conn:
            for column in f15.TABLES[0][2]:
                conn.execute(text(f15.rewrite_all_sql("memories", column)))
            assert _memory_values(conn, held) == {(None, None, None)}
            assert _json_null_memories(conn, prefix) == 0

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _memory_values(conn, bulk) == {(None, None, None)}
            assert other.values(conn, "erasure_requests", "null") == (None,)
        captured.messages.clear()
        _upgrade_to(F15_REVISION)
        assert [m for m in captured.messages if m.startswith("#2026: memories:")] == [
            "#2026: memories: set JSON null to SQL NULL on 0 rows "
            "(details=0, context=0, supersede_candidate=0); 0 rows skipped (held by a request)"
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


def test_object_seeds_are_json_objects() -> None:
    """The seed values the test above relies on parse as objects."""
    assert json.loads(_OBJ) == {"k": "v"}
