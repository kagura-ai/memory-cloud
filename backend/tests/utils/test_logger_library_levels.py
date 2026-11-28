"""Provider library loggers ``setup_logger()`` quiets.

voyageai logs a failed request's ``detail`` (the provider's error text) at
INFO, and at DEBUG the request body (the query and every document), on the
stdlib logger ``"voyage"``. The root logger runs at ``LOG_LEVEL`` (INFO by
default), so without a level of its own that text reaches the logs.
"""

import logging
from collections.abc import Iterator

import pytest

from tests.structlog_state import preserved_structlog_config
from utils.logger import setup_logger


@pytest.fixture
def _logging_restored() -> Iterator[None]:
    root = logging.getLogger()
    voyage = logging.getLogger("voyage")
    saved_level, saved_handlers = root.level, root.handlers[:]
    saved_formatters = [handler.formatter for handler in saved_handlers]
    saved_voyage_level = voyage.level
    with preserved_structlog_config():
        yield
    root.setLevel(saved_level)
    root.handlers[:] = saved_handlers
    for handler, formatter in zip(saved_handlers, saved_formatters, strict=True):
        handler.setFormatter(formatter)
    voyage.setLevel(saved_voyage_level)


@pytest.mark.usefixtures("_logging_restored")
@pytest.mark.parametrize("log_level", ["DEBUG", "INFO"])
def test_the_voyage_library_logs_warnings_only(log_level: str) -> None:
    logging.getLogger("voyage").setLevel(logging.NOTSET)

    setup_logger(log_level)

    voyage = logging.getLogger("voyage")
    assert not voyage.isEnabledFor(logging.INFO)
    assert not voyage.isEnabledFor(logging.DEBUG)
    assert voyage.isEnabledFor(logging.WARNING)
