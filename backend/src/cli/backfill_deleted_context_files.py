"""Soft-delete the files left live in soft-deleted contexts (#1966).

Before #1960 a context deletion left its files live: hidden from every
listing but still counted by the upload quota, while the capacity lock and
the downgrade eligibility (#1961) do not count them. This backfill gives
those files the deletion their context had, releasing their bytes from
``workspace_storage_usage`` and the Redis counter. Runs where the API runs
(same env: DATABASE_URL, REDIS_URL), e.g. inside the API container::

    python -m src.cli.backfill_deleted_context_files                # plan (default, read-only)
    python -m src.cli.backfill_deleted_context_files --apply --yes  # soft-delete and release

One context per transaction; a second run finds nothing. The files come
back with ``restore_context`` inside the file restore window, as for any
context deleted since #1960.

Exit codes: 0 ok · 1 error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from services.storage_backfill import (  # noqa: E402
    DeletedContextFilesBackfill,
    backfill_deleted_context_files,
)
from utils.datetime import to_utc_iso  # noqa: E402


def _print_plan(result: DeletedContextFilesBackfill) -> None:
    if not result.contexts:
        print("no live files in deleted contexts")
        return
    print(f"{'context':36}  {'workspace':36}  {'deleted at':20}  files  uploaded bytes")
    for c in result.contexts:
        print(
            f"{str(c.context_id):36}  {str(c.workspace_id):36}  "
            f"{to_utc_iso(c.deleted_at):20}  {c.files:5}  {c.uploaded_bytes}"
        )
    print(
        f"total      {len(result.contexts)} context(s) in {len(result.workspaces)} workspace(s), "
        f"{result.files} file(s), {result.uploaded_bytes} uploaded bytes"
    )


def _print_applied(result: DeletedContextFilesBackfill) -> None:
    print(f"files soft-deleted: {result.files_soft_deleted}")
    for ws_id, size in sorted(result.released_bytes.items(), key=lambda kv: str(kv[0])):
        print(f"released   {ws_id}  {size} bytes")


def _summary(result: DeletedContextFilesBackfill) -> str:
    return (
        f"{result.files} file(s) in {len(result.contexts)} deleted context(s), "
        f"{result.uploaded_bytes} uploaded bytes"
    )


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    async def run(db: AsyncSession, dry_run: bool) -> DeletedContextFilesBackfill:
        return await backfill_deleted_context_files(db, dry_run=dry_run)

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda result: result.files,
        noun="file",
        verb="soft-delete",
        summary=_summary,
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="show what would be deleted (default)")
    mode.add_argument("--apply", action="store_true", help="soft-delete the files and release")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
