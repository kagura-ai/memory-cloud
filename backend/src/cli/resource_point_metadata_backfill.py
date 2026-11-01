"""Add ``type`` / ``tags`` / ``context_summary`` to resource points indexed before v0.122.0.

Recall's ``type`` / ``tags`` filters read the point payload. The resource
indexer writes the owning row's three keys into every point since v0.122.0
(#2160); points indexed earlier carry none, so ``filters.type="resource_data"``
does not match them until this runs (#2254). Runs where the API runs (same
env), e.g. inside the API container::

    python -m src.cli.resource_point_metadata_backfill                # plan (default, read-only)
    python -m src.cli.resource_point_metadata_backfill --apply --yes  # write the payloads

Set-payload only — no vector is deleted or re-embedded. Idempotent: a second
run writes the same values again. ``--workspace <id>`` limits the run to one
workspace (a canary).

Exit codes: 0 ok · 1 error (including a point that could not be written).
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
from tasks.resource_point_metadata_backfill import (  # noqa: E402
    ResourcePointMetadataBackfill,
    backfill_resource_point_metadata,
)


def _print_plan(result: ResourcePointMetadataBackfill) -> None:
    print(f"resource-indexed memories with their own point: {result.rows}")
    print(f"contexts: {result.contexts}")


def _print_applied(result: ResourcePointMetadataBackfill) -> None:
    print(f"points written: {result.written}")
    if result.shared_skipped:
        print(f"skipped    {result.shared_skipped} point(s) shared with another live memory")
    if result.gone_skipped:
        print(f"skipped    {result.gone_skipped} memory(ies) deleted or changed during the run")
    for memory_id in result.failed:
        print(f"failed     {memory_id}  (see the log)")


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    applied: list[ResourcePointMetadataBackfill] = []

    async def run(db: AsyncSession, dry_run: bool) -> ResourcePointMetadataBackfill:
        result = await backfill_resource_point_metadata(
            db, dry_run=dry_run, workspace_id=args.workspace
        )
        if not dry_run:
            applied.append(result)
        return result

    code = await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        # The plan's candidates; after the write, the points actually written
        # (the headline must not count skipped or failed rows as updated).
        changes=lambda result: result.rows if result.dry_run else result.written,
        noun="resource point",
        verb="update",
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )
    # A partial apply is not a clean one: a wrapper checking the exit code
    # must see the rows that still need a run.
    if code == 0 and any(result.failed for result in applied):
        return 1
    return code


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
