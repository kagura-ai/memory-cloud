"""DB contract for f45 (#2800): ``mcp_notices.plans``.

- upgrade adds a nullable ``varchar(50)[]`` column and the CHECK
  ``ck_mcp_notices_plans_nonempty``; an existing notice reads ``NULL`` (it
  still goes to everyone); the database refuses an empty array;
- downgrade refuses (``RuntimeError``, the column kept) while a notice with
  plans is live or scheduled, and runs once it is withdrawn or has expired;
  live notices without plans do not block it;
- downgrade takes the create advisory lock before it counts, so a targeted
  create that has inserted but not committed is counted, not dropped under;
- the round trip is repeatable.

The column against the model is compared by
``test_create_all_vs_alembic_drift.py`` (it does not compare the element
width, so the type is checked here), the CHECK by ``tests/test_schema_drift.py``.
Not executed in the local unit run: needs the DB container.
"""

from __future__ import annotations

import importlib.util
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f45_2800_mcp_notice_plans.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("_f45_2800_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _migration_module()
F45_REV = _MODULE.revision
PRIOR_REV = _MODULE.down_revision


def _to(revision: str) -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), revision)


def _down() -> None:
    with _alembic_at_test_db():
        command.downgrade(_get_alembic_config(), PRIOR_REV)


def _leave_db_at_head() -> None:
    _to("head")


def _start_at_prior() -> None:
    _reset_alembic_state()
    _to(PRIOR_REV)


def _has_column(conn: Connection) -> bool:
    return (
        conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'mcp_notices' AND column_name = 'plans'"
            )
        ).scalar_one()
        == 1
    )


def _untargeted(conn: Connection, *, starts: str = "now()", ends: str = "now() + interval '1 day'"):
    return conn.execute(
        text(
            "INSERT INTO mcp_notices (text, starts_at, expires_at)"
            f" VALUES ('Maintenance on Saturday.', {starts}, {ends}) RETURNING id"
        )
    ).scalar_one()


def _targeted(
    conn: Connection,
    *,
    starts: str = "now()",
    ends: str = "now() + interval '1 day'",
    withdrawn: str = "NULL",
    created: str = "now()",
) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO mcp_notices (text, starts_at, expires_at, withdrawn_at, created_at, plans)"
            f" VALUES ('For pro only.', {starts}, {ends}, {withdrawn}, {created}, ARRAY['pro'])"
            " RETURNING id"
        )
    ).scalar_one()


def _clear(conn: Connection) -> None:
    conn.execute(text("DELETE FROM mcp_notices"))


