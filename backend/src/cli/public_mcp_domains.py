"""List, show and release verified custom domains of public MCP endpoints (#2809).

The rows of ``public_mcp_domains`` (contract: docs/public-mcp-domains.md).
Runs where the API runs (same env: DATABASE_URL), e.g. inside the API
container::

    python -m src.cli.public_mcp_domains list [--state S] [--workspace UUID]
    python -m src.cli.public_mcp_domains show --domain-id ID
    python -m src.cli.public_mcp_domains show --hostname NAME
    python -m src.cli.public_mcp_domains release --domain-id ID                     # plan
    python -m src.cli.public_mcp_domains release --domain-id ID --apply --yes       # release
    python -m src.cli.public_mcp_domains release --domain-id ID --skip-cooldown --apply --yes

``list`` and ``show`` never write. ``release`` plans by default; with
``--apply`` it marks a held (``active`` / ``suspended``) row ``released``
with the reassignment cooldown, the way an owner's detach does, and deletes
a ``pending`` row. ``--skip-cooldown`` deletes the row instead (a held one,
or a ``released`` one still in its cooldown). It deletes this row only: the
name is free for another workspace once no other row of it is held or still
cooling down (a workspace that detached a name twice leaves two ``released``
rows), which ``show --hostname`` lists. Every change writes one audit row
(``public_mcp_domain.released``, actor ``cli:public-mcp-domains``, the host
name's digest only).

It works while ``MCP_PUBLIC_DOMAINS_ENABLED`` is off: that is how an operator
removes host names after switching the feature off. Host names are printed
here, for the operator; they never go to a log line. The verification token
is not printed.

Exit codes: 0 ok · 1 error (no such domain) · 2 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from models.public_mcp_domain import PublicMcpDomain  # noqa: E402
from services.public_mcp_domains import (  # noqa: E402
    DomainActor,
    _audit,
    _db_now,
    _release,
)
from utils.datetime import to_utc_iso  # noqa: E402
from utils.public_hostname import normalize_hostname  # noqa: E402

# The command line's audit actor. ``AuditLog.user_email`` is NOT NULL: the
# actor string fills both columns. (Ids starting with "operator:" are the
# operator principals of the system admin mode, never minted here.)
CLI_ACTOR = DomainActor("cli:public-mcp-domains", "cli:public-mcp-domains")
STATES = ("pending", "active", "suspended", "released")

ReleaseAction = Literal["release", "delete", "none"]


# ----------------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------------


async def list_domains(
    db: AsyncSession, *, state: str | None, workspace_id: UUID | None
) -> list[PublicMcpDomain]:
    """Every row, optionally of one state and one workspace, by host name."""
    stmt = select(PublicMcpDomain).order_by(PublicMcpDomain.hostname, PublicMcpDomain.created_at)
    if state is not None:
        stmt = stmt.where(PublicMcpDomain.state == state)
    if workspace_id is not None:
        stmt = stmt.where(PublicMcpDomain.workspace_id == workspace_id)
    return list((await db.execute(stmt)).scalars().all())


async def find_domains(
    db: AsyncSession, *, domain_id: UUID | None, hostname: str | None
) -> list[PublicMcpDomain]:
    """The row of ``domain_id``, or every row of ``hostname`` (oldest first)."""
    stmt = select(PublicMcpDomain).order_by(PublicMcpDomain.created_at)
    if domain_id is not None:
        stmt = stmt.where(PublicMcpDomain.id == domain_id)
    else:
        stmt = stmt.where(PublicMcpDomain.hostname == hostname)
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        raise LookupError("no public MCP domain matches")
    return rows


def _print_table(rows: list[PublicMcpDomain]) -> None:
    print(f"{len(rows)} domain(s)")
    for row in rows:
        reason = f"({row.suspended_reason})" if row.suspended_reason else ""
        until = row.serve_until if row.state == "active" else row.cooldown_until
        print(
            f"{row.id}  {row.state + reason:<22} ws {row.workspace_id}  "
            f"{row.hostname}  until {to_utc_iso(until) or '-'}"
        )


def _print_rows(rows: list[PublicMcpDomain]) -> None:
    for row in rows:
        print(f"domain_id             {row.id}")
        print(f"hostname              {row.hostname}")
        print(f"state                 {row.state}")
        print(f"suspended_reason      {row.suspended_reason or '-'}")
        print(f"workspace_id          {row.workspace_id}")
        print(f"endpoint_id           {row.endpoint_id}")
        for name in (
            "serve_until",
            "created_at",
            "verified_at",
            "proof_ok_at",
            "last_checked_at",
            "suspended_at",
            "released_at",
            "cooldown_until",
        ):
            print(f"{name:<22}{to_utc_iso(getattr(row, name)) or '-'}")
        print(f"last_proof_result     {row.last_proof_result or '-'}")
        print(f"proof_missing_checks  {row.proof_missing_checks}")
        print(f"routing_status        {row.routing_status or '-'}")
        print(f"check_seq             {row.check_seq}")
        print()


# ----------------------------------------------------------------------------
# Release
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleasePlan:
    """What ``release`` does (or did) to one row."""

    domain_id: UUID
    hostname: str
    workspace_id: UUID
    state: str
    action: ReleaseAction
    applied: bool


def _action(state: str, skip_cooldown: bool) -> ReleaseAction:
    if state == "pending":
        return "delete"
    if state in ("active", "suspended"):
        return "delete" if skip_cooldown else "release"
    return "delete" if skip_cooldown else "none"


async def release_domain(
    db: AsyncSession, domain_id: UUID, *, skip_cooldown: bool, dry_run: bool
) -> ReleasePlan:
    """Plan, or apply and commit, the release of one row.

    Applying re-reads the row ``FOR UPDATE`` and decides again from its state
    then. A held row is released with the cooldown (database clock), or
    deleted with ``skip_cooldown``; a ``pending`` row is deleted; a
    ``released`` row is deleted with ``skip_cooldown`` and left alone without.

    Raises:
        LookupError: No row has this id.
    """
    stmt = select(PublicMcpDomain).where(PublicMcpDomain.id == domain_id)
    if not dry_run:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        await db.rollback()
        raise LookupError(f"no public MCP domain {domain_id}")
    state = row.state
    action = _action(state, skip_cooldown)
    plan = ReleasePlan(row.id, row.hostname, row.workspace_id, state, action, applied=False)
    if dry_run or action == "none":
        await db.rollback()
        return plan
    try:
        if action == "delete":
            await db.delete(row)
            to_state = None
        else:
            _release(row, await _db_now(db))
            to_state = "released"
        await db.flush()
        _audit(
            db,
            CLI_ACTOR,
            "released",
            domain_id=plan.domain_id,
            workspace_id=plan.workspace_id,
            endpoint_id=row.endpoint_id,
            hostname=plan.hostname,
            from_state=state,
            to_state=to_state,
            reason="operator",
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return ReleasePlan(plan.domain_id, plan.hostname, plan.workspace_id, state, action, True)


def _print_release(plan: ReleasePlan) -> None:
    print(f"domain_id     {plan.domain_id}")
    print(f"hostname      {plan.hostname}")
    print(f"workspace_id  {plan.workspace_id}")
    print(f"state         {plan.state}")
    if plan.action == "none":
        print("action        nothing to do (released; --skip-cooldown deletes it)")
    elif plan.action == "delete":
        print(
            "action        delete this row (other rows of the name may still hold it"
            " or be cooling down: show --hostname)"
        )
    else:
        print("action        release with the reassignment cooldown")


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    if args.command == "list":

        async def list_run(db: AsyncSession, dry_run: bool) -> list[PublicMcpDomain]:
            return await list_domains(db, state=args.state, workspace_id=args.workspace)

        return await run_plan_apply(
            run=list_run,
            print_plan=_print_table,
            changes=lambda rows: 0,
            noun="domain",
            apply=False,
            assume_yes=False,
        )

    if args.command == "show":
        hostname: str | None = None
        if args.hostname is not None:
            hostname = normalize_hostname(args.hostname)
            if hostname is None:
                print("error: --hostname is not a host name", file=sys.stderr)
                return 2

        async def show_run(db: AsyncSession, dry_run: bool) -> list[PublicMcpDomain]:
            return await find_domains(db, domain_id=args.domain_id, hostname=hostname)

        return await run_plan_apply(
            run=show_run,
            print_plan=_print_rows,
            changes=lambda rows: 0,
            noun="domain",
            apply=False,
            assume_yes=False,
        )

    skip_cooldown: bool = args.skip_cooldown

    async def release_run(db: AsyncSession, dry_run: bool) -> ReleasePlan:
        return await release_domain(
            db, args.domain_id, skip_cooldown=skip_cooldown, dry_run=dry_run
        )

    return await run_plan_apply(
        run=release_run,
        print_plan=_print_release,
        changes=lambda plan: 0 if plan.action == "none" else 1,
        noun="domain",
        verb="delete" if skip_cooldown else "release",
        past_tense="deleted" if skip_cooldown else "released",
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_cmd = commands.add_parser("list", help="print the rows, one per line")
    list_cmd.add_argument("--state", choices=STATES, help="only rows in this state")
    list_cmd.add_argument("--workspace", type=UUID, help="only rows of this workspace id")
    add_log_level_argument(list_cmd)

    show = commands.add_parser("show", help="print one row, or every row of a host name")
    selector = show.add_mutually_exclusive_group(required=True)
    selector.add_argument("--domain-id", type=UUID, help="domain id")
    selector.add_argument("--hostname", help="host name (every row of it)")
    add_log_level_argument(show)

    release = commands.add_parser(
        "release", help="release one domain (plan by default; --apply writes)"
    )
    release.add_argument("--domain-id", type=UUID, required=True, help="domain id")
    release.add_argument(
        "--skip-cooldown",
        action="store_true",
        help="delete this row instead (other rows of the name: show --hostname)",
    )
    mode = release.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="show what would change (default)")
    mode.add_argument("--apply", action="store_true", help="release the domain")
    release.add_argument("--yes", action="store_true", help="no confirmation prompt")
    add_log_level_argument(release)

    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
