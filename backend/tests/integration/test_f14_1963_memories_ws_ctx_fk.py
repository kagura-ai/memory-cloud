"""Migration tests for f14_1963_memories_ws_ctx_fk (#1963).

Seeded at f13, then upgraded:

1. a live cross-rooted memory (its workspace differs from its context's) is
   moved to the context's workspace and re-queued for embedding; a
   soft-deleted one, and one in a soft-deleted context, are moved with their
   status kept. The rows are spread over several repair batches;
2. consistent rows and rows without a context are byte-identical afterwards
   (every column, ``updated_at`` and ``embedding_status`` included);
3. a failure mid-repair leaves the batches before it committed, and running
   the upgrade again resumes; an INVALID leftover of the unique index is
   rebuilt;
4. the FK and the index exist and are valid; the FK refuses a cross-rooted
   insert, accepts a row without a context, and a context hard delete
   through the ORM removes its memories with both FKs in place;
5. downgrade drops the FK and the index and keeps the repaired data; the
   re-upgrade finds nothing to repair;
6. a memory whose context row is missing stops the migration before any row
   is written.

Not executed in the local unit run — needs the DB container (the orphan seed
and the INVALID index use ``session_replication_role`` / ``pg_index``, so the
test user must be a superuser, as the CI and local containers are).
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F14_REVISION = "f14_1963_memories_ws_ctx_fk"
PRIOR_HEAD = "f13_1952_api_keys_connector_id"
FK_NAME = "fk_memories_workspace_context"
INDEX_NAME = "ux_contexts_workspace_id_id"

# More cross-rooted rows than the migration's BATCH_SIZE (500) x 2, so the
# repair takes three batches and a failure in the last leaves two committed.
_BULK_CROSS_ROWS = 1100
# Sorts after every random uuid, so it lands in the last batch.
_LAST_ID = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")

_INSERT = (
    "INSERT INTO memories "
    "(id, user_id, workspace_id, context_id, summary, content, type, embedding_status, "
    " importance, confidence, scope, long_term, access_count, client, source, "
    " created_at, updated_at, deleted_at) "
    "SELECT COALESCE(CAST(:id AS uuid), gen_random_uuid()), :user, :ws, :ctx, 'seed', 'body', "
    "'note', :status, 0.5, 1.0, 'working', false, 0, 'test', 'mcp_remember', "
    "'2026-01-01T00:00:00', '2026-01-02T00:00:00', CAST(:deleted AS timestamp) "
    "FROM generate_series(1, :n)"
)


class _Captured(logging.Filter):
    """Records the migration's log lines.

    A filter, not a handler: ``alembic/env.py`` runs ``fileConfig`` on every
    command, which clears the handlers of the ``alembic.*`` loggers but keeps
    their filters.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        self.messages.append(record.getMessage())
        return True


def _workspace(conn: Connection, owner: str) -> uuid.UUID:
    workspace_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": workspace_id, "name": f"f14-{workspace_id.hex[:8]}", "owner": owner},
    )
    return workspace_id


def _context(
    conn: Connection, workspace_id: uuid.UUID, owner: str, deleted: bool = False
) -> uuid.UUID:
    context_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by, deleted_at) "
            "VALUES (:id, :ws, :name, :owner, CAST(:deleted AS timestamp))"
        ),
        {
            "id": context_id,
            "ws": workspace_id,
            "name": f"f14-{context_id.hex[:8]}",
            "owner": owner,
            "deleted": "2026-02-01T00:00:00" if deleted else None,
        },
    )
    return context_id


def _seed(
    conn: Connection,
    user: str,
    ws: uuid.UUID,
    ctx: uuid.UUID | None,
    *,
    status: str = "success",
    deleted: bool = False,
    n: int = 1,
    memory_id: uuid.UUID | None = None,
) -> None:
    conn.execute(
        text(_INSERT),
        {
            "id": memory_id,
            "user": user,
            "ws": ws,
            "ctx": ctx,
            "status": status,
            "deleted": "2026-03-01T00:00:00" if deleted else None,
            "n": n,
        },
    )


