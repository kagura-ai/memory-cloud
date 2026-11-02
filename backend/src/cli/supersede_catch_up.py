"""Run the supersede detection on rows of one context that never had it (#2256).

The post-embed detection runs only inside the embedding pipeline, and a
re-embed of unchanged text runs none (#2092). Memories embedded before the
detection existed, before the judge was on, or while it was capped stay
without a ``supersede_candidate`` until their text changes. This command
runs the same detection for them, in process, from each row's stored vector
(no embedding call, no seed edge, no point write). Runs where the API runs
(same env), e.g. inside the API container, from ``backend/``::

    python -m src.cli.supersede_catch_up --context <uuid>               # plan
    python -m src.cli.supersede_catch_up --context <uuid> --apply --yes

Rows: embedded (``success``), live, not a resource row, no candidate (with
``--include-unverified`` also a candidate without a judge verdict), oldest
first, at most ``--limit`` (200). ``--before`` (ISO-8601, UTC when no offset)
only takes rows created before that instant.

Judge spend: with ``SUPERSEDE_VERIFY_MODE=llm`` each row may cost one judge
call, counted on the same per-workspace daily cap as live ingest
(``SUPERSEDE_VERIFY_MAX_CALLS_PER_DAY``). The run stops once that counter
advanced by ``--max-judge-calls`` (50); run it early in the UTC day. Past the
daily cap a pair at 0.85 or above is stored unverified and a pair below is
skipped, as on ingest. ``judge_calls`` in the report is how far the counter
advanced during the run (live ingest in the same workspace included), or
``unknown`` when Redis could not be read (the budget is then not enforced;
the judge itself skips its call while the counter is unreachable).

A row whose nearest neighbour is not near enough stays without a candidate
and is planned again on the next run; narrow a repeated run with
``--before`` / ``--limit``.

Exit codes: 0 ok · 1 error · 2 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from tasks.supersede_catch_up import SupersedeCatchUp, run_supersede_catch_up  # noqa: E402


def _print_plan(result: SupersedeCatchUp) -> None:
    print(f"rows without a supersede check: {result.eligible}")
    print(f"rows this run checks: {result.planned}")


def _print_applied(result: SupersedeCatchUp) -> None:
    judge_calls = "unknown" if result.judge_calls is None else str(result.judge_calls)
    print(f"processed    {result.processed}")
    print(f"detected     {result.detected}")
    print(f"updated      {result.updated}")
    print(f"cleared      {result.cleared}")
    print(f"unchanged    {result.unchanged}")
    print(f"no_vector    {result.no_vector}")
    print(f"skipped      {result.skipped}")
    print(f"judge_calls  {judge_calls}")
    print(f"stopped      {result.stopped or '-'}")


def _changes(result: SupersedeCatchUp) -> int:
    return result.planned if result.dry_run else result.processed


def _utc_instant(value: str) -> datetime:
    """``--before``: ISO-8601; no offset means UTC. Returned as naive UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    async def run(db: AsyncSession, dry_run: bool) -> SupersedeCatchUp:
        return await run_supersede_catch_up(
            db,
            args.context,
            dry_run=dry_run,
            before=args.before,
            include_unverified=args.include_unverified,
            limit=args.limit,
            max_judge_calls=args.max_judge_calls,
        )

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=_changes,
        noun="memory",
        verb="examine",  # run_plan_apply appends "d" for the report
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--context", type=UUID, required=True, help="the context to walk")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="count the rows (default)")
    mode.add_argument("--apply", action="store_true", help="run the detection")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    parser.add_argument(
        "--before",
        type=_utc_instant,
        default=None,
        help="only rows created before this ISO-8601 instant (UTC when no offset)",
    )
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="also re-check rows whose candidate has no judge verdict",
    )
    parser.add_argument(
        "--limit", type=_positive, default=200, help="at most this many rows (default 200)"
    )
    parser.add_argument(
        "--max-judge-calls",
        type=_positive,
        default=50,
        help="stop once the workspace's judge counter advanced this much (default 50)",
    )
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
