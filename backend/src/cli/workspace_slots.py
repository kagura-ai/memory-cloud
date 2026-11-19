"""Inspect and reconcile workspace slot suspension (#2918).

Runs where the API runs (same env), e.g. inside the API container::

    python -m src.cli.workspace_slots status                  # mode, counts
    python -m src.cli.workspace_slots status --owner <user_id>
    python -m src.cli.workspace_slots status --over-allowance # owners above it now
    python -m src.cli.workspace_slots reconcile               # every owner with rows
    python -m src.cli.workspace_slots reconcile --owner <user_id>
    python -m src.cli.workspace_slots reconcile --even-when-off

``status --over-allowance`` lists the owners who have more workspaces active
than their allowance now (from before the feature, or a resume-only swap):
their next drop cuts them to it. ``reconcile`` resumes and prunes only; it
never suspends. It waits for each owner's slot lock (up to 5 s) and commits
per owner; an owner whose reconcile fails is reported in every mode. While
``WORKSPACE_SLOT_SUSPENSION=off`` it refuses unless ``--even-when-off`` is
given: run that before switching from ``off`` back to ``shadow`` or
``enforce``, so rows left from an earlier ``enforce`` period that should no
longer hold (a workspace upgraded or deleted meanwhile) are gone first.

Exit codes: 0 ok · 1 an owner was skipped or failed (the others ran) · 2 refused (mode off without --even-when-off, or
the tables are missing).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select  # noqa: E402

from cli._env_guard import require_settings  # noqa: E402
from db.base import get_db, isolated_session  # noqa: E402
from models.auth import Workspace  # noqa: E402
from models.workspace_slots import WorkspaceSlotState, WorkspaceSlotSuspension  # noqa: E402
from services.workspace_slots import (  # noqa: E402
    SlotMode,
    get_slot_overview,
    owner_slot_lock,
    reconcile_workspace_slots,
    slot_tables_present,
    workspace_slot_mode,
)


async def _over_allowance(db) -> int:
    """Print the owners with more workspaces active than their allowance."""
    owners = list(
        (
            await db.execute(
                select(Workspace.owner_user_id)
                .where(Workspace.deleted_at.is_(None))
                .group_by(Workspace.owner_user_id)
                .having(func.count() > 1)
                .order_by(Workspace.owner_user_id)
            )
        ).scalars()
    )
    over = 0
    for one in owners:
        overview = await get_slot_overview(db, one)
        allowance = max(overview.allowance.cap, overview.contracted_count)
        if overview.active_count > allowance:
            over += 1
            print(
                f"  {one}: active {overview.active_count}, allowance {allowance}, "
                f"owned {overview.owned_count}"
            )
    print(f"owners over their allowance: {over}")
    return 0


async def _status(owner: str | None, over_allowance: bool = False) -> int:
    require_settings()
    mode = workspace_slot_mode()
    print(f"WORKSPACE_SLOT_SUSPENSION={mode}")
    async for db in get_db():
        if not await slot_tables_present(db):
            print("slot tables: missing (run `alembic upgrade head`)")
            return 2
        if over_allowance:
            return await _over_allowance(db)
        suspended = await db.scalar(select(func.count()).select_from(WorkspaceSlotSuspension))
        owners = await db.scalar(
            select(func.count(func.distinct(WorkspaceSlotSuspension.owner_user_id)))
        )
        required = await db.scalar(
            select(func.count()).where(WorkspaceSlotState.selection_required_since.is_not(None))
        )
        print(f"suspension rows: {suspended} (owners: {owners})")
        print(f"owners flagged selection_required: {required}")
        if owner is not None:
            overview = await get_slot_overview(db, owner)
            allowance = overview.allowance
            print(
                f"owner {owner}: cap {allowance.cap} (base {allowance.base} + bonus "
                f"{allowance.slot_bonus} + {allowance.tier} grant {allowance.tier_grant}), "
                f"owned {overview.owned_count}, active {overview.active_count} of "
                f"{overview.active_slots}, contracted {overview.contracted_count}, "
                f"selection_required {overview.selection_required}, version {overview.version}"
            )
            for w in overview.workspaces:
                print(f"  {w.id} {w.status:<14} {w.plan_name:<7} {w.name}")
    return 0


async def _reconcile(owner: str | None, even_when_off: bool) -> int:
    require_settings()
    configured = workspace_slot_mode()
    if configured == "off" and not even_when_off:
        print(
            "WORKSPACE_SLOT_SUSPENSION=off: nothing runs. Pass --even-when-off to resume and prune."
        )
        return 2
    # Resume and prune only, in every mode, as enforce runs it (no suspension
    # on this path): an error reaches the report instead of the shadow log.
    mode: SlotMode = "enforce"
    async for db in get_db():
        if not await slot_tables_present(db):
            print("slot tables: missing (run `alembic upgrade head`)")
            return 2
        if owner is not None:
            owners = [owner]
        else:
            owners = list(
                (
                    await db.execute(
                        select(WorkspaceSlotSuspension.owner_user_id)
                        .group_by(WorkspaceSlotSuspension.owner_user_id)
                        .order_by(WorkspaceSlotSuspension.owner_user_id)
                    )
                ).scalars()
            )
        await db.commit()
        skipped = failed = 0
        for one in owners:
            try:
                async with isolated_session(db) as session:
                    if not await owner_slot_lock(session, one, wait=True):
                        print(f"  {one}: busy, skipped")
                        skipped += 1
                        continue
                    outcome = await reconcile_workspace_slots(
                        session, one, cause="cli", allow_suspend=False, mode=mode
                    )
                    await session.commit()
                    print(f"  {one}: resumed {len(outcome.resumed)}, pruned {outcome.pruned}")
            except Exception as exc:  # noqa: BLE001 — one owner never stops the run
                print(f"  {one}: failed ({type(exc).__name__})")
                failed += 1
        print(f"owners: {len(owners)}, skipped: {skipped}, failed: {failed}")
        return 1 if skipped or failed else 0
    return 0


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="mode, row counts, one owner's slots")
    status.add_argument("--owner", help="a user_id: show that owner's slots")
    status.add_argument(
        "--over-allowance",
        action="store_true",
        help="list the owners with more workspaces active than their allowance",
    )
    reconcile = sub.add_parser("reconcile", help="resume and prune (never suspends)")
    reconcile.add_argument("--owner", help="only this user_id (default: every owner with rows)")
    reconcile.add_argument(
        "--even-when-off",
        action="store_true",
        help="run while WORKSPACE_SLOT_SUSPENSION=off (before switching it back on)",
    )
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    if args.command == "status":
        return await _status(args.owner, args.over_allowance)
    return await _reconcile(args.owner, args.even_when_off)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
