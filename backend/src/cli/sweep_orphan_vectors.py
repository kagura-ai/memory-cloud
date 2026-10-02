"""Remove orphaned points from the vector store (#1798).

A point is an orphan when no reader can reach it: a memory point whose row is
gone or was soft-deleted more than the grace period ago, or a resource point
whose context is. Context deletion left such points behind until v0.88.0, and
a failed best-effort vector delete still can. The API runs the same sweep
daily; this command is for the first run on an existing deployment, and for
looking before deleting. Runs where the API runs (same env: DATABASE_URL,
QDRANT_URL, KAGURA_VECTOR_BACKEND), e.g. inside the API container::

    python -m src.cli.sweep_orphan_vectors                 # plan (default, read-only)
    python -m src.cli.sweep_orphan_vectors --apply --yes   # delete the orphans

A point whose memory row is live is never deleted. ``--apply`` scans again and
looks every orphan up a second time before deleting it, so it can delete
fewer points than the plan listed; a second run then reports 0 orphans.

Exit codes: 0 ok · 1 error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from services.orphan_vector_sweep import (  # noqa: E402
    DEFAULT_GRACE,
    SweepResult,
    sweep_orphan_points,
)


def _print_plan(result: SweepResult) -> None:
    print(
        f"{'collection':<44} {'points':>8} {'no row':>8} {'soft-deleted':>13} {'context gone':>13}"
    )
    for stats in result.collections:
        print(
            f"{stats.collection:<44} {stats.scanned:>8} {stats.no_row:>8} "
            f"{stats.tombstoned:>13} {stats.context_deleted:>13}"
        )
        if stats.error:
            print(f"  not read, skipped: {stats.error}")
    print(
        f"{result.scanned} point(s) in {len(result.collections)} collection(s), "
        f"{result.live_embedded_memories} live embedded memories: "
        f"would delete {result.orphans}"
    )


def _print_applied(result: SweepResult) -> None:
    """What is left, next to the number it should match."""
    for stats in result.collections:
        if stats.error:
            print(f"{stats.collection}: skipped: {stats.error}")
    print(
        f"{result.remaining} point(s) left; {result.live_embedded_memories} live embedded memories"
    )


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    grace = timedelta(hours=args.grace_hours)

    async def run(db: AsyncSession, dry_run: bool) -> SweepResult:
        result = await sweep_orphan_points(db, dry_run=dry_run, grace=grace)
        if result.refused:
            # Not a result to report as "deleted 0": the operator has to run
            # it again.
            raise RuntimeError(result.refused)
        return result

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda result: result.orphans if result.dry_run else result.deleted,
        noun="orphaned point",
        verb="delete",
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )


def _non_negative_hours(value: str) -> float:
    hours = float(value)
    if hours < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return hours


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="count the orphans (default, read-only)")
    mode.add_argument("--apply", action="store_true", help="delete the orphans")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    parser.add_argument(
        "--grace-hours",
        type=_non_negative_hours,
        default=DEFAULT_GRACE.total_seconds() / 3600,
        help="leave a point alone until its memory has been soft-deleted this long (default 1)",
    )
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
