"""Structured logging with color support for Kagura Memory Cloud.

Based on structlog for structured logging with color output in Docker.
"""

import logging
import os
import sys

import structlog


def setup_logger(log_level: str = "INFO", enable_colors: bool = True) -> None:
    """Setup structured logger with color support.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        enable_colors: Enable color output (default: True)
    """
    # Get log level from environment or parameter
    level_str = os.getenv("LOG_LEVEL", log_level).upper()
    level = getattr(logging, level_str, logging.INFO)

    # Configure stdlib logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

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
        # Color output for development
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
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
