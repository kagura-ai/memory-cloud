"""``api.main`` configures logging once per process, not once per import (#2241).

Importing ``api.main`` used to call ``setup_logger()`` unconditionally, so a
pytest process (``tests/conftest.py`` configures first) or any host that set
up logging itself had it replaced. ``ensure_logger_configured()`` runs
``setup_logger()`` only when nothing configured logging yet; a uvicorn worker
still configures at import, before the first log line of building the app.
"""

import ast
from pathlib import Path

import pytest

import utils.logger as logger_mod

_MAIN = Path(__file__).resolve().parents[2] / "src" / "api" / "main.py"


@pytest.fixture
def setup_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(logger_mod, "setup_logger", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_runs_setup_with_defaults_when_nothing_configured(monkeypatch, setup_calls):
    monkeypatch.setattr(logger_mod, "_configured", False)

    logger_mod.ensure_logger_configured()

    assert setup_calls == [((), {})]


def test_keeps_an_existing_configuration(monkeypatch, setup_calls):
    monkeypatch.setattr(logger_mod, "_configured", True)

    logger_mod.ensure_logger_configured()

    assert setup_calls == []


def test_wraps_uvicorn_handlers_installed_after_the_first_setup(monkeypatch, setup_calls):
    """uvicorn started inside an already configured process still gets redaction (#1581)."""
    import logging

    monkeypatch.setattr(logger_mod, "_configured", True)
    access = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler()
    inner = logging.Formatter("%(message)s")
    handler.setFormatter(inner)
    access.addHandler(handler)
    try:
        logger_mod.ensure_logger_configured()
        logger_mod.ensure_logger_configured()  # idempotent: no double wrap
    finally:
        access.removeHandler(handler)

    assert setup_calls == []
    assert isinstance(handler.formatter, logger_mod._RedactingStdlibFormatter)
    assert handler.formatter._inner is inner


def test_the_test_process_is_already_configured():
    """``tests/conftest.py`` ran ``setup_logger()``, so importing ``api.main`` is a no-op."""
    assert logger_mod._configured is True


def test_api_main_configures_once_at_import_before_it_logs():
    """Module level of ``api.main``: configure before any app import or log call.

    Importing the middleware and route modules logs (and so does building
    the app), so ``ensure_logger_configured()`` must come before the first
    ``api.*`` / ``mcp_server.*`` import as well as the first ``logger.*`` call.
    """
    tree = ast.parse(_MAIN.read_text())
    calls = [
        (node.lineno, ast.unparse(node.value.func))
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    app_imports = [
        node.lineno
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".")[0] in {"api", "mcp_server"}
    ]
    names = [name for _, name in calls]
    configure_line = next(line for line, name in calls if name == "ensure_logger_configured")
    first_log = min(line for line, name in calls if name.startswith("logger."))

    assert "setup_logger" not in names
    assert names.count("ensure_logger_configured") == 1
    assert app_imports and configure_line < min(app_imports)
    assert configure_line < first_log
