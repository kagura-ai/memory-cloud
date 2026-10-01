"""Shared scaffold for one-shot, plan-then-apply operator commands.

``apply_rerank_defaults`` (#1572) and ``transfer_context_creator`` (#1783)
share the same shape: plan (read-only, printed), confirm, apply, report.
Keeping the driver here means a fix to the confirmation or the session
handling lands in every command at once.
"""

from __future__ import annotations

import argparse
import re
import sys
import warnings
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_qdrant_url
from config.settings import get_settings
from db.base import get_db
from utils.logger import setup_logger

R = TypeVar("R")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# qdrant-client's wording (qdrant_remote.py); it fires when an api_key meets
# a plain-http URL, which is the normal shape on a private network.
INSECURE_QDRANT_WARNING = "Api key is used with an insecure connection."


def add_log_level_argument(parser: argparse.ArgumentParser) -> None:
    """``--log-level`` for a one-shot command (default INFO, see configure_logging)."""
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="INFO",
        help="diagnostics go to stderr at this level (default INFO; DEBUG also prints "
        "one vector-store line per memory)",
    )


def configure_logging(level: str = "INFO") -> None:
    """Configure logging for a one-shot command the way ``api/main.py`` does (#1788).

    Without this a CLI process leaves structlog unconfigured — every level,
    debug included, rendered to stdout — so ``transfer_context_creator
    --apply`` printed one ``memory_payload_updated_in_qdrant`` line per
    memory on top of its report. Diagnostics go to stderr so stdout stays
    the plan report; the explicit level wins over ``LOG_LEVEL`` in the
    environment, which is the API's setting, not the operator's. Call it
    before the first log line (``setup_logger`` caches loggers on first use).

    Args:
        level: One of ``LOG_LEVELS``.
    """
    setup_logger(level, stream=sys.stderr, level_from_env=False)
    silence_insecure_qdrant_warning(get_qdrant_url(), get_settings().qdrant_api_key)


def silence_insecure_qdrant_warning(url: str, api_key: str) -> None:
    """Drop qdrant-client's insecure-connection warning when plain http is the setup.

    Only that one message, and only when it would fire: an api_key on an
    ``http://`` URL. On ``https`` nothing changes, so a deployment that lost
    TLS by mistake still gets the warning.

    Args:
        url: The configured Qdrant URL.
        api_key: The configured Qdrant API key ("" when unset).
    """
    if api_key and url.startswith("http://"):
        warnings.filterwarnings(
            "ignore", message=re.escape(INSECURE_QDRANT_WARNING), category=UserWarning
        )


def confirm(prompt: str, assume_yes: bool) -> bool:
    """``y``/``yes`` at the prompt, or ``assume_yes`` from ``--yes``."""
    if assume_yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def run_plan_apply(
    *,
    run: Callable[[AsyncSession, bool], Awaitable[R]],
    print_plan: Callable[[R], None],
    changes: Callable[[R], int],
    noun: str,
    apply: bool,
    assume_yes: bool,
) -> int:
    """Plan, print, confirm, apply — the body of every one-shot command's main.

    Args:
        run: ``run(db, dry_run)`` — the command's worker; the same callable
            serves the plan (``dry_run=True``) and the write (``False``).
        print_plan: Renders a result to stdout.
        changes: How many rows the result would change / changed.
        noun: What is being changed, for the prompt ("context").
        apply: ``--apply`` was given.
        assume_yes: ``--yes`` was given.

    Returns:
        Process exit code: 0 ok, 1 error.
    """
    try:
        async for db in get_db():
            plan = await run(db, True)
            print_plan(plan)
            if not apply:
                if changes(plan):
                    print("dry run — pass --apply to write")
                return 0
            if not changes(plan):
                return 0
            if not confirm(f"Change {changes(plan)} {noun}(s)?", assume_yes):
                print("  skipped")
                return 0
            applied = await run(db, False)
            print(f"changed {changes(applied)} {noun}(s)")
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
