"""Guard: alembic/env.py must not disable the loggers that already exist (#1808).

``logging.config.fileConfig`` disables every logger that exists when it runs
unless it is told ``disable_existing_loggers=False``. The migration tests run
alembic in the pytest process, so ``env.py``'s ``fileConfig`` switched off
every module logger imported before it (``mcp_server.transport`` among them).
Each suite passed on its own; run in one process with ``tests/integration``,
about 30 log-asserting tests in ``tests/mcp_server`` failed because their
records never reached ``caplog``.
"""

from __future__ import annotations

import ast
from pathlib import Path

ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


def _file_config_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "fileConfig")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "fileConfig")
        )
    ]


def _keeps_existing_loggers(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "disable_existing_loggers":
            return isinstance(keyword.value, ast.Constant) and keyword.value.value is False
    return False


def test_env_py_file_config_keeps_existing_loggers() -> None:
    calls = _file_config_calls(ast.parse(ENV_PY.read_text(encoding="utf-8")))

    assert calls, "alembic/env.py no longer calls fileConfig; update this guard"
    assert all(_keeps_existing_loggers(call) for call in calls)


def test_the_guard_rejects_the_default() -> None:
    (call,) = _file_config_calls(ast.parse("fileConfig(config.config_file_name)"))

    assert not _keeps_existing_loggers(call)
