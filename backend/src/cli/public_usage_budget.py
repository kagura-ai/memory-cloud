"""Inspect and refill a workspace's public usage budget (#2805).

The workspace-wide seven-day budget of anonymous public calls
(``MCP_PUBLIC_USAGE_BUDGET_ENABLED``; contract: docs/public-usage-budget.md).
Runs where the API runs (same env: DATABASE_URL), e.g. inside the API
container::

    python -m src.cli.public_usage_budget show <workspace-id>
    python -m src.cli.public_usage_budget reset <workspace-id>                # plan (read-only)
    python -m src.cli.public_usage_budget reset <workspace-id> --apply --yes  # refill

``show`` prints the owner's view (the ``usage_budget`` field of the owner
routes) as JSON; it never writes. ``reset --apply`` sets the running period's
count to 0 and keeps the period (its start, number and reset time), and
writes the ``public_usage_budget_reset`` log line. A workspace without a
stored period is an error; nothing is created.

Exit codes: 0 ok · 1 error (no such workspace, no stored period) · 2 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from models.public_usage_budget import PublicUsageBudgetStatus  # noqa: E402
from services.public_usage_budget import (  # noqa: E402
    BudgetReset,
    read_public_usage_budget,
    reset_public_usage_budget,
)
from utils.datetime import to_utc_iso  # noqa: E402


def _print_status(status: PublicUsageBudgetStatus) -> None:
    print(status.model_dump_json(indent=2))


def _print_reset(result: BudgetReset) -> None:
    print(f"workspace       {result.workspace_id}")
    print(f"period          #{result.period_seq} since {to_utc_iso(result.period_started_at)}")
    print(f"calls used      {result.calls_used_before} -> 0")


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    workspace_id: UUID = args.workspace_id

    if args.command == "show":

        async def show(db: AsyncSession, dry_run: bool) -> PublicUsageBudgetStatus:
            from models.auth import Workspace

            if await db.get(Workspace, workspace_id) is None:
                raise LookupError(f"no workspace {workspace_id}")
            return await read_public_usage_budget(db, workspace_id)

        return await run_plan_apply(
            run=show,
            print_plan=_print_status,
            changes=lambda status: 0,
            noun="budget",
            apply=False,
            assume_yes=False,
        )

    async def reset(db: AsyncSession, dry_run: bool) -> BudgetReset:
        return await reset_public_usage_budget(db, workspace_id, dry_run=dry_run)

    return await run_plan_apply(
        run=reset,
        print_plan=_print_reset,
        changes=lambda result: 1,
        noun="budget",
        verb="refill",
        past_tense="refilled",
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="print the workspace's budget as JSON")
    show.add_argument("workspace_id", type=UUID, help="workspace id")
    add_log_level_argument(show)
    reset = commands.add_parser(
        "reset", help="refill the running period (plan by default; --apply writes)"
    )
    reset.add_argument("workspace_id", type=UUID, help="workspace id")
    mode = reset.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="show what would change (default)")
    mode.add_argument("--apply", action="store_true", help="refill the period")
    reset.add_argument("--yes", action="store_true", help="no confirmation prompt")
    add_log_level_argument(reset)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
