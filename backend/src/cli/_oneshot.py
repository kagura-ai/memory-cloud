"""Shared scaffold for one-shot, plan-then-apply operator commands.

``apply_rerank_defaults`` (#1572) and ``transfer_context_creator`` (#1783)
share the same shape: plan (read-only, printed), confirm, apply, report.
Keeping the driver here means a fix to the confirmation or the session
handling lands in every command at once.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db

R = TypeVar("R")


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
