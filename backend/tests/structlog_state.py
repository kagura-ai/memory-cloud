"""Snapshot and restore the process-wide structlog configuration (#2167).

``structlog.get_config()["processors"]`` is the live list, not a copy, and
``setup_logger()`` edits that list in place so loggers cached on first use
keep following it. A snapshot that only holds the dict would "restore" the
contents the test wrote. Copy the contents, then put them back into the
same list object, so loggers cached before the test still see the chain
``structlog.testing.capture_logs()`` edits.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import structlog


@contextmanager
def preserved_structlog_config() -> Iterator[None]:
    saved = structlog.get_config()
    processors = saved["processors"]
    saved_processors = list(processors)
    try:
        yield
    finally:
        processors[:] = saved_processors
        structlog.configure(**saved)