def _rows(conn: Connection, user: str) -> dict[uuid.UUID, str]:
    """Each seeded row as the text of the whole record (every column)."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            text("SELECT m.id, m::text FROM memories AS m WHERE m.user_id = :u"), {"u": user}
        )
    }


def _state(conn: Connection, user: str) -> set[tuple[uuid.UUID, str]]:
    return {
        (row[0], row[1])
        for row in conn.execute(
            text("SELECT workspace_id, embedding_status FROM memories WHERE user_id = :u"),
            {"u": user},
        )
    }


def _fk(conn: Connection) -> tuple[bool, str, str] | None:
    row = conn.execute(
        text(
            "SELECT convalidated, confupdtype, confdeltype FROM pg_constraint "
            "WHERE conrelid = 'memories'::regclass AND conname = :n"
        ),
        {"n": FK_NAME},
    ).first()
    return None if row is None else (row[0], str(row[1]), str(row[2]))


def _index(conn: Connection) -> tuple[bool, bool] | None:
    row = conn.execute(
        text(
            "SELECT i.indisvalid, i.indisunique FROM pg_class c "
            "JOIN pg_index i ON c.oid = i.indexrelid WHERE c.relname = :n"
        ),
        {"n": INDEX_NAME},
    ).first()
    return None if row is None else (row[0], row[1])


def _version(conn: Connection) -> str:
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _cleanup(prefix: str) -> None:
    engine = _sync_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM memories WHERE user_id LIKE :p"), {"p": f"{prefix}%"})
            conn.execute(
                text("DELETE FROM workspaces WHERE owner_user_id LIKE :p"), {"p": f"{prefix}%"}
            )
    finally:
        engine.dispose()


def test_f14_repairs_cross_rooted_rows_and_enforces_the_fk() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    prefix = f"f14-{uuid.uuid4().hex[:8]}-"
    owner = f"{prefix}owner"
    live, dead, in_dead_ctx, bulk, last, consistent, no_ctx = (
        f"{prefix}{kind}"
        for kind in ("live", "dead", "deadctx", "bulk", "last", "consistent", "noctx")
    )
    captured = _Captured()
    migration_logger = logging.getLogger("alembic.runtime.migration")
    migration_logger.addFilter(captured)
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            ws_a, ws_b = _workspace(conn, owner), _workspace(conn, owner)
            ctx_a = _context(conn, ws_a, owner)
            ctx_a_deleted = _context(conn, ws_a, owner, deleted=True)
            ctx_b = _context(conn, ws_b, owner)
            _seed(conn, live, ws_b, ctx_a)
            _seed(conn, dead, ws_b, ctx_a, deleted=True)
            _seed(conn, in_dead_ctx, ws_b, ctx_a_deleted)
            _seed(conn, bulk, ws_b, ctx_a, status="failed", n=_BULK_CROSS_ROWS)
            _seed(conn, last, ws_b, ctx_a, memory_id=_LAST_ID)
            _seed(conn, consistent, ws_a, ctx_a, n=3)
            _seed(conn, consistent, ws_b, ctx_b, status="pending", deleted=True)
            _seed(conn, no_ctx, ws_b, None, n=2)
            before = {user: _rows(conn, user) for user in (consistent, no_ctx, dead, in_dead_ctx)}

            # An interrupted earlier build left the unique index INVALID.
            conn.execute(text(f"CREATE UNIQUE INDEX {INDEX_NAME} ON contexts (workspace_id, id)"))
            conn.execute(
                text(
                    "UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:n AS regclass)"
                ),
                {"n": INDEX_NAME},
            )
            # A failure in the last repair batch.
            conn.execute(
                text(
                    "CREATE FUNCTION f14_test_fail() RETURNS trigger AS $$ BEGIN "
                    "IF NEW.id = 'ffffffff-ffff-4fff-bfff-ffffffffffff' THEN "
                    "RAISE EXCEPTION 'f14 forced failure'; END IF; RETURN NEW; END $$ "
                    "LANGUAGE plpgsql"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER f14_test_fail BEFORE UPDATE ON memories "
                    "FOR EACH ROW EXECUTE FUNCTION f14_test_fail()"
                )
            )

        with pytest.raises(DBAPIError, match="f14 forced failure"):
            _upgrade_to(F14_REVISION)

        with engine.begin() as conn:
            assert _version(conn) == PRIOR_HEAD
            assert _index(conn) == (True, True)  # the leftover was rebuilt
            assert _fk(conn) == (False, "a", "c")  # added, not yet validated
            # 1104 cross-rooted rows: the first two batches (1000 rows)
            # committed, the last one (with the failing row) rolled back.
            moved_first = conn.execute(
                text(
                    "SELECT count(*) FROM memories WHERE user_id = ANY(:users) "
                    "AND workspace_id = :ws"
                ),
                {"users": [live, dead, in_dead_ctx, bulk, last], "ws": ws_a},
            ).scalar_one()
            assert moved_first == 1000
            assert _state(conn, last) == {(ws_b, "success")}
            conn.execute(text("DROP TRIGGER f14_test_fail ON memories"))
            conn.execute(text("DROP FUNCTION f14_test_fail()"))

        captured.messages.clear()
        _upgrade_to(F14_REVISION)

        with engine.begin() as conn:
            assert _version(conn) == F14_REVISION
            assert _fk(conn) == (True, "a", "c")
            assert _index(conn) == (True, True)

            # Live rows move and are re-queued; the bulk rows were 'failed'.
            assert _state(conn, live) == {(ws_a, "pending")}
            assert _state(conn, last) == {(ws_a, "pending")}
            assert _state(conn, bulk) == {(ws_a, "pending")}
            # Soft-deleted rows, or rows in a soft-deleted context, move and
            # keep everything else.
            for user in (dead, in_dead_ctx):
                after = _rows(conn, user)
                assert after == {
                    k: v.replace(str(ws_b), str(ws_a)) for k, v in before[user].items()
                }
                assert _state(conn, user) == {(ws_a, "success")}
            # Untouched rows are byte-identical.
            assert _rows(conn, consistent) == before[consistent]
            assert _rows(conn, no_ctx) == before[no_ctx]

            remaining = conn.execute(
                text(
                    "SELECT count(*) FROM memories m JOIN contexts c ON c.id = m.context_id "
                    "WHERE m.workspace_id <> c.workspace_id"
                )
            ).scalar_one()
            assert remaining == 0

        # The rerun moved only what the failed run left: the last batch.
        moved = [m for m in captured.messages if "cross-rooted memories" in m]
        assert len(moved) == 1 and moved[0].startswith("#1963: moved 104 "), captured.messages
        assert any(f"{ws_b} -> {ws_a}" in m for m in captured.messages), captured.messages

        # The FK in force: a cross-rooted insert is refused, a row without a
        # context is fine.
        with engine.connect() as conn:
            with pytest.raises(IntegrityError, match=FK_NAME):
                _seed(conn, f"{prefix}refused", ws_b, ctx_a)
            conn.rollback()
            _seed(conn, no_ctx, ws_a, None)
            conn.commit()

        # A context hard delete through the ORM cascades through both FKs.
        from models.auth import Context

        with Session(engine) as session:
            session.delete(session.get(Context, ctx_a))
            session.commit()
        with engine.begin() as conn:
            assert _rows(conn, live) == {}
            assert _rows(conn, bulk) == {}
            assert len(_rows(conn, in_dead_ctx)) == 1
            assert len(_rows(conn, no_ctx)) == 3

        _downgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            assert _fk(conn) is None
            assert _index(conn) is None
            assert _state(conn, in_dead_ctx) == {(ws_a, "success")}  # repaired data stays

        captured.messages.clear()
        _upgrade_to(F14_REVISION)
        assert "#1963: no cross-rooted memories" in captured.messages
        with engine.begin() as conn:
            assert _fk(conn) == (True, "a", "c")
    finally:
        migration_logger.removeFilter(captured)
        try:
            with engine.begin() as conn:
                conn.execute(text("DROP TRIGGER IF EXISTS f14_test_fail ON memories"))
                conn.execute(text("DROP FUNCTION IF EXISTS f14_test_fail()"))
            _upgrade_to("head")
            _cleanup(prefix)
        finally:
            engine.dispose()


def test_f14_refuses_on_a_memory_without_its_context_row() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    prefix = f"f14-{uuid.uuid4().hex[:8]}-"
    owner = f"{prefix}owner"
    orphan, cross = f"{prefix}orphan", f"{prefix}cross"
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            ws_a, ws_b = _workspace(conn, owner), _workspace(conn, owner)
            ctx_a = _context(conn, ws_a, owner)
            _seed(conn, cross, ws_b, ctx_a)
            # RI triggers off for this transaction: a row the context_id FK
            # would refuse.
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            _seed(conn, orphan, ws_a, uuid.uuid4())

        with pytest.raises(RuntimeError, match="1 memories name a context that does not exist"):
            _upgrade_to(F14_REVISION)

        with engine.begin() as conn:
            assert _version(conn) == PRIOR_HEAD
            assert _state(conn, cross) == {(ws_b, "success")}  # nothing written
            # The operator deletes the row and runs the migration again.
            conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": orphan})

        _upgrade_to(F14_REVISION)
        with engine.begin() as conn:
            assert _version(conn) == F14_REVISION
            assert _state(conn, cross) == {(ws_a, "pending")}
            assert _fk(conn) == (True, "a", "c")
    finally:
        try:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": orphan})
            _upgrade_to("head")
            _cleanup(prefix)
        finally:
            engine.dispose()
