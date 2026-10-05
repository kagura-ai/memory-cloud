"""Fixtures shared by the one-shot CLI tests."""

from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

import pytest
import structlog

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

_PER_REQUEST_LOGGERS = ("httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Undo what ``cli._oneshot.configure_logging`` does to process-wide state (#1788).

    Snapshot + restore (NOT ``structlog.reset_defaults``): later tests must
    find the suite-wide configuration from ``tests/conftest.py`` — library
    defaults would make ``structlog.testing.capture_logs`` order-dependent.
    Covers the structlog config, the root and HTTP-client logger levels and
    the warnings filters.
    """
    saved_structlog = structlog.get_config()
    root = logging.getLogger()
    saved_root_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in _PER_REQUEST_LOGGERS}
    with warnings.catch_warnings():
        yield
    structlog.configure(**saved_structlog)
    root.setLevel(saved_root_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture(autouse=True)
def _no_structlog_reconfigure(monkeypatch):
    """A CLI's ``_main`` must not re-point structlog while the suite runs.

    ``configure_logging`` sends structlog to the current ``sys.stderr`` —
    under pytest a capture stream that is closed when the test ends — and
    ``setup_logger`` caches loggers on first use, so a module logger first
    used inside such a test would write to a closed file for the rest of the
    session. ``test_oneshot_logging.py`` overrides this fixture to exercise
    the real configuration with throwaway loggers.
    """
    monkeypatch.setattr("cli._oneshot.setup_logger", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _no_real_session_store(monkeypatch):
    """``reset_password`` must not reach a real Redis from a unit test (#1866).

    A password reset deletes the account's browser sessions; a developer's
    local Redis is not a test fixture. Tests that assert on the store patch
    ``SessionManager`` themselves.
    """
    from unittest.mock import MagicMock

    manager = MagicMock()
    manager.delete_user_sessions.return_value = 0
    monkeypatch.setattr("cli.reset_password.SessionManager", MagicMock(return_value=manager))
