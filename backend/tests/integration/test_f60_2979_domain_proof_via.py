"""#2979 (f60): ``public_mcp_domains.last_proof_via``, a nullable column.

1. The upgrade adds ``last_proof_via`` VARCHAR(8) NULL with the CHECK
   ``ck_public_mcp_domains_last_proof_via`` (``txt``, ``cname``, ``both``) and
   takes no lock on another table: it runs while ``public_mcp_endpoints``,
   ``workspaces`` and ``users`` are held in ``ACCESS EXCLUSIVE`` by another
   transaction.
2. An existing row reads NULL; an INSERT that does not name the column (the
   previous release) gets NULL; a value outside the three is refused.
3. A rerun after an interrupted upgrade is a no-op; downgrade drops the column
   and the CHECK and keeps the rows.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f60_2979_domain_proof_via"
BEFORE = "f59_2980_context_fusion"
CHECK = "ck_public_mcp_domains_last_proof_via"

_INSERT = (
    "INSERT INTO public_mcp_domains "
    "(id, hostname, workspace_id, endpoint_id, state, verification_token) "
    "VALUES (:id, :hostname, :ws, :endpoint, 'pending', :token)"
)


def _column(conn) -> tuple[str, str, int | None, str | None] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type, character_maximum_length, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'public_mcp_domains' AND column_name = 'last_proof_via'"
        )
    ).first()
    return tuple(row) if row is not None else None


def _check(conn) -> str | None:
    return conn.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = :name AND conrelid = 'public_mcp_domains'::regclass"
        ),
        {"name": CHECK},
    )


def _insert(conn) -> uuid.UUID:
    rid = uuid.uuid4()
    conn.execute(
        text(_INSERT),
        {
            "id": rid,
            "hostname": f"f60-{rid.hex[:10]}.example.org",
            "ws": uuid.uuid4(),
            "endpoint": uuid.uuid4(),
            "token": uuid.uuid4().hex * 2,
        },
    )
    return rid


def _via(conn, rid: uuid.UUID) -> str | None:
    return conn.scalar(
        text("SELECT last_proof_via FROM public_mcp_domains WHERE id = :id"), {"id": rid}
    )


def test_f60_round_trip_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    ids: list[uuid.UUID] = []
    try:
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _column(conn) is None
            ids.append(_insert(conn))
            conn.commit()

        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text("LOCK TABLE public_mcp_endpoints, workspaces, users IN ACCESS EXCLUSIVE MODE")
            )
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()

        with engine.connect() as conn:
            assert _column(conn) == ("YES", "character varying", 8, None)
            check = _check(conn)
            assert check is not None and "NOT VALID" not in check
            for value in ("txt", "cname", "both"):
                assert f"'{value}'" in check
            assert _via(conn, ids[0]) is None, "an existing row reads NULL, no backfill"
            # The previous release's INSERT does not name the column.
            ids.append(_insert(conn))
            assert _via(conn, ids[1]) is None
            conn.execute(
                text("UPDATE public_mcp_domains SET last_proof_via = 'cname' WHERE id = :id"),
                {"id": ids[1]},
            )
            conn.commit()

        with pytest.raises(IntegrityError) as rejected, engine.begin() as conn:
            conn.execute(
                text("UPDATE public_mcp_domains SET last_proof_via = 'dns' WHERE id = :id"),
                {"id": ids[0]},
            )
        assert (
            getattr(rejected.value.orig, "sqlstate", None)
            or getattr(rejected.value.orig, "pgcode", None)
        ) == "23514"

        # A rerun after an interrupted upgrade keeps the stored value.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _via(conn, ids[1]) == "cname"

        _downgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _column(conn) is None
            assert _check(conn) is None
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM public_mcp_domains WHERE id = ANY(:ids)"),
                    {"ids": ids},
                )
                == 2
            )
        _upgrade_to(REVISION)
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
            with engine.connect() as conn:
                conn.execute(
                    text("DELETE FROM public_mcp_domains WHERE id = ANY(:ids)"), {"ids": ids}
                )
                conn.commit()
        finally:
            engine.dispose()
            holder.dispose()
