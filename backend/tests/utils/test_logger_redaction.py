"""Pin the postgres DETAIL redaction chokepoint (#1359).

asyncpg puts the FULL failing row (content, details.location coordinates)
into the server-side ``DETAIL:`` string of CHECK-violation errors.
SQLAlchemy's ``hide_parameters`` only suppresses bind-parameter logging —
it does nothing to the server-composed DETAIL text, so any
``exc_info=True`` log call would ship precise coordinates to log
aggregation. The redaction lives in the structlog pipeline (single
chokepoint for every logger) on BOTH render branches.
"""

import uuid

import pytest
import structlog

from utils.logger import redact_pg_detail, setup_logger


@pytest.fixture
def _structlog_reset():
    yield
    structlog.reset_defaults()


def _fresh_logger():
    # cache_logger_on_first_use caches per proxy — a unique name guarantees
    # the logger picks up the configuration under test.
    return structlog.get_logger(f"redaction-test-{uuid.uuid4().hex}")


_ASYNCPG_STYLE_MESSAGE = (
    'new row for relation "memories" violates check constraint '
    '"valid_location_range"\n'
    "DETAIL:  Failing row contains (mem-1, secret content, "
    '{"location": {"lat": 35.6812, "lon": 139.7671}}).'
)


def test_redact_pg_detail_scrubs_string_values():
    event_dict = {
        "event": "database_error",
        "error": _ASYNCPG_STYLE_MESSAGE,
        "count": 3,
    }
    out = redact_pg_detail(None, "error", event_dict)
    assert "35.6812" not in out["error"]
    assert "secret content" not in out["error"]
    assert "DETAIL: [redacted]" in out["error"]
    assert "valid_location_range" in out["error"]  # constraint name survives
    assert out["count"] == 3


def test_redact_pg_detail_leaves_clean_values_alone():
    event_dict = {"event": "ok", "message": "no detail here"}
    assert redact_pg_detail(None, "info", event_dict) == event_dict


def test_json_pipeline_scrubs_detail_from_rendered_exception(monkeypatch, capsys, _structlog_reset):
    monkeypatch.setenv("LOG_COLORIZE", "false")
    setup_logger(enable_colors=False)
    logger = _fresh_logger()
    try:
        raise RuntimeError(_ASYNCPG_STYLE_MESSAGE)
    except RuntimeError:
        logger.error("database_error", exc_info=True)
    out = capsys.readouterr().out
    assert "35.6812" not in out
    assert "secret content" not in out
    assert "DETAIL: [redacted]" in out


def test_console_pipeline_scrubs_detail_from_rendered_exception(
    monkeypatch, capsys, _structlog_reset
):
    monkeypatch.setenv("LOG_COLORIZE", "true")
    setup_logger(enable_colors=True)
    logger = _fresh_logger()
    try:
        raise RuntimeError(_ASYNCPG_STYLE_MESSAGE)
    except RuntimeError:
        logger.error("database_error", exc_info=True)
    out = capsys.readouterr().out
    assert "35.6812" not in out
    assert "secret content" not in out
    assert "[redacted]" in out


def test_json_path_orders_redaction_after_format_exc_info(monkeypatch):
    """The rendered ``exception`` field only exists after format_exc_info —
    redaction must run between it and the renderer."""
    from unittest.mock import patch

    monkeypatch.setenv("LOG_COLORIZE", "false")
    with patch.object(structlog, "configure") as configure:
        setup_logger(enable_colors=False)
    processors = configure.call_args.kwargs["processors"]
    assert redact_pg_detail in processors
    assert processors.index(redact_pg_detail) > processors.index(
        structlog.processors.format_exc_info
    )
    assert processors.index(redact_pg_detail) < len(processors) - 1
