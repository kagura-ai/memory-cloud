"""Real Alembic upgrade/downgrade for ``public_mcp_domains`` (#2809).

One new table, no foreign key (so the revision locks no existing table), the
named CHECKs and the two partial unique indexes: one holder per host name,
one open domain per publication.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f50_2809_public_mcp_domains"
_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f50_2809_public_mcp_domains.py"
)

_EXPECTED_COLUMNS = {
    "id": ("uuid", "NO"),
    "hostname": ("character varying", "NO"),
    "workspace_id": ("uuid", "NO"),
    "endpoint_id": ("uuid", "NO"),
    "state": ("character varying", "NO"),
    "suspended_reason": ("character varying", "YES"),
    "verification_token": ("character varying", "NO"),
    "serve_until": ("timestamp without time zone", "YES"),
    "check_seq": ("bigint", "NO"),
    "last_proof_result": ("character varying", "YES"),
    "routing_status": ("character varying", "YES"),
    "proof_ok_at": ("timestamp without time zone", "YES"),
    "proof_missing_checks": ("integer", "NO"),
    "created_at": ("timestamp without time zone", "NO"),
    "verified_at": ("timestamp without time zone", "YES"),
    "last_checked_at": ("timestamp without time zone", "YES"),
    "suspended_at": ("timestamp without time zone", "YES"),
    "released_at": ("timestamp without time zone", "YES"),
    "cooldown_until": ("timestamp without time zone", "YES"),
}

_EXPECTED_CHECKS = {
    "ck_public_mcp_domains_state",
    "ck_public_mcp_domains_suspended_reason",
    "ck_public_mcp_domains_suspended",
    "ck_public_mcp_domains_released",
    "ck_public_mcp_domains_active_lease",
    "ck_public_mcp_domains_hostname",
    "ck_public_mcp_domains_last_proof_result",
    "ck_public_mcp_domains_routing_status",
}


def _module():
    spec = importlib.util.spec_from_file_location("f50_2809", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(conn, **values) -> None:
    row = {
        "hostname": f"mcp-{uuid4().hex[:8]}.example.org",
        "workspace_id": uuid4(),
        "endpoint_id": uuid4(),
        "state": "pending",
        "verification_token": "a" * 64,
    }
    row.update(values)
    columns = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    conn.execute(text(f"INSERT INTO public_mcp_domains ({columns}) VALUES ({params})"), row)


def _insert_active(conn, **values) -> None:
    row = {
        "hostname": f"mcp-{uuid4().hex[:8]}.example.org",
        "workspace_id": uuid4(),
        "endpoint_id": uuid4(),
        "verification_token": "b" * 64,
    }
    row.update(values)
    conn.execute(
        text(
            "INSERT INTO public_mcp_domains (hostname, workspace_id, endpoint_id, state, "
            "verification_token, serve_until, verified_at, proof_ok_at) VALUES "
            "(:hostname, :workspace_id, :endpoint_id, 'active', :verification_token, "
            "now() + interval '72 hours', now(), now())"
        ),
        row,
    )


def _refused(engine, statement: str, params: dict | None = None) -> None:
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(statement), params or {})


def test_f50_metadata() -> None:
    module = _module()
    assert module.revision == REVISION
    assert module.down_revision == "f48_2807_sync_feed_runs"
    source = _PATH.read_text(encoding="utf-8")
    assert "from src" not in source and "import src" not in source
    assert "ForeignKey" not in source


def test_f50_creates_the_table_and_downgrades_cleanly() -> None:
    prior = _module().down_revision
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(prior)
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            columns = {
                row.column_name: (row.data_type, row.is_nullable)
                for row in conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'public_mcp_domains'"
                    )
                ).all()
            }
            assert columns == _EXPECTED_COLUMNS
            fks = conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conrelid = 'public_mcp_domains'::regclass AND contype = 'f'"
                )
            ).scalar_one()
            assert fks == 0
            checks = {
                row.conname
                for row in conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'public_mcp_domains'::regclass AND contype = 'c'"
                    )
                ).all()
            }
            assert checks == _EXPECTED_CHECKS
            indexes = {
                row.indexname: row.indexdef
                for row in conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'public_mcp_domains'"
                    )
                ).all()
            }
            assert set(indexes) == {
                "public_mcp_domains_pkey",
                "ux_public_mcp_domains_hostname_held",
                "ux_public_mcp_domains_endpoint_open",
                "ix_public_mcp_domains_hostname",
                "ix_public_mcp_domains_workspace_id",
                "ix_public_mcp_domains_due",
            }
            held = indexes["ux_public_mcp_domains_hostname_held"]
            assert held.startswith("CREATE UNIQUE INDEX") and "(hostname)" in held
            assert "'active'" in held and "'suspended'" in held and "'pending'" not in held
            open_ = indexes["ux_public_mcp_domains_endpoint_open"]
            assert open_.startswith("CREATE UNIQUE INDEX") and "(endpoint_id)" in open_
            assert "'released'" in open_
            due = indexes["ix_public_mcp_domains_due"]
            assert "(last_checked_at)" in due and "'active'" in due and "'suspended'" in due

        # Server defaults.
        with engine.begin() as conn:
            _insert(conn, hostname="defaults.example.org")
            row = conn.execute(
                text(
                    "SELECT id, check_seq, proof_missing_checks, created_at, "
                    "timezone('UTC', statement_timestamp()) - created_at AS age "
                    "FROM public_mcp_domains WHERE hostname = 'defaults.example.org'"
                )
            ).one()
            assert row.id is not None
            assert row.check_seq == 0
            assert row.proof_missing_checks == 0
            assert abs(row.age.total_seconds()) < 60  # naive UTC from the database clock

        _check_constraints(engine)
        _check_unique_indexes(engine)

        _downgrade_to(prior)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('public.public_mcp_domains')")).scalar()
                is None
            )
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM public_mcp_domains")).scalar_one() == 0
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")


def _check_constraints(engine) -> None:
    insert = (
        "INSERT INTO public_mcp_domains (hostname, workspace_id, endpoint_id, state, "
        "suspended_reason, verification_token, serve_until, verified_at, proof_ok_at, "
        "released_at, cooldown_until, last_proof_result, routing_status) VALUES "
        "(:hostname, gen_random_uuid(), gen_random_uuid(), :state, :reason, :token, "
        ":serve_until, :verified_at, :proof_ok_at, :released_at, :cooldown_until, "
        ":proof, :routing)"
    )

    def params(**values) -> dict:
        base = {
            "hostname": f"c-{uuid4().hex[:8]}.example.org",
            "state": "pending",
            "reason": None,
            "token": "c" * 64,
            "serve_until": None,
            "verified_at": None,
            "proof_ok_at": None,
            "released_at": None,
            "cooldown_until": None,
            "proof": None,
            "routing": None,
        }
        base.update(values)
        return base

    now = datetime(2026, 11, 15, 12, 0, 0)
    lease = {"serve_until": now, "verified_at": now, "proof_ok_at": now}

    # Accepted shapes.
    with engine.begin() as conn:
        conn.execute(text(insert), params())
        conn.execute(text(insert), params(state="active", **lease, proof="ok", routing="ok"))
        conn.execute(
            text(insert),
            params(state="suspended", reason="proof_lost", proof="missing", routing="mismatch"),
        )
        conn.execute(
            text(insert),
            params(
                state="released",
                released_at=now,
                cooldown_until=now,
                proof="unavailable",
                routing="unresolved",
            ),
        )

    refused = [
        params(state="deleted"),
        params(state="suspended"),  # suspended without a reason
        params(state="active", reason="reserved", **lease),  # reason outside suspended
        params(state="suspended", reason="tired"),
        params(state="released"),  # released without its times
        params(state="released", released_at=now),
        params(state="pending", released_at=now, cooldown_until=now),
        # ck_public_mcp_domains_active_lease
        params(state="active"),
        params(state="active", verified_at=now, proof_ok_at=now),
        params(state="active", serve_until=now, proof_ok_at=now),
        params(state="active", serve_until=now, verified_at=now),
        # ck_public_mcp_domains_hostname
        params(hostname="Upper.example.org"),
        params(hostname="trailing.example.org."),
        params(hostname="a.b"),
        params(hostname="a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 40 + ".io"),
        params(proof="maybe"),
        params(routing="fine"),
    ]
    for values in refused:
        _refused(engine, insert, values)


def _check_unique_indexes(engine) -> None:
    # Pending rows hold nothing: several workspaces may claim one name.
    with engine.begin() as conn:
        _insert(conn, hostname="shared.example.org")
        _insert(conn, hostname="shared.example.org")
        _insert_active(conn, hostname="shared.example.org")
    # A second holder (active or suspended) of the same name is refused.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_active(conn, hostname="shared.example.org")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public_mcp_domains (hostname, workspace_id, endpoint_id, state, "
                "suspended_reason, verification_token) VALUES ('shared.example.org', "
                "gen_random_uuid(), gen_random_uuid(), 'suspended', 'proof_lost', :t)"
            ),
            {"t": "d" * 64},
        )
    # A released row does not hold the name.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE public_mcp_domains SET state = 'released', released_at = now(), "
                "cooldown_until = now() WHERE hostname = 'shared.example.org' "
                "AND state = 'active'"
            )
        )
        _insert_active(conn, hostname="shared.example.org")

    # One open domain per publication; released rows do not count.
    endpoint_id = uuid4()
    with engine.begin() as conn:
        _insert(conn, endpoint_id=endpoint_id)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert(conn, endpoint_id=endpoint_id)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_active(conn, endpoint_id=endpoint_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE public_mcp_domains SET state = 'released', released_at = now(), "
                "cooldown_until = now() WHERE endpoint_id = :e"
            ),
            {"e": endpoint_id},
        )
        _insert(conn, endpoint_id=endpoint_id)
