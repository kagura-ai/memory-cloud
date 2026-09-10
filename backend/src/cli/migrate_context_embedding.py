"""Move contexts to another embedding model without a search outage (#1525).

Runs where the API runs (same env: DATABASE_URL, QDRANT_*, SELF_HOSTED_*,
EMBEDDING_MODEL_ALLOWLIST), e.g. inside the API container::

    python -m src.cli.migrate_context_embedding --to qwen3-embedding:4b --context <uuid>
    python -m src.cli.migrate_context_embedding --to qwen3-embedding:4b --all --run --yes

Steps (each is a flag; ``--run`` chains the first three):

    --plan        resolve source/target, count memories        (default, read-only)
    --reembed     fill the target collection; routing untouched (idempotent, re-runnable)
    --verify      every live memory has a point in the target  (read-only)
    --switch      flip routing + re-queue the delta since the re-embed started
    --purge       delete the source points (only after a switch; irreversible)
    --rollback-to MODEL   flip routing back (source points are still there)

Exit codes: 0 ok · 1 error · 2 verification found missing points.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.constants import EMBEDDING_MODEL_REGISTRY  # noqa: E402
from config.settings import get_settings  # noqa: E402
from db.base import get_db  # noqa: E402
from services.embedding_migration_service import (  # noqa: E402
    MigrationPlan,
    list_migratable_context_ids,
    plan_context_migration,
    purge_source_points,
    reembed_context,
    switch_context_embedding,
    verify_context_migration,
)
from utils.exceptions import MemoryCloudException  # noqa: E402


def _print_plan(plan: MigrationPlan) -> None:
    print(f"context   {plan.context_id}  (workspace {plan.workspace_id})")
    print(f"  source  {plan.source_model} / {plan.source_dimensions}d  -> {plan.source_collection}")
    print(f"  target  {plan.target_model} / {plan.target_dimensions}d  -> {plan.target_collection}")
    print(f"  memories {plan.memory_count}")


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def _one_context(db, context_id: UUID, args: argparse.Namespace) -> int:
    settings = get_settings()

    if args.rollback_to:
        dims = EMBEDDING_MODEL_REGISTRY[args.rollback_to][0]
        if not _confirm(f"Route {context_id} back to {args.rollback_to}?", args.yes):
            print("  skipped")
            return 0
        result = await switch_context_embedding(db, context_id, args.rollback_to, dims)
        print(
            f"  rolled back {context_id}: {result.previous_model} -> {result.model} "
            f"(requeued {result.requeued})"
        )
        return 0

    plan = await plan_context_migration(
        db, context_id, args.to, allowlist_setting=settings.embedding_model_allowlist
    )
    _print_plan(plan)
    if not (args.reembed or args.verify or args.switch or args.purge or args.run):
        return 0  # --plan only

    requeue_since = None
    if args.reembed or args.run:

        def _progress(done: int, total: int) -> None:
            print(f"  re-embedded {done}/{total}", flush=True)

        result = await reembed_context(db, plan, batch_size=args.batch_size, progress=_progress)
        requeue_since = result.started_at
        print(f"  re-embed done: {result.embedded} points in {result.batches} batches")

    if args.verify or args.run:
        verified = await verify_context_migration(db, plan)
        print(f"  verify: {verified.present}/{verified.expected} present")
        if not verified.ok:
            print(
                f"  MISSING {len(verified.missing)}: {', '.join(str(m) for m in verified.missing[:20])}"
            )
            return 2

    if args.switch or args.run:
        if requeue_since is None:
            print(
                "  note: --switch without --reembed in the same run re-queues nothing; "
                "memories written since the re-embed will be embedded under the new "
                "routing only if you pass --requeue-since"
            )
            requeue_since = args.requeue_since
        if not _confirm(f"Switch {context_id} to {plan.target_model}?", args.yes):
            print("  skipped")
            return 0
        switched = await switch_context_embedding(
            db,
            context_id,
            plan.target_model,
            plan.target_dimensions,
            requeue_since=requeue_since,
        )
        print(
            f"  switched: {switched.previous_model} -> {switched.model} "
            f"(requeued {switched.requeued} for the sweep)"
        )

    if args.purge:
        if not _confirm(
            f"Delete {context_id}'s points from {plan.source_collection}? (irreversible)",
            args.yes,
        ):
            print("  skipped")
            return 0
        deleted = await purge_source_points(db, plan)
        print(f"  purged {deleted} points from {plan.source_collection}")

    return 0


async def _main(args: argparse.Namespace) -> int:
    worst = 0
    async for db in get_db():
        if args.all:
            context_ids = await list_migratable_context_ids(db)
        elif args.workspace:
            context_ids = await list_migratable_context_ids(db, workspace_id=args.workspace)
        else:
            context_ids = [args.context]
        print(f"{len(context_ids)} context(s)")
        for context_id in context_ids:
            try:
                code = await _one_context(db, context_id, args)
            except MemoryCloudException as exc:
                print(f"  skip {context_id}: {exc}")
                code = 1
            worst = max(worst, code)
    return worst


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--context", type=UUID, help="one context id")
    scope.add_argument("--workspace", type=UUID, help="every live context in a workspace")
    scope.add_argument("--all", action="store_true", help="every live context in the deployment")
    parser.add_argument("--to", help="target embedding model (registry name)")
    parser.add_argument("--plan", action="store_true", help="resolve and print only (default)")
    parser.add_argument("--reembed", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--switch", action="store_true")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--run", action="store_true", help="--reembed + --verify + --switch")
    parser.add_argument("--rollback-to", metavar="MODEL", help="flip routing back to MODEL")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--requeue-since",
        type=lambda s: __import__("datetime").datetime.fromisoformat(s),
        help="ISO timestamp; with a standalone --switch, re-queue memories written since",
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompts")
    args = parser.parse_args(argv)
    if args.rollback_to:
        if args.rollback_to not in EMBEDDING_MODEL_REGISTRY:
            parser.error(f"unknown model for --rollback-to: {args.rollback_to}")
    elif not args.to:
        parser.error("--to is required unless --rollback-to is given")
    if args.purge and (args.reembed or args.run):
        parser.error("--purge is a separate step; run it after a switch has settled")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
