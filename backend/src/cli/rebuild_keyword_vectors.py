"""Rebuild the keyword (BM25) sparse vectors of one context or of every context (#2984).

Each point's ``bm25`` vector is weighted like Okapi BM25 with its context's
average field lengths. This command stores every live row's field lengths and
rewrites every point's ``bm25`` vector with the context's current averages.
It changes nothing else: no embedding call, no dense vector, no other payload
key (a summary point's four token fields are rewritten when they differ from
the row's tokenization), no row column but the five keyword lengths. Run it
after the upgrade that introduced the weighting (keyword ranking mixes old and
new weights until it finished), and for a context whose averages moved a lot.
Runs where the API runs (same env), e.g. inside the API container, from
``backend/``::

    python -m src.cli.rebuild_keyword_vectors --all                  # plan
    python -m src.cli.rebuild_keyword_vectors --all --apply --yes
    python -m src.cli.rebuild_keyword_vectors --context <uuid> --apply --yes

Contexts are taken by id. ``--limit N`` stops after N contexts; the report ends
with a ``resume`` line (``--after <context id>``, the last context finished) to
go on from. Stopping a run mid-context (Ctrl-C) is safe: a rerun of that
context redoes it, and points already rebuilt are reported ``unchanged``.

Rows another writer is busy with (locked, re-queued, being embedded) are
skipped and counted (``skipped``); their writer weights them. With the LanceDB
vector store the command prints that it has nothing to do (LanceDB's own FTS
scores with BM25).

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
from tasks.keyword_rebuild import KeywordRebuild, run_keyword_rebuild  # noqa: E402


def _print_plan(result: KeywordRebuild) -> None:
    if result.store == "lance":
        print("vector store: LanceDB, which keeps no keyword vectors; nothing to rebuild")
        return
    print(f"contexts                      {result.contexts}")
    print(f"embedded rows                 {result.rows}")
    print(f"rows without keyword lengths  {result.rows_without_lengths}")
    _print_resume(result)


def _print_applied(result: KeywordRebuild) -> None:
    print(f"contexts          {result.contexts}")
    print(f"lengths_written   {result.lengths_written}")
    print(f"tokens_rewritten  {result.tokens_rewritten}")
    print(f"vectors_written   {result.vectors_written}")
    print(f"unchanged         {result.unchanged}")
    print(f"skipped           {result.skipped}")
    print(f"missing_point     {result.missing_point}")
    print(f"seconds           {result.seconds:.1f}")
    _print_resume(result)


def _print_resume(result: KeywordRebuild) -> None:
    if result.stopped and result.last_context is not None:
        print(f"resume            --after {result.last_context}")


def _changes(result: KeywordRebuild) -> int:
    if result.store == "lance":
        return 0
    return result.rows if result.dry_run else result.written


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

    async def run(db: AsyncSession, dry_run: bool) -> KeywordRebuild:
        return await run_keyword_rebuild(
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
        noun="row",
        verb="rebuild",
        past_tense="rebuilt",
        print_applied=_print_applied,
        summary=lambda r: f"the keyword vectors of {r.contexts} context(s)",
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
    mode.add_argument("--plan", action="store_true", help="count the rows (default)")
    mode.add_argument("--apply", action="store_true", help="rebuild the vectors")
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
