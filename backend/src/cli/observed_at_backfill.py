"""Add the ``observed_at`` payload to vector points written before #2330.

Recall's ``observed_after`` / ``observed_before`` filters read the point
payload. Points embedded before migration f16_2330_observed_at carry no
``observed_at``, so a memory whose ``details.observed_at`` was written earlier
does not match them until this runs. Every write since the migration carries
it already. Runs where the API runs (same env), e.g. inside the API container::

    python -m src.cli.observed_at_backfill                # plan (default, read-only)
    python -m src.cli.observed_at_backfill --apply --yes  # write the payloads

Idempotent: a second run writes the same values again. ``--workspace <id>``
limits the run to one workspace (a canary).

Exit codes: 0 ok · 1 error.
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
from tasks.observed_at_backfill import (  # noqa: E402
    ObservedAtBackfill,
    backfill_observed_at_payloads,
)


def _print_plan(result: ObservedAtBackfill) -> None:
    print(f"embedded memories with observed_at: {result.rows}")


def _print_applied(result: ObservedAtBackfill) -> None:
    print(f"points written: {result.written}")
    if result.shared_skipped:
        print(f"skipped    {result.shared_skipped} point(s) shared with another live memory")
    for memory_id in result.failed:
        print(f"failed     {memory_id}  (see the log; run again)")


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    async def run(db: AsyncSession, dry_run: bool) -> ObservedAtBackfill:
        return await backfill_observed_at_payloads(db, dry_run=dry_run, workspace_id=args.workspace)

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda result: result.rows,
        noun="memory point",
        verb="update",
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="count the rows (default)")
    mode.add_argument("--apply", action="store_true", help="write the payloads")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    parser.add_argument(
        "--workspace", type=UUID, default=None, help="limit the run to this workspace"
    )
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