class TestF45McpNoticePlans:
    def test_revision_ids(self) -> None:
        assert F45_REV == "f45_2800_mcp_notice_plans"
        assert len(F45_REV) <= 32
        assert PRIOR_REV == "f44_2802_sync_installations"
        assert len(PRIOR_REV) <= 32

    def test_upgrade_adds_the_nullable_column_and_the_check(self) -> None:
        engine = _sync_engine()
        try:
            _start_at_prior()
            with engine.begin() as conn:
                assert not _has_column(conn)
                old = _untargeted(conn)

            _to(F45_REV)

            with engine.begin() as conn:
                column_type, not_null = conn.execute(
                    text(
                        "SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute"
                        " WHERE attrelid = 'mcp_notices'::regclass AND attname = 'plans'"
                    )
                ).one()
                assert column_type == "character varying(50)[]"
                assert not_null is False
                assert (
                    conn.execute(
                        text("SELECT plans FROM mcp_notices WHERE id = :id"), {"id": old}
                    ).scalar_one()
                    is None
                )
                check = conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                        " WHERE conname = 'ck_mcp_notices_plans_nonempty'"
                    )
                ).scalar_one()
                assert "cardinality(plans) >= 1" in check

            with pytest.raises(IntegrityError) as caught:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO mcp_notices (text, starts_at, expires_at, plans)"
                            " VALUES ('x', now(), now() + interval '1 day', '{}')"
                        )
                    )
            # 23514 check_violation (psycopg2 names it ``pgcode``).
            assert getattr(caught.value.orig, "pgcode", None) == "23514"
            assert "ck_mcp_notices_plans_nonempty" in str(caught.value)

            with engine.begin() as conn:
                accepted = _targeted(conn)
                assert conn.execute(
                    text("SELECT plans FROM mcp_notices WHERE id = :id"), {"id": accepted}
                ).scalar_one() == ["pro"]
                _clear(conn)
        finally:
            engine.dispose()
            _leave_db_at_head()

    def test_downgrade_refuses_while_a_targeted_notice_is_live_or_scheduled(self) -> None:
        engine = _sync_engine()
        try:
            _start_at_prior()
            _to(F45_REV)
            for when in ("live", "scheduled"):
                with engine.begin() as conn:
                    _clear(conn)
                    if when == "live":
                        notice = _targeted(conn)
                    else:
                        notice = _targeted(
                            conn,
                            starts="now() + interval '2 days'",
                            ends="now() + interval '3 days'",
                        )
                with pytest.raises(RuntimeError) as caught:
                    _down()
                message = str(caught.value)
                assert "1 " in message
                assert "/api/v1/admin/mcp-notices/" in message and "/withdraw" in message
                assert "For pro only." not in message
                with engine.begin() as conn:
                    assert _has_column(conn), when
                    assert conn.execute(
                        text("SELECT plans FROM mcp_notices WHERE id = :id"), {"id": notice}
                    ).scalar_one() == ["pro"]

            # Withdrawn: the downgrade runs. Live notices without plans never block.
            with engine.begin() as conn:
                _clear(conn)
                _targeted(conn, withdrawn="now()")
                _untargeted(conn)
                _untargeted(
                    conn, starts="now() + interval '2 days'", ends="now() + interval '3 days'"
                )
            _down()
            with engine.begin() as conn:
                assert not _has_column(conn)
                assert (
                    conn.execute(
                        text(
                            "SELECT count(*) FROM pg_constraint"
                            " WHERE conname = 'ck_mcp_notices_plans_nonempty'"
                        )
                    ).scalar_one()
                    == 0
                )
                assert conn.execute(text("SELECT count(*) FROM mcp_notices")).scalar_one() == 3

            # Expired: the downgrade runs too.
            _to(F45_REV)
            with engine.begin() as conn:
                _clear(conn)
                _targeted(
                    conn,
                    starts="now() - interval '3 days'",
                    ends="now() - interval '1 second'",
                    created="now() - interval '3 days'",
                )
            _down()
            with engine.begin() as conn:
                assert not _has_column(conn)
                _clear(conn)
        finally:
            engine.dispose()
            _leave_db_at_head()

    def test_downgrade_takes_the_create_lock_before_it_counts(self, monkeypatch) -> None:
        """A targeted create holds the create lock and has inserted, not yet
        committed. The downgrade waits on that lock (not on the table), so it
        counts the notice once the create commits and refuses."""
        monkeypatch.setenv("KAGURA_MIGRATION_LOCK_TIMEOUT", "30s")
        engine = _sync_engine()
        holder = engine.connect()
        outcome: dict[str, BaseException | None] = {}
        try:
            _start_at_prior()
            _to(F45_REV)
            with engine.begin() as conn:
                _clear(conn)

            holder_tx = holder.begin()
            holder.execute(text(_MODULE.CREATE_LOCK_SQL))
            _targeted(holder)

            def run_downgrade() -> None:
                try:
                    _down()
                    outcome["error"] = None
                except BaseException as exc:  # noqa: BLE001 - reported to the test thread
                    outcome["error"] = exc

            worker = threading.Thread(target=run_downgrade, daemon=True)
            worker.start()

            deadline = time.monotonic() + 25
            waiting = 0
            with engine.connect() as probe:
                while time.monotonic() < deadline:
                    waiting = probe.execute(
                        text(
                            "SELECT count(*) FROM pg_locks"
                            " WHERE locktype = 'advisory' AND NOT granted"
                        )
                    ).scalar_one()
                    probe.rollback()
                    if waiting or not worker.is_alive():
                        break
                    time.sleep(0.1)
            assert waiting == 1, "the downgrade did not wait on the create lock"

            holder_tx.commit()
            worker.join(timeout=60)
            assert not worker.is_alive()
            assert isinstance(outcome.get("error"), RuntimeError), outcome
            with engine.begin() as conn:
                assert _has_column(conn)
                _clear(conn)
        finally:
            holder.close()
            engine.dispose()
            _leave_db_at_head()

    def test_round_trip(self) -> None:
        engine = _sync_engine()
        try:
            _start_at_prior()
            _to(F45_REV)
            _down()
            with engine.begin() as conn:
                assert not _has_column(conn)
            _to(F45_REV)
            with engine.begin() as conn:
                assert _has_column(conn)
                _targeted(conn)
                _clear(conn)
        finally:
            engine.dispose()
            _leave_db_at_head()
