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


def test_the_test_process_is_already_configured():
    """``tests/conftest.py`` ran ``setup_logger()``, so importing ``api.main`` is a no-op."""
    assert logger_mod._configured is True


def test_api_main_configures_once_at_import_before_it_logs():
    """Module level of ``api.main``: ``ensure_logger_configured()`` before the first log call."""
    tree = ast.parse(_MAIN.read_text())
    calls = [
        ast.unparse(node.value.func)
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    first_log = next(i for i, name in enumerate(calls) if name.startswith("logger."))

    assert "setup_logger" not in calls
    assert calls.index("ensure_logger_configured") < first_log
