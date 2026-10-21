"""A repeat ``setup_logger()`` must not strand loggers already cached (#2167).

``setup_logger()`` runs from ``tests/conftest.py`` and again whenever
``api.main`` is imported. With ``cache_logger_on_first_use=True`` a module
logger that logged before that second call holds the processor list that was
current then. ``structlog.testing.capture_logs()`` edits the *current* list in
place, so if the second call installed a new list, the cached logger would
write past the capture: ``tests/tasks`` run as a directory lost the
``orphan_vector_sweep_task_failed`` event that way.
"""

import json
import uuid

import pytest
import structlog

from tests.structlog_state import preserved_structlog_config
from utils.logger import setup_logger


@pytest.fixture
def _structlog_restored():
    with preserved_structlog_config():
        yield


@pytest.mark.usefixtures("_structlog_restored")
def test_capture_logs_reaches_a_logger_cached_before_a_repeat_setup(capsys):
    logger = structlog.get_logger(f"t2167-{uuid.uuid4().hex}")
    logger.info("warm")  # caches the bound logger against the current processors

    setup_logger()

    with structlog.testing.capture_logs() as logs:
        logger.info("after_reconfigure")

    assert [entry["event"] for entry in logs] == ["after_reconfigure"]


@pytest.mark.usefixtures("_structlog_restored")
def test_a_repeat_setup_keeps_the_processor_list_instance():
    before = structlog.get_config()["processors"]

    setup_logger(enable_colors=False)

    assert structlog.get_config()["processors"] is before


@pytest.mark.usefixtures("_structlog_restored")
def test_a_repeat_setup_switches_the_chain_of_cached_loggers(monkeypatch, capsys):
    # The chain follows the new call (console -> JSON here). Level and output
    # stream do not: structlog freezes those per logger on first use.
    monkeypatch.setenv("LOG_COLORIZE", "true")
    setup_logger(enable_colors=True)
    logger = structlog.get_logger(f"t2167-{uuid.uuid4().hex}")
    logger.info("warm")
    capsys.readouterr()

    setup_logger(enable_colors=False)
    logger.info("as_json", n=1)

    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line)["event"] == "as_json"


def test_preserved_structlog_config_restores_contents_in_place():
    processors = structlog.get_config()["processors"]
    original = list(processors)

    with preserved_structlog_config():
        setup_logger(enable_colors=False)
        processors.append(lambda _logger, _name, event: event)

    assert structlog.get_config()["processors"] is processors
    assert processors == original
