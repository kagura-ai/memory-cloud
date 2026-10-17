"""Migration test for e99_2022_supersede_cand_null (#2022).

1. upgrade turns every JSON-null ``supersede_candidate`` into SQL NULL, across
   more than one keyset batch (the seed is larger than ``BATCH_SIZE``);
2. a candidate object, a dismissal tombstone and an already SQL-NULL row are
   left exactly as they were;
3. downgrade is a no-op — the rewritten rows stay SQL NULL.

Every seeded row carries a ``user_id`` under one per-run prefix and is deleted
again in ``finally``.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

E99_REVISION = "e99_2022_supersede_cand_null"
PRIOR_HEAD = "e98_2007_mae_list_changes_since"

# More rows than the migration's BATCH_SIZE (5000), so the walk takes >1 step.
_JSON_NULL_ROWS = 6500

_CANDIDATE = {"memory_id": str(uuid.uuid4()), "summary": "older fact", "similarity": 0.93}
_TOMBSTONE = {"dismissed": {"memory_id": "*", "similarity": 1.0}}

_INSERT = (
    "INSERT INTO memories "
    "(id, user_id, workspace_id, summary, content, type, embedding_status, importance, "
    " confidence, scope, long_term, access_count, client, source, created_at, "
    " supersede_candidate) "
    "SELECT gen_random_uuid(), :user, gen_random_uuid(), 'seed', 'body', 'note', 'success', "
    "0.5, 1.0, 'working', false, 0, 'test', 'mcp_remember', now(), CAST(:cand AS json) "
    "FROM generate_series(1, :n)"
)


def _seed(conn: Connection, user: str, cand: str | None, n: int = 1) -> None:
    conn.execute(text(_INSERT), {"user": user, "cand": cand, "n": n})


def _stored(conn: Connection, user: str) -> list[str | None]:
    """Every seeded row's column as text (None = SQL NULL), bypassing the ORM."""
    return list(
        conn.execute(
            text("SELECT supersede_candidate::text FROM memories WHERE user_id = :u"),
            {"u": user},
        ).scalars()
    )


def test_e99_rewrites_json_null_only() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    prefix = f"e99-{uuid.uuid4().hex[:8]}-"
    json_null, candidate, tombstone, sql_null = (
        f"{prefix}{kind}" for kind in ("jsonnull", "candidate", "tombstone", "sqlnull")
    )
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            _seed(conn, json_null, "null", n=_JSON_NULL_ROWS)
            _seed(conn, candidate, json.dumps(_CANDIDATE))
            _seed(conn, tombstone, json.dumps(_TOMBSTONE))
            _seed(conn, sql_null, None)
            assert set(_stored(conn, json_null)) == {"null"}  # JSON null, not SQL NULL

        _upgrade_to(E99_REVISION)
        with engine.connect() as conn:
            assert _stored(conn, json_null) == [None] * _JSON_NULL_ROWS
            assert [json.loads(v) for v in _stored(conn, candidate)] == [_CANDIDATE]
            assert [json.loads(v) for v in _stored(conn, tombstone)] == [_TOMBSTONE]
            assert _stored(conn, sql_null) == [None]
            remaining = conn.execute(
                text(
                    "SELECT count(*) FROM memories "
                    "WHERE user_id LIKE :p AND supersede_candidate::text = 'null'"
                ),
                {"p": f"{prefix}%"},
            ).scalar_one()
            assert remaining == 0

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _stored(conn, json_null) == [None] * _JSON_NULL_ROWS
            assert [json.loads(v) for v in _stored(conn, candidate)] == [_CANDIDATE]
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM memories WHERE user_id LIKE :p"), {"p": f"{prefix}%"}
                    )
            finally:
                engine.dispose()
