"""Structured logging with color support for Kagura Memory Cloud.

Based on structlog for structured logging with color output in Docker.
"""

import io
import logging
import os
import re
import sys

import structlog

# #1359: asyncpg composes CHECK/constraint-violation messages with a
# server-side ``DETAIL:  Failing row contains (...)`` payload — the FULL
# row, including memory content and details.location coordinates.
# SQLAlchemy's ``hide_parameters`` only suppresses bind-parameter echoes;
# it cannot touch this server-composed string. Redact from ``DETAIL:`` to
# the end of the value (DOTALL: row content may itself contain newlines,
# and anything after it — SQLAlchemy's ``[SQL: ...]`` suffix — is cheap
# to lose and safe-side to over-scrub).
_PG_DETAIL_RE = re.compile(r"DETAIL:.*", re.DOTALL)
_REDACTED = "DETAIL: [redacted]"


def _scrub_detail(value):  # noqa: ANN001, ANN202
    """Recursively scrub DETAIL payloads from strings and containers.

    Nested containers matter (#1360 review): a handler logging
    ``results=[{"error": str(exc)}]`` would otherwise carry the failing
    row straight past a top-level-only scrub — the JSON renderer
    stringifies the structure AFTER the processors have run.
    """
    if isinstance(value, str):
        if "DETAIL:" in value:
            return _PG_DETAIL_RE.sub(_REDACTED, value)
        return value
    if isinstance(value, dict):
        return {key: _scrub_detail(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_scrub_detail(item) for item in value)
    return value


def redact_pg_detail(logger, method_name, event_dict):  # noqa: ANN001, ANN201
    """structlog processor: scrub postgres DETAIL payloads (#1359).

    Runs on every event so ANY logger that stringifies a DB error (the
    global SQLAlchemyError handler's ``exc_info=True``, ad-hoc
    ``error=str(exc)`` fields, nested dict/list fields) hits one
    chokepoint. The constraint name before DETAIL survives for
    diagnosability; the failing-row payload never reaches log
    aggregation.
    """
    for key, value in event_dict.items():
        event_dict[key] = _scrub_detail(value)
    return event_dict


class _RedactingStdlibFormatter(logging.Formatter):
    """stdlib ``logging`` formatter that scrubs postgres DETAIL payloads (#1359).

    ``redact_pg_detail`` only runs inside the structlog pipeline, but ~30
    modules log via plain ``logging.getLogger(__name__)`` — including the
    secret store, the MCP transport/dispatch catch-alls, and the auth layer —
    whose asyncpg CHECK/IntegrityError text (both the interpolated message and
    the ``exc_info`` traceback) would otherwise reach stdout unredacted,
    defeating #1359 for that whole subset. Scrubbing the fully-rendered record
    catches the message and the appended traceback in one place, regardless of
    how the error reached the record (``msg``/``args``/``exc_info``).
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        rendered = super().format(record)
        if "DETAIL:" in rendered:
            return _PG_DETAIL_RE.sub(_REDACTED, rendered)
        return rendered


def _redacting_console_traceback(sio, exc_info) -> None:
    """Console exception formatter that scrubs DETAIL from tracebacks.

    The ConsoleRenderer renders exceptions itself (after all processors),
    so the dev branch needs the scrub inside the formatter — the JSON
    branch scrubs the ``format_exc_info``-rendered field instead.
    """
    buffer = io.StringIO()
    structlog.dev.plain_traceback(buffer, exc_info)
    sio.write(_PG_DETAIL_RE.sub(_REDACTED, buffer.getvalue()))


def setup_logger(log_level: str = "INFO", enable_colors: bool = True) -> None:
    """Setup structured logger with color support.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        enable_colors: Enable color output (default: True)
    """
    # Get log level from environment or parameter
    level_str = os.getenv("LOG_LEVEL", log_level).upper()
    level = getattr(logging, level_str, logging.INFO)

    # Configure stdlib logging. Route the root logger through a redacting
    # formatter so DB-error DETAIL payloads logged via plain
    # ``logging.getLogger(__name__)`` (the secret store, MCP transport, auth,
    # neural, etc.) are scrubbed just like the structlog pipeline — closing the
    # #1359 gap where basicConfig(format="%(message)s") shipped them verbatim.
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # Idempotent: replace any handler a prior setup_logger()/basicConfig()
    # left behind so repeated calls (reload, tests) neither double-log nor keep
    # a non-redacting handler.
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    stdlib_handler = logging.StreamHandler(sys.stdout)
    stdlib_handler.setFormatter(_RedactingStdlibFormatter("%(message)s"))
    root_logger.addHandler(stdlib_handler)

    # Determine if colors should be enabled
    use_colors = enable_colors and (os.getenv("LOG_COLORIZE", "true").lower() == "true")

    # Configure structlog
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=False),
    ]

    if use_colors:
        # Color output for development. The redaction processor scrubs
        # string fields; the wrapped exception formatter scrubs the
        # traceback the renderer produces itself (#1359).
        processors.append(redact_pg_detail)
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=_redacting_console_traceback,
            )
        )
    else:
        # JSON output for production. format_exc_info is load-bearing (#1339):
        # without it, exc_info=True serializes as a bare boolean and the
        # traceback is silently dropped — every exc_info caller (worker-app
        # lifecycle failure arms, the unhandled-exception handler) would emit
        # undiagnosable errors in production. The ConsoleRenderer branch above
        # renders exceptions itself and must NOT get this processor (structlog
        # docs: ConsoleRenderer + format_exc_info double-render).
        processors.append(structlog.processors.format_exc_info)
        # #1359: AFTER format_exc_info (the rendered ``exception`` field
        # must exist to be scrubbed), BEFORE the renderer.
        processors.append(redact_pg_detail)
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Get a logger instance.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)


# Example usage:
# from utils.logger import get_logger
#
# logger = get_logger(__name__)
# logger.info("server_started", port=8080, version="0.1.0")
# logger.error("database_connection_failed", error=str(e), database="postgresql")
