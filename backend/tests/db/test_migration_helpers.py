"""Unit tests for the migration helpers (#2300, #2240). No database."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from alembic import op
from db.migration_helpers import (
    DEFAULT_MIGRATION_LOCK_TIMEOUT,
    MIGRATION_LOCK_TIMEOUT_ENV,
    index_is_invalid,
    install_migration_lock_timeout,
    lock_timeout_statement,
    migration_lock_timeout,
    reapply_migration_lock_timeout,
    session_lock_timeout,
    validate_constraint,
)
from tests.migration_ast import offline_sql

ENV_PY = Path(__file__).resolve().parents[2] / "alembic" / "env.py"


def test_default_is_a_few_seconds() -> None:
    assert migration_lock_timeout({}) == DEFAULT_MIGRATION_LOCK_TIMEOUT == "5s"
    assert migration_lock_timeout({MIGRATION_LOCK_TIMEOUT_ENV: "  "}) == "5s"


@pytest.mark.parametrize("value", ["500", "500ms", "30s", "2min", "1h", "24d", " 10s "])
def test_a_duration_overrides_it(value: str) -> None:
    assert migration_lock_timeout({MIGRATION_LOCK_TIMEOUT_ENV: value}) == value.strip()


@pytest.mark.parametrize("value", ["0", "0s", "000ms"])
def test_zero_turns_it_off(value: str) -> None:
    assert migration_lock_timeout({MIGRATION_LOCK_TIMEOUT_ENV: value}) is None


@pytest.mark.parametrize(
    "value",
    [
        "5 s",
        "-1",
        "1.5s",
        "5sec",
        "500us",
        "off",
        "5s'; DROP TABLE api_keys; --",
        "5s;",
        # Non-ASCII digits, and values past PostgreSQL's int milliseconds.
        "\uff15s",
        "25d",
        "2147483648",
    ],
)
def test_anything_else_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match=MIGRATION_LOCK_TIMEOUT_ENV):
        migration_lock_timeout({MIGRATION_LOCK_TIMEOUT_ENV: value})


def test_statement_is_transaction_local_by_default() -> None:
    assert lock_timeout_statement("5s") == "SET LOCAL lock_timeout = '5s'"
    assert lock_timeout_statement("5s", local=False) == "SET lock_timeout = '5s'"


def test_statement_refuses_a_non_duration() -> None:
    with pytest.raises(ValueError):
        lock_timeout_statement("5s'; SELECT 1; --")


class _NoConnection:
    """Fails on any use: turned off means nothing touches the connection."""

    def __getattr__(self, name: str):
        raise AssertionError(f"connection.{name} used while turned off")


def test_turned_off_installs_nothing() -> None:
    install_migration_lock_timeout(_NoConnection(), None)  # type: ignore[arg-type]
    reapply_migration_lock_timeout(None)(ctx=_NoConnection(), step=None, heads=set(), run_args={})


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_env_py_installs_the_lock_timeout_on_the_online_connection() -> None:
    tree = ast.parse(ENV_PY.read_text(encoding="utf-8"))

    assert _calls(tree, "migration_lock_timeout")
    assert _calls(tree, "install_migration_lock_timeout")
    (online,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "do_run_migrations"
    ]
    configure = [
        node
        for node in ast.walk(online)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "configure"
    ]
    assert configure, "do_run_migrations no longer calls context.configure"
    callbacks = [
        kw.value for call in configure for kw in call.keywords if kw.arg == "on_version_apply"
    ]
    assert len(callbacks) == 1
    assert isinstance(callbacks[0], ast.Call)
    assert isinstance(callbacks[0].func, ast.Name)
    assert callbacks[0].func.id == "reapply_migration_lock_timeout"


# --- validate_constraint / session_lock_timeout / index_is_invalid (#2240) ---


class _Rows:
    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row


class _ProbeBind:
    """Records the probe's statement and parameters; answers with ``row``."""

    def __init__(self, row: object) -> None:
        self.row = row
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params=None, **_kwargs) -> _Rows:
        self.calls.append((str(statement), dict(params or {})))
        return _Rows(self.row)


def _offline(fn, bind: object | None = None) -> list[str]:
    return offline_sql(fn, None if bind is None else (lambda _buf: bind))


@pytest.mark.parametrize("env", ["5s", "30s", "0", None])
def test_validate_constraint_runs_in_a_transaction_of_its_own(monkeypatch, env) -> None:
    """Commit, VALIDATE, commit: the cap is the transaction's own ``SET LOCAL``
    (env.py), so the helper sets no session state whatever the override."""
    if env is None:
        monkeypatch.delenv(MIGRATION_LOCK_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(MIGRATION_LOCK_TIMEOUT_ENV, env)

    lines = _offline(lambda: validate_constraint("memories", "ck_memories_x"))

    assert lines == [
        "COMMIT;",
        "BEGIN;",
        "ALTER TABLE memories VALIDATE CONSTRAINT ck_memories_x;",
        "COMMIT;",
        "BEGIN;",
    ]


@pytest.mark.parametrize(
    ("table", "name"),
    [
        ("memories; DROP TABLE api_keys", "ck"),
        ("memories", "ck x"),
        ("Memories", "ck"),
        ('"memories"', "ck"),
        ("memories", "c" * 64),
        ("", "ck"),
    ],
)
def test_validate_constraint_refuses_a_non_identifier(table: str, name: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        _offline(lambda: validate_constraint(table, name))


def test_session_lock_timeout_resets_when_the_body_raises() -> None:
    def _body() -> None:
        with session_lock_timeout("750ms"):
            op.execute("UPDATE t SET a = 1")
            raise RuntimeError("batch failed")

    buf: list[str] = []

    def _run() -> None:
        try:
            _body()
        except RuntimeError:
            buf.append("raised")

    lines = _offline(_run)

    assert buf == ["raised"]
    assert lines == ["SET lock_timeout = '750ms';", "UPDATE t SET a = 1;", "RESET lock_timeout;"]


def test_session_lock_timeout_off_sends_nothing() -> None:
    def _run() -> None:
        with session_lock_timeout(None):
            op.execute("UPDATE t SET a = 1")

    assert _offline(_run) == ["UPDATE t SET a = 1;"]


def test_session_lock_timeout_refuses_a_non_duration() -> None:
    def _run() -> None:
        with session_lock_timeout("5s'; SELECT 1; --"):
            pass

    with pytest.raises(ValueError):
        _offline(_run)


@pytest.mark.parametrize(("row", "expected"), [((1,), True), (None, False)])
def test_index_is_invalid_reads_pg_index(row: object, expected: bool) -> None:
    bind = _ProbeBind(row)
    seen: list[bool] = []

    _offline(lambda: seen.append(index_is_invalid("ix_t_a")), bind=bind)

    assert seen == [expected]
    ((statement, params),) = bind.calls
    assert "indisvalid IS FALSE" in statement
    assert "pg_table_is_visible(c.oid)" in statement
    assert "c.relname = :name" in statement
    assert params == {"name": "ix_t_a"}
