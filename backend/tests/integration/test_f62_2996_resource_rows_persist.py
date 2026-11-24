"""#2996 (f62): the indexer's live working rows become persistent, nothing else changes.

1. The upgrade moves exactly the live working rows that own their indexer
   point (``source='resource_ingest'``); a tombstone, a plain working row, a
   context-merge copy of an indexer row, a re-pointed ``remember`` row and an
   already persistent resource row stay as they are. ``version`` and
   ``updated_at`` are untouched.
2. A rerun (``RERUN_SQL``, and the upgrade itself after an interrupted run) is
   a no-op on the moved rows; downgrade changes nothing.
3. The walk pages by id and skips a row a request holds, which the rerun
   then moves.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from services.resource_indexer import resource_point_id
from tests.integration.test_alembic_migrations import (
    TestB03NeuralEdgesBackfillMigration,
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f62_2996_resource_rows_persist"
BEFORE = "f61_2984_keyword_lengths"


def _migration() -> Any:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(REVISION, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(
    conn: Connection,
    owner: str,
    ws_id: str,
    ctx_id: str,
    *,
    doc_id: str | None = "doc",
    source: str = "resource_ingest",
    scope: str = "working",
    own_point: bool = False,
    deleted: bool = False,
    promoted: bool = False,
) -> str:
    mem_id = str(uuid.uuid4())
    details = {"resource_id": "catalog", "doc_id": doc_id, "version": 1} if doc_id else {}
    point = mem_id if own_point or not doc_id else str(resource_point_id("catalog", doc_id, 1))
    conn.execute(
        text(
            "INSERT INTO memories "
            "(id, user_id, workspace_id, context_id, summary, content, type, "
            " embedding_status, importance, confidence, scope, long_term, "
            " access_count, client, source, details, summary_embedding_id, version, "
            " deleted_at, promoted_at, updated_at) "
            "VALUES (:id, :u, :ws, :ctx, 's', 'c', 'resource_data', 'success', "
            " 0.6, 1.0, :scope, false, 0, 'resource_indexer', :source, CAST(:details AS JSON), "
            " :point, 1, "
            " CASE WHEN :deleted THEN timezone('UTC', now()) END, "
            " CASE WHEN :promoted THEN TIMESTAMP '2020-01-01 00:00:00' END, "
            " TIMESTAMP '2021-01-01 00:00:00')"
        ),
        {
            "id": mem_id,
            "u": owner,
            "ws": ws_id,
            "ctx": ctx_id,
            "scope": scope,
            "source": source,
            "details": json.dumps(details),
            "point": point,
            "deleted": deleted,
            "promoted": promoted,
        },
    )
    return mem_id


def _rows(conn: Connection, ids: list[str]) -> dict[str, tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT id::text, scope, promoted_at, version, updated_at "
            "FROM memories WHERE id::text = ANY(:ids)"
        ),
        {"ids": ids},
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def test_f62_moves_only_live_working_indexer_rows_and_reruns_cleanly() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(BEFORE)
        owner = f"owner-2996-{uuid.uuid4().hex[:8]}"
        with engine.begin() as conn:
            ws_id, ctx_id = TestB03NeuralEdgesBackfillMigration._seed_workspace_and_context(
                conn, owner
            )
            seed = lambda **kw: _seed(conn, owner, ws_id, ctx_id, **kw)  # noqa: E731
            moved = [seed(doc_id="a"), seed(doc_id="b")]
            kept = {
                "tombstone": seed(doc_id="c", deleted=True),
                "plain working row": seed(doc_id=None, source="mcp_remember"),
                "merge copy": seed(doc_id="d", own_point=True),
                "re-pointed remember row": seed(doc_id="e", source="mcp_remember"),
                "persistent resource row": seed(doc_id="f", scope="persistent", promoted=True),
            }
            before = _rows(conn, [*moved, *kept.values()])

        _upgrade_to(REVISION)

        with engine.begin() as conn:
            after = _rows(conn, [*moved, *kept.values()])
            stamped = conn.execute(text("SELECT timezone('UTC', now())")).scalar_one()
        for mem_id in moved:
            scope, promoted_at, version, updated_at = after[mem_id]
            assert scope == "persistent"
            assert promoted_at is not None and abs((stamped - promoted_at).total_seconds()) < 300
            assert (version, updated_at) == before[mem_id][2:]
        assert {name: after[i] for name, i in kept.items()} == {
            name: before[i] for name, i in kept.items()
        }

        # A rerun after an interrupted upgrade, and the documented statement:
        # nothing left to move, the stamps stay.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        module = _migration()
        with engine.begin() as conn:
            assert conn.execute(text(module.RERUN_SQL)).rowcount == 0
            assert _rows(conn, [*moved, *kept.values()]) == after

        # Downgrade is a no-op.
        _downgrade_to(BEFORE)
        with engine.begin() as conn:
            assert _rows(conn, [*moved, *kept.values()]) == after
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()


def test_f62_walk_pages_by_id_and_skips_a_held_row() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    try:
        _upgrade_to(BEFORE)
        owner = f"owner-2996-{uuid.uuid4().hex[:8]}"
        with engine.begin() as conn:
            ws_id, ctx_id = TestB03NeuralEdgesBackfillMigration._seed_workspace_and_context(
                conn, owner
            )
            ids = sorted(_seed(conn, owner, ws_id, ctx_id, doc_id=f"doc-{n}") for n in range(5))
        module = _migration()

        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text("SELECT 1 FROM memories WHERE id = CAST(:id AS uuid) FOR UPDATE"),
                {"id": ids[2]},
            )
            try:
                with engine.begin() as conn:
                    assert module.persist_resource_rows(conn, batch_size=2) == 4
            finally:
                lock_tx.rollback()

        with engine.begin() as conn:
            scopes = {i: row[0] for i, row in _rows(conn, ids).items()}
            assert scopes == {i: "working" if i == ids[2] else "persistent" for i in ids}
            assert conn.execute(text(module.RERUN_SQL)).rowcount == 1
            assert {row[0] for row in _rows(conn, ids).values()} == {"persistent"}
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
            holder.dispose()
