"""Migration round-trip for f22_2639_context_members_uq (#2639).

1. upgrade over a database that already holds duplicate ``context_members``
   rows keeps one row per (context_id, user_id): the oldest by ``created_at``,
   the lowest ``id`` on a tie. Rows of other pairs are untouched;
2. the unique constraint is then in place: a further insert of a kept pair
   raises an ``IntegrityError`` that names it;
3. downgrade drops the constraint only (the deleted duplicates stay deleted).

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db.constraint_names import (
    CONTEXT_MEMBERS_CONTEXT_USER_UNIQUE,
    integrity_error_constraint_name,
)
from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F22_REVISION = "f22_2639_context_members_uq"
PRIOR_HEAD = "f21_2476_deferred_point_deletes"
ACTOR = "f22-migration-test"


def _seed(conn: Any, workspace_id: uuid.UUID, context_id: uuid.UUID) -> dict[str, int]:
    """Duplicated membership rows in the f21 schema (raw SQL with explicit
    columns, so a later model column cannot break the insert)."""
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :actor, 'free')"
        ),
        {"id": workspace_id, "name": f"f22-{workspace_id.hex[:8]}", "actor": ACTOR},
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, :actor)"
        ),
        {"id": context_id, "ws": workspace_id, "name": f"f22-{context_id.hex[:8]}", "actor": ACTOR},
    )

    def _member(user_id: str, role: str, created_at: str) -> int:
        return conn.execute(
            text(
                "INSERT INTO context_members (context_id, user_id, role, created_at) "
                "VALUES (:ctx, :u, :role, :at) RETURNING id"
            ),
            {"ctx": context_id, "u": user_id, "role": role, "at": created_at},
        ).scalar_one()

    # "dup": the older row is inserted second (higher id), so the dedupe
    # must order by created_at, not by id.
    return {
        "dup_newer": _member("f22-dup", "owner", "2026-01-02 00:00:00"),
        "dup_older": _member("f22-dup", "viewer", "2026-01-01 00:00:00"),
        "dup_newest": _member("f22-dup", "editor", "2026-01-03 00:00:00"),
        # "tie": same created_at, the lowest id is kept.
        "tie_first": _member("f22-tie", "editor", "2026-02-01 00:00:00"),
        "tie_second": _member("f22-tie", "owner", "2026-02-01 00:00:00"),
        "single": _member("f22-single", "viewer", "2026-03-01 00:00:00"),
    }


def _rows(conn: Any, context_id: uuid.UUID) -> dict[str, list[int]]:
    rows = conn.execute(
        text("SELECT user_id, id FROM context_members WHERE context_id = :c ORDER BY user_id, id"),
        {"c": context_id},
    ).all()
    out: dict[str, list[int]] = {}
    for user_id, row_id in rows:
        out.setdefault(user_id, []).append(row_id)
    return out


def _constraint_exists(conn: Any) -> bool:
    return (
        conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = :n AND conrelid = 'context_members'::regclass AND contype = 'u'"
            ),
            {"n": CONTEXT_MEMBERS_CONTEXT_USER_UNIQUE},
        ).scalar_one()
        == 1
    )


def test_f22_dedupes_then_enforces_one_row_per_pair() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id, context_id = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            assert not _constraint_exists(conn)
            ids = _seed(conn, workspace_id, context_id)

        _upgrade_to(F22_REVISION)
        with engine.connect() as conn:
            assert _constraint_exists(conn)
            assert _rows(conn, context_id) == {
                "f22-dup": [ids["dup_older"]],
                "f22-tie": [ids["tie_first"]],
                "f22-single": [ids["single"]],
            }
            # The kept row is the oldest one, untouched (its role included).
            assert (
                conn.execute(
                    text("SELECT role FROM context_members WHERE id = :i"),
                    {"i": ids["dup_older"]},
                ).scalar_one()
                == "viewer"
            )

        with pytest.raises(IntegrityError) as raised, engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO context_members (context_id, user_id, role) "
                    "VALUES (:ctx, 'f22-single', 'editor')"
                ),
                {"ctx": context_id},
            )
        assert integrity_error_constraint_name(raised.value) == CONTEXT_MEMBERS_CONTEXT_USER_UNIQUE

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert not _constraint_exists(conn)
            # The downgrade drops the constraint only.
            assert set(_rows(conn, context_id)) == {"f22-dup", "f22-tie", "f22-single"}
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == PRIOR_HEAD
            )
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM context_members WHERE context_id = :c"),
                        {"c": context_id},
                    )
                    conn.execute(text("DELETE FROM contexts WHERE id = :c"), {"c": context_id})
                    conn.execute(
                        text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id}
                    )
            finally:
                engine.dispose()
