"""The admin CLIs record ``admitted_via='admin_cli'`` (#2803).

``create_admin`` and ``seed_e2e_admin`` create a local admin from the shell;
the row says so, with no invite details. Each runs against a private schema
of TEST_DATABASE_URL (an empty users table, so "no password admin yet" holds).

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from cli import create_admin, seed_e2e_admin
from config.database import to_sync_database_url
from db.base import Base
from models.auth import User, Workspace, WorkspaceMember

PASSWORD = "Correct-Horse-Battery-42"


def _tables() -> set[Any]:
    tables = {m.__table__ for m in (User, Workspace, WorkspaceMember)}
    while True:
        dependencies = {fk.column.table for table in tables for fk in table.foreign_keys}
        if dependencies <= tables:
            return tables
        tables.update(dependencies)


@pytest.fixture
def private_engine() -> Iterator[Engine]:
    schema = "admission_cli_" + uuid4().hex
    url = to_sync_database_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
        )
    )
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            Base.metadata.create_all(conn, tables=_tables())
    except OperationalError as exc:
        engine.dispose()
        if os.environ.get("REQUIRE_TEST_DATABASE") == "1":
            pytest.fail(f"PostgreSQL required: {type(exc).__name__}")
        pytest.skip("PostgreSQL test server unavailable")
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def _records(engine: Engine) -> list[tuple]:
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                select(
                    User.user_id,
                    User.admitted_via,
                    User.admitted_beta_invite_id,
                    User.admitted_invite_issuer,
                    User.admitted_by_user_id,
                )
            )
        ]


def test_create_admin_records_admin_cli(private_engine: Engine) -> None:
    answers = iter(["root", "n"])
    with (
        patch.object(create_admin._env_guard, "require_api_key_secret", return_value="x"),
        patch.object(create_admin._env_guard, "require_settings"),
        patch.object(create_admin, "create_engine", return_value=private_engine),
        patch.object(create_admin, "_create_api_key", return_value="kagura_test_key"),
        patch.object(create_admin, "_configure_embedding_provider", return_value=None),
        patch("builtins.input", side_effect=lambda *_: next(answers)),
        patch("getpass.getpass", return_value=PASSWORD),
        patch.object(private_engine, "dispose"),
    ):
        create_admin.create_admin(skip_mcp_json=True)

    assert _records(private_engine) == [("local:root", "admin_cli", None, None, None)]


def test_seed_e2e_admin_records_admin_cli(private_engine: Engine, monkeypatch) -> None:
    monkeypatch.setenv("E2E_ADMIN_LOGIN_ID", "e2e-admin")
    monkeypatch.setenv("E2E_ADMIN_PASSWORD", PASSWORD)
    with (
        patch.object(seed_e2e_admin, "require_settings"),
        patch.object(seed_e2e_admin, "create_engine", return_value=private_engine),
        patch.object(private_engine, "dispose"),
    ):
        seed_e2e_admin.seed_e2e_admin()

    assert _records(private_engine) == [("local:e2e-admin", "admin_cli", None, None, None)]
