"""Plan or apply dated admin invite batches (#2704).

Mint emits credential CSV only on apply; diagnostics and plans go to stderr.
Run in the API environment: python -m src.cli.beta_invite_batches --help.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import configure_logging, run_plan_apply  # noqa: E402
from services.beta_invite_service import BetaInviteService, MintedBetaInvite  # noqa: E402
from utils.datetime import to_utc_iso  # noqa: E402


@dataclass
class BatchResult:
    count: int
    minted: list[MintedBetaInvite] = field(default_factory=list)


def dated_batches(args: argparse.Namespace) -> list[tuple[str, datetime, int]]:
    """Resolve each local expiry separately (calendar days, including DST)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,88}", args.label_prefix):
        raise ValueError("Label prefix must be 1-89 ASCII letters, digits, underscores or hyphens")
    if not 1 <= args.days <= 60 or not 1 <= args.valid_days <= 60:
        raise ValueError("Days and valid-days must be between 1 and 60")
    if not 1 <= args.per_day <= 1000 or args.days * args.per_day > 10000:
        raise ValueError("Use 1-1000 invites per day and at most 10000 per run")
    zone = ZoneInfo(args.timezone)
    expiry_time = time.fromisoformat(args.expiry_time)
    if expiry_time.tzinfo is not None or expiry_time.second or expiry_time.microsecond:
        raise ValueError("Expiry time must be HH:MM without an offset")
    batches = []
    for day in range(args.days):
        sending = args.start + timedelta(days=day)
        expiry = datetime.combine(sending + timedelta(days=args.valid_days), expiry_time, zone)
        # Refuse ambiguous/nonexistent wall times rather than silently choosing
        # a different expiry across a DST transition.
        if (
            expiry.replace(fold=0).utcoffset() != expiry.replace(fold=1).utcoffset()
            or expiry.astimezone(UTC).astimezone(zone) != expiry
        ):
            raise ValueError("Expiry falls in a DST transition; choose another time")
        batches.append(
            (
                f"{args.label_prefix}-{sending.isoformat()}",
                expiry.astimezone(UTC).replace(tzinfo=None),
                args.per_day,
            )
        )
    return batches


async def _main(args: argparse.Namespace) -> int:
    configure_logging()
    output = sys.stdout
    reported = False
    try:
        batches = dated_batches(args) if args.command == "mint" else []
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        print("error: invalid batch schedule, prefix, time or timezone", file=sys.stderr)
        return 2

    async def run(db: AsyncSession, dry_run: bool) -> BatchResult:
        svc = BetaInviteService(db)
        try:
            if args.command == "mint":
                minted = await svc.create_batches(args.inviter, batches, dry_run=dry_run)
                return BatchResult(sum(count for _, _, count in batches), minted)
            return BatchResult(
                await svc.revoke_unused_by_label(args.inviter, args.label, dry_run=dry_run)
            )
        except (ValueError, PermissionError):
            raise
        except Exception:
            # SQL exceptions can include parameters (labels, token hashes).
            raise RuntimeError(
                "Batch operation failed; no credential output is available"
            ) from None

    def print_applied(result: BatchResult) -> None:
        nonlocal reported
        reported = True
        if args.command == "revoke":
            print(result.count, file=output)
            return
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["label", "expires_at", "invite_id", "url"])
        for minted in result.minted:
            writer.writerow(
                [
                    minted.invite.label,
                    to_utc_iso(minted.invite.expires_at),
                    str(minted.invite.id),
                    minted.url,
                ]
            )

    def print_plan(result: BatchResult) -> None:
        print(f"{args.command}: {result.count} invite(s)")
        for index, (_, expiry, count) in enumerate(batches, start=1):
            print(f"  batch {index}: {count} invite(s), expires {to_utc_iso(expiry)}")

    # _oneshot's plan, confirmation and status lines must never contaminate CSV.
    with redirect_stdout(sys.stderr):
        code = await run_plan_apply(
            run=run,
            print_plan=print_plan,
            changes=lambda result: result.count,
            noun="invite",
            verb=args.command,
            apply=args.apply,
            assume_yes=args.yes,
            print_applied=print_applied,
        )
    if code == 0 and args.command == "revoke" and args.apply and not reported:
        print(0, file=output)
    return code


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    mint = commands.add_parser("mint", help="mint dated invite stock")
    revoke = commands.add_parser("revoke", help="revoke unused invites in one batch")
    for command in (mint, revoke):
        command.add_argument("--inviter", required=True, help="system admin user id (not email)")
        command.add_argument("--apply", action="store_true")
        command.add_argument("--yes", action="store_true")
    mint.add_argument("--start", required=True, type=date.fromisoformat)
    mint.add_argument("--days", required=True, type=int)
    mint.add_argument("--per-day", required=True, type=int)
    mint.add_argument("--valid-days", type=int, default=7)
    mint.add_argument("--label-prefix", required=True)
    mint.add_argument("--expiry-time", default="10:00", help="local HH:MM (default 10:00)")
    mint.add_argument("--timezone", default="Asia/Tokyo", help="IANA zone (default Asia/Tokyo)")
    revoke.add_argument("--label", required=True)
    args = parser.parse_args(argv)
    if args.apply != args.yes:
        parser.error("writes require both --apply and --yes; omit both for a plan")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parse())))
