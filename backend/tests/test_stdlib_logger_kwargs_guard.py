"""Guard: never pass structlog-style kwargs to a stdlib-bound logger (#1445).

``logging.Logger._log()`` accepts only ``exc_info`` / ``stack_info`` /
``stacklevel`` / ``extra``. Calling a stdlib logger the structlog way —
``logger.info("event", key=value)`` — raises ``TypeError`` at call time, not at
import, so it survives until the branch actually runs.

That has shipped twice:

1. ``api/routes/auth.py`` — fixed in PR #522 (the comment at its ``get_logger``
   binding records it).
2. ``mcp_server/tools/sleep.py`` — fixed in #1440 / PR #1441, where it turned a
   benign sleep-rollback edge mismatch into a reported ``partial_rollback``
   whose recorded reason was Python internals rather than the mismatch.

Neither was caught by ruff, by pyright, or by the rest of the suite.

**Scope note.** This guard deliberately does NOT ban ``logging.getLogger`` in
``backend/src``. Around thirty modules bind it and call it only with a
positional message, which is perfectly safe; banning the binding would force a
wide migration to fix a bug that exists in none of them. The defect is the
*combination* — stdlib binding plus non-stdlib kwargs — so that is what is
checked.

Follows the precedent of ``tests/test_models_no_column_guard.py``: the checker
is a plain function so the negative cases can feed it synthetic sources, proving
the guard both catches the bug class and stays quiet on the safe patterns.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# Keywords `logging.Logger.debug/info/warning/error/...` actually accept.
# Anything else reaches Logger._log() and raises TypeError.
STDLIB_LOG_KWARGS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})

LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)


def _stdlib_bound_logger_names(tree: ast.Module) -> set[str]:
    """Names assigned from ``logging.getLogger(...)`` anywhere in the module.

    A name assigned from ``get_logger(...)`` (structlog) is explicitly removed,
    so a module that migrated its binding is not flagged by a stale alias.
    """
    stdlib: set[str] = set()
    structlog: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        # logging.getLogger(...)
        is_stdlib = (
            isinstance(func, ast.Attribute)
            and func.attr == "getLogger"
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
        )
        # get_logger(...) / utils.logger.get_logger(...)
        is_structlog = (isinstance(func, ast.Name) and func.id == "get_logger") or (
            isinstance(func, ast.Attribute) and func.attr == "get_logger"
        )
        if not (is_stdlib or is_structlog):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                (stdlib if is_stdlib else structlog).add(target.id)

    return stdlib - structlog


def find_violations(source: str, filename: str = "<source>") -> list[str]:
    """Return one message per structlog-style kwarg on a stdlib-bound logger."""
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - the tree must parse to be shipped
        return []

    logger_names = _stdlib_bound_logger_names(tree)
    if not logger_names:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in LOG_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id in logger_names
        ):
            continue
        for keyword in node.keywords:
            # ``**kwargs`` splat carries no static name — skip rather than guess.
            if keyword.arg is None or keyword.arg in STDLIB_LOG_KWARGS:
                continue
            violations.append(
                f"{filename}:{node.lineno}: {func.value.id}.{func.attr}(..., "
                f"{keyword.arg}=...) — {func.value.id} is bound from "
                f"logging.getLogger(); stdlib Logger._log() rejects "
                f"'{keyword.arg}' with TypeError. Bind via "
                f"utils.logger.get_logger() instead."
            )
    return violations


def test_no_structlog_kwargs_on_stdlib_loggers() -> None:
    """No shipped module may call a stdlib-bound logger the structlog way."""
    violations: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        violations.extend(
            find_violations(
                path.read_text(encoding="utf-8"),
                str(path.relative_to(SRC_ROOT.parent)),
            )
        )

    assert not violations, "structlog-style kwargs on a stdlib logger:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Negative cases — prove the guard catches the bug class and nothing else.
# ---------------------------------------------------------------------------


def test_catches_the_sleep_py_regression() -> None:
    """The exact shape of the #1440 bug must be reported."""
    source = """
import logging
logger = logging.getLogger(__name__)

def f(action):
    logger.warning(
        "shadow_merge_rollback_edge_mismatch",
        src_id=str(action.memory_id),
        dst_id=str(action.target_id),
    )
"""
    violations = find_violations(source, "sleep.py")
    assert len(violations) == 2, violations
    assert "src_id" in violations[0]
    assert "dst_id" in violations[1]
    assert "sleep.py:6" in violations[0]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """
import logging
logger = logging.getLogger(__name__)
logger.info("plain positional message")
""",
            id="stdlib-positional",
        ),
        pytest.param(
            """
import logging
logger = logging.getLogger(__name__)
def f(e):
    logger.error(f"failed: {e}", exc_info=True)
""",
            id="stdlib-fstring-with-exc_info",
        ),
        pytest.param(
            """
import logging
logger = logging.getLogger(__name__)
logger.warning("msg", extra={"a": 1}, stacklevel=2, stack_info=True)
""",
            id="stdlib-allowed-kwargs",
        ),
        pytest.param(
            """
from utils.logger import get_logger
logger = get_logger(__name__)
logger.info("event", user_id="u", count=3)
""",
            id="structlog-kwargs-are-fine",
        ),
        pytest.param(
            """
import logging
from utils.logger import get_logger
logger = logging.getLogger(__name__)
logger = get_logger(__name__)
logger.info("event", user_id="u")
""",
            id="rebound-to-structlog",
        ),
        pytest.param(
            """
import logging
logger = logging.getLogger(__name__)
def f(**kw):
    logger.info("event", **kw)
""",
            id="kwargs-splat-not-guessed",
        ),
    ],
)
def test_safe_patterns_are_not_flagged(source: str) -> None:
    assert find_violations(source) == []


def test_flags_non_module_level_binding() -> None:
    """A stdlib logger bound inside a function is still a stdlib logger."""
    source = """
import logging

def f():
    log = logging.getLogger("x")
    log.info("event", key=1)
"""
    assert len(find_violations(source)) == 1
