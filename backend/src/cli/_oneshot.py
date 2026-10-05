"""Shared scaffold for one-shot, plan-then-apply operator commands.

``apply_rerank_defaults`` (#1572), ``transfer_context_creator`` (#1783),
``sweep_orphan_vectors`` (#1798) and ``restore_context`` (#1804) share the same
shape: plan (read-only, printed), confirm, apply, report.
Keeping the driver here means a fix to the confirmation or the session
handling lands in every command at once.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import warnings
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from utils.logger import setup_logger

R = TypeVar("R")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# qdrant-client's wording (qdrant_remote.py). It fires when an api_key meets
# a plain-http URL — the normal shape on a private network, and also what a
# deployment that lost TLS by mistake looks like, so it is kept, once.
INSECURE_QDRANT_WARNING = "Api key is used with an insecure connection."
# Log one line per HTTP request, i.e. one per memory on a payload sweep.
_PER_REQUEST_LOGGERS = ("httpx", "httpcore")


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
    the plan report (colored only on a terminal), and the level given here
    wins over ``LOG_LEVEL`` in the environment, which is the API's setting,
    not the operator's. Below DEBUG the HTTP client's per-request lines are
    held at WARNING — they are per-memory too — and qdrant-client's
    insecure-connection warning is shown once per run instead of repeating.
    Call it before the first log line (``setup_logger`` caches loggers on
    first use).

    Args:
        level: One of ``LOG_LEVELS``.
    """
    setup_logger(level, enable_colors=sys.stderr.isatty(), stream=sys.stderr)
    # An explicit level on a child logger is not re-filtered by the root's, so
    # never set it below the level asked for (--log-level ERROR).
    per_request = (
        logging.NOTSET
        if level == "DEBUG"
        else max(logging.WARNING, logging.getLevelNamesMapping()[level])
    )
    for name in _PER_REQUEST_LOGGERS:
        logging.getLogger(name).setLevel(per_request)
    warnings.filterwarnings(
        "once", message=re.escape(INSECURE_QDRANT_WARNING), category=UserWarning
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
    verb: str = "change",
    print_applied: Callable[[R], None] | None = None,
    summary: Callable[[R], str] | None = None,
) -> int:
    """Plan, print, confirm, apply — the body of every one-shot command's main.

    Args:
        run: ``run(db, dry_run)`` — the command's worker; the same callable
            serves the plan (``dry_run=True``) and the write (``False``).
        print_plan: Renders a result to stdout.
        changes: How many rows the result would change / changed.
        noun: What is being changed, for the prompt ("context").
        verb: What happens to it, for the prompt and the report ("change",
            "delete").
        print_applied: Renders the applied result after the one-line report,
            for a command whose outcome is more than a count.
        summary: Names what the result changes, for a command that changes
            more than one kind of row ("2 context(s), 5 memory row(s)"). It
            replaces "<changes> <noun>(s)" in the prompt and the report;
            ``changes`` still decides whether there is anything to do.
        apply: ``--apply`` was given.
        assume_yes: ``--yes`` was given.

    Returns:
        Process exit code: 0 ok, 1 error.
    """

    def describe(result: R) -> str:
        if summary is not None:
            return summary(result)
        return f"{changes(result)} {noun}(s)"

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
            if not confirm(f"{verb.capitalize()} {describe(plan)}?", assume_yes):
                print("  skipped")
                return 0
            applied = await run(db, False)
            print(f"{verb}d {describe(applied)}")
            if print_applied is not None:
                print_applied(applied)
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
