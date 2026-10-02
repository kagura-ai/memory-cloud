"""Restore a deleted context from its soft-deleted rows (#1804).

Deleting a context soft-deletes it and its memories and, since v0.88.0,
removes its points from the vector store. Until the tombstone purge
(``CLEANUP_DELETED_MEMORIES_RETENTION_DAYS``, default 30 days) the rows are
still there; this brings the context back from them and marks its memories
for re-embedding, which the API's embedding sweep then does (about 2,400
memories an hour). Runs where the API runs (same env: DATABASE_URL), e.g.
inside the API container::

    python -m src.cli.restore_context <context-id>                # plan (default, read-only)
    python -m src.cli.restore_context <context-id> --apply --yes  # restore
    python -m src.cli.restore_context <context-id> --name new-name --apply

Memories forgotten before the deletion and Sleep tombstones stay deleted, as
do rows the purge already removed. ``--name`` restores under another name when
a live context of the workspace has taken the old one.

Exit codes: 0 ok · 1 error (not found, not deleted, name or resource taken).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from services.context_restore import (  # noqa: E402
    ContextRestoreResult,
    restore_deleted_context,
)
from utils.datetime import to_utc_iso  # noqa: E402


def _print_plan(result: ContextRestoreResult) -> None:
    name = result.name
    if result.renamed_from:
        name = f"{result.name} (was {result.renamed_from})"
    print(f"context    {result.context_id}  {name}")
    print(f"workspace  {result.workspace_id}")
    print(f"deleted    {to_utc_iso(result.deleted_at)} by {result.deleted_by or '-'}")
    print(f"memories   {result.memories_restored} to restore and re-embed")
    if result.memories_left_deleted:
        print(
            f"           {result.memories_left_deleted} stay deleted "
            "(forgotten before the deletion, or Sleep tombstones)"
        )
    for warning in result.warnings:
        print(f"warning    {warning}")


def _print_applied(result: ContextRestoreResult) -> None:
    print(
        f"memories restored: {result.memories_restored}; the embedding sweep rebuilds "
        "their vectors over the next runs"
    )


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    async def run(db: AsyncSession, dry_run: bool) -> ContextRestoreResult:
        return await restore_deleted_context(
            db, args.context_id, dry_run=dry_run, new_name=args.name, actor_id=args.actor
        )

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        # One context; a context with no memories left is still restored.
        changes=lambda result: 1,
        noun="context",
        verb="restore",
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("context_id", type=UUID, help="id of the deleted context")
    parser.add_argument(
        "--name", default=None, help="restore under this name (when the old one is taken)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="show what would be restored (default)")
    mode.add_argument("--apply", action="store_true", help="restore the context")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    parser.add_argument(
        "--actor",
        default=None,
        help="who restores, for the audit row (default: cli:<OS user>)",
    )
    add_log_level_argument(parser)
    args = parser.parse_args(argv)
    if not args.actor:
        args.actor = f"cli:{_os_user()}"
    return args


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no USER/LOGNAME and no passwd entry (some containers)
        return "unknown"


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
