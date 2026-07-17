"""Pin the production (JSON) log path's exception rendering (#1339).

Without ``format_exc_info`` ahead of the JSONRenderer, ``exc_info=True``
serializes as a bare boolean and the traceback is silently dropped — the
failure-diagnostics contract of the worker-app lifecycle arms (and every
other ``exc_info`` caller) would be void in production. The console branch
renders exceptions itself and must NOT get the processor (double-render).
"""

from unittest.mock import patch

import structlog

from utils.logger import setup_logger


def _captured_processors(enable_colors: bool, monkeypatch):
    # setup_logger's branch is ALSO gated by LOG_COLORIZE — pin it per
    # branch so the test is deterministic regardless of the environment
    # (CI commonly sets LOG_COLORIZE=false).
    monkeypatch.setenv("LOG_COLORIZE", "true" if enable_colors else "false")
    with patch.object(structlog, "configure") as configure:
        setup_logger(enable_colors=enable_colors)
    return configure.call_args.kwargs["processors"]


def test_json_path_renders_exc_info(monkeypatch):
    processors = _captured_processors(False, monkeypatch)
    assert structlog.processors.format_exc_info in processors
    # Must run BEFORE the renderer or the rendered field never materializes.
    assert processors.index(structlog.processors.format_exc_info) < len(processors) - 1
    assert isinstance(processors[-1], type(structlog.processors.JSONRenderer()))


def test_console_path_has_no_format_exc_info(monkeypatch):
    processors = _captured_processors(True, monkeypatch)
    assert structlog.processors.format_exc_info not in processors
