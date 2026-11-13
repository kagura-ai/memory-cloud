"""Upload malware scan: backlog status and rescans (#565).

Runs where the API runs (same env: DATABASE_URL, FILE_SCAN_ENABLED, ...),
e.g. inside the API container::

    python -m src.cli.file_scan status
    python -m src.cli.file_scan rescan --file <file-id>                  # plan (read-only)
    python -m src.cli.file_scan rescan --file <file-id> --apply --yes    # scan it again
    python -m src.cli.file_scan rescan --errors --since 2026-11-01 --apply

``status`` prints counts and the oldest due time only. ``rescan`` puts files
back to ``pending``; the sweeper scans them within a minute (without checksum
binding a file still inside its upload window waits for it). ``--file`` takes
any live uploaded file, whatever its verdict — the way to re-check a false
positive after a signature update; there is no manual "release". ``--errors``
limits the run to files the scanner could not check and needs ``--since``
(verdicts that recent or newer) or ``--file``. Refused while
``FILE_SCAN_ENABLED`` is false (nothing would scan them).

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

from cli._env_guard import require_settings  # noqa: E402
from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from db.base import get_db  # noqa: E402
from services.file_scan_service import (  # noqa: E402
    RescanSelection,
    file_scan_backlog,
    requeue_for_rescan,
)
from utils.datetime import to_utc_iso, utcnow  # noqa: E402


def _parse_since(value: str) -> datetime:
    """ISO date or time; an offset is converted, a naive value is UTC. Naive UTC out."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _scan_enabled() -> bool:
    return require_settings().file_scan_enabled


async def _status() -> int:
    require_settings()
    try:
        async for db in get_db():
            backlog = await file_scan_backlog(db, utcnow())
            oldest = (
                f" (oldest due {to_utc_iso(backlog.oldest_due_pending_at)})"
                if backlog.oldest_due_pending_at
                else ""
            )
            print(f"scanning        {'on' if backlog.enabled else 'off'}")
            print(f"due pending     {backlog.due_pending}{oldest}")
            print(f"waiting         {backlog.waiting_pending}")
            print(f"scan failed     {backlog.errored}")
            print(f"quarantined     {backlog.quarantined}")
            print(f"unscanned       {backlog.unscanned}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _print_plan(selection: RescanSelection) -> None:
    for file_id, status, cause in selection.rows:
        print(f"file       {file_id}  {status}{f' ({cause})' if cause else ''}")
    print(f"{selection.count} file(s) to rescan")


async def _rescan(args: argparse.Namespace) -> int:
    async def run(db: AsyncSession, dry_run: bool) -> RescanSelection:
        if not _scan_enabled():
            raise RuntimeError("FILE_SCAN_ENABLED is false; nothing would scan the files")
        return await requeue_for_rescan(
            db,
            now=utcnow(),
            settings=require_settings(),
            dry_run=dry_run,
            file_id=args.file,
            errors_only=args.errors,
            errors_since=args.since,
        )

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda selection: selection.count,
        noun="file",
        verb="requeue",
        apply=args.apply,
        assume_yes=args.yes,
    )


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    if args.command == "status":
        return await _status()
    return await _rescan(args)


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="print the scan backlog")
    rescan = commands.add_parser("rescan", help="put files back to pending")
    for command in (status, rescan):
        add_log_level_argument(command)
    rescan.add_argument("--file", type=UUID, default=None, help="this file")
    rescan.add_argument(
        "--errors", action="store_true", help="only files the scanner could not check"
    )
    rescan.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="with --errors: verdicts at or after this ISO date/time (UTC unless an offset)",
    )
    mode = rescan.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="show what would change (default)")
    mode.add_argument("--apply", action="store_true", help="requeue the files")
    rescan.add_argument("--yes", action="store_true", help="no confirmation prompt")
    args = parser.parse_args(argv)
    if args.command == "rescan":
        if args.file is None and not args.errors:
            parser.error("rescan needs --file or --errors")
        if args.errors and args.file is None and args.since is None:
            parser.error("--errors needs --since or --file")
        if args.since is not None and not args.errors:
            parser.error("--since works with --errors")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
