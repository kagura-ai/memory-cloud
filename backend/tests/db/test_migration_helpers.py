"""Unit tests for the migration lock_timeout helpers (#2300). No database."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from db.migration_helpers import (
    DEFAULT_MIGRATION_LOCK_TIMEOUT,
    MIGRATION_LOCK_TIMEOUT_ENV,
    install_migration_lock_timeout,
    lock_timeout_statement,
    migration_lock_timeout,
    reapply_migration_lock_timeout,
)

ENV_PY = Path(__file__).resolve().parents[2] / "alembic" / "env.py"


def test_default_is_a_few_seconds() -> None:
    assert migration_lock_timeout({}) == DEFAULT_MIGRATION_LOCK_TIMEOUT == "5s"
    assert migration_lock_timeout({MIGRATION_LOCK_TIMEOUT_ENV: "  "}) == "5s"


@pytest.mark.parametrize("value", ["500", "500ms", "30s", "2min", "1h", " 10s "])
def test_a_duration_overrides_it(value: str) -> None:
    assert migration_lock_timeout({MIGRATION_LOCK_TIMEOUT_ENV: value}) == value.strip()


@pytest.mark.parametrize("value", ["0", "0s", "000ms"])
def test_zero_turns_it_off(value: str) -> None:
    assert migration_lock_timeout({MIGRATION_LOCK_TIMEOUT_ENV: value}) is None


@pytest.mark.parametrize(
    "value",
    ["5 s", "-1", "1.5s", "5sec", "off", "5s'; DROP TABLE api_keys; --", "5s;"],
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
