"""Bring resource points written before v0.181.0 under the slug-holder rule.

A resource document's point belongs to the memory of the context that holds
the resource's slug. Memories a context kept after it was unpublished or
changed its ``resource_id`` become ordinary memories with a point of their
own; this command applies that to what was written before the rule (the
indexer and every point writer keep to it since). It counts and repairs:

* ``released_rows``: memories whose context no longer holds the slug of the
  point they name; each gets a point of its own (copied when the shared point
  still holds its context's document, else queued for the embedding pipeline);
* ``shared_points``: memories of the holder whose point a memory of another
  context names too; the other memories get points of their own;
* ``foreign_points``: memories of the holder whose point carries another
  context; the point is rebuilt from the holder's memory (one embedding
  request per document).

Runs where the API runs (same env), e.g. inside the API container, from
``backend/``::

    python -m src.cli.repair_resource_point_holders --all                  # plan
    python -m src.cli.repair_resource_point_holders --all --apply --yes
    python -m src.cli.repair_resource_point_holders --context <uuid> --apply --yes

The plan reads Postgres and the vector store and prints counts only. Contexts
are taken by id; ``--limit N`` stops after N contexts and the report ends with
a ``resume`` line (``--after <context id>``). A memory another transaction
holds is skipped (``skipped``) and taken by the next run; a run can be stopped
and run again, and a run over a repaired deployment finds nothing.
``missing_points`` (a holder memory whose point is gone) are counted, not
repaired.

Exit codes: 0 ok · 1 error · 2 usage.
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
from tasks.resource_point_holders import HolderRepair, run_holder_repair  # noqa: E402


def _print_plan(result: HolderRepair) -> None:
    print(f"contexts        {result.contexts}")
    print(f"released_rows   {result.released_rows}")
    print(f"shared_points   {result.shared_points}")
    print(f"foreign_points  {result.foreign_points}")
    print(f"missing_points  {result.missing_points}")
    _print_resume(result)


def _print_applied(result: HolderRepair) -> None:
    print(f"contexts        {result.contexts}")
    print(f"detached        {result.detached}")
    print(f"rebuilt         {result.rebuilt}")
    print(f"skipped         {result.skipped}")
    print(f"failed          {result.failed}")
    print(f"missing_points  {result.missing_points}")
    print(f"seconds         {result.seconds:.1f}")
    _print_resume(result)


def _print_resume(result: HolderRepair) -> None:
    if result.stopped and result.last_context is not None:
        print(f"resume          --after {result.last_context}")


def _changes(result: HolderRepair) -> int:
    return result.found if result.dry_run else result.repaired


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    async def run(db: AsyncSession, dry_run: bool) -> HolderRepair:
        return await run_holder_repair(
            db,
            context_id=args.context,
            after=args.after,
            limit=args.limit,
            dry_run=dry_run,
            progress=None if dry_run else _progress,
        )

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=_changes,
        noun="memory",
        verb="repair",
        past_tense="repaired",
        print_applied=_print_applied,
        summary=lambda r: f"the resource points of {r.contexts} context(s)",
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--context", type=UUID, help="one context")
    target.add_argument("--all", action="store_true", help="every live context, by id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="count (default)")
    mode.add_argument("--apply", action="store_true", help="repair")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    parser.add_argument(
        "--after",
        type=UUID,
        default=None,
        help="with --all: only contexts with an id above this (the resume line of a run)",
    )
    parser.add_argument(
        "--limit", type=_positive, default=None, help="with --all: at most this many contexts"
    )
    add_log_level_argument(parser)
    args = parser.parse_args(argv)
    if args.context is not None and (args.after is not None or args.limit is not None):
        parser.error("--after and --limit go with --all")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
