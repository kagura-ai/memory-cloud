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
first, at most ``--limit`` (200). ``--before <ISO-8601>`` (UTC when no
offset) only takes rows created before that instant. ``--after`` resumes past
a cursor ``<ISO-8601>,<memory id>`` (the ``resume`` line of a run, in the
rows' order); a bare instant includes rows created at that instant.

Judge spend: with ``SUPERSEDE_VERIFY_MODE=llm`` each row may cost one judge
call, counted on the workspace's bulk-lane daily cap
(``SUPERSEDE_VERIFY_MAX_BULK_CALLS_PER_DAY``, default 500), a counter apart
from live ingest's ``SUPERSEDE_VERIFY_MAX_CALLS_PER_DAY`` (#2510). The run
stops once that counter advanced by ``--max-judge-calls`` (50); run it early
in the UTC day. Past the bulk cap a pair at 0.85 or above is stored unverified
and a pair below is skipped, as on ingest. ``judge_calls`` in the report is
how far the bulk counter advanced during the run (another catch-up run in the
same workspace included, live ingest not), or ``unknown`` when Redis could
not be read. With the judge on, a run that cannot read the counter stops
(``stopped counter_unreadable``); with the judge off it goes on.

A row whose nearest neighbour is not near enough stays without a candidate
and is planned again by a run over the same range: the report ends with a
``resume`` line (``--after <created_at>,<id>`` of the last row reached) to walk on
past it.

Exit codes: 0 ok · 1 error · 2 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from tasks.supersede_catch_up import SupersedeCatchUp, run_supersede_catch_up  # noqa: E402
from utils.datetime import to_naive_utc, to_utc_iso  # noqa: E402


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
    print(f"failed       {result.failed}")
    print(f"no_vector    {result.no_vector}")
    print(f"skipped      {result.skipped}")
    print(f"judge_calls  {judge_calls}")
    print(f"stopped      {result.stopped or '-'}")
    if result.last_created_at is not None:
        cursor = to_utc_iso(result.last_created_at)
        if result.last_id is not None:
            cursor = f"{cursor},{result.last_id}"
        print(f"resume       --after {cursor}")


def _changes(result: SupersedeCatchUp) -> int:
    return result.planned if result.dry_run else result.processed


def _cursor(value: str) -> tuple[datetime, UUID | None]:
    """``--after``: ``<ISO-8601>[,<memory id>]``, the resume line of a run."""
    instant, _, memory_id = value.partition(",")
    try:
        return _utc_instant(instant), UUID(memory_id) if memory_id else None
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a memory id: {memory_id!r}") from exc


def _utc_instant(value: str) -> datetime:
    """ISO-8601; no offset means UTC. Returned as naive UTC."""
    try:
        return to_naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {value!r}") from exc


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
            after=args.after[0] if args.after else None,
            after_id=args.after[1] if args.after else None,
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
        "--after",
        type=_cursor,
        default=None,
        help="resume past <ISO-8601>[,<memory id>] (the resume line of a run)",
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
        help="stop once the workspace's bulk judge counter advanced this much (default 50)",
    )
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
