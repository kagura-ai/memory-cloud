"""Move contexts to another embedding model without a search outage (#1525).

Runs where the API runs (same env: DATABASE_URL, QDRANT_*, SELF_HOSTED_*,
EMBEDDING_MODEL_ALLOWLIST), e.g. inside the API container::

    python -m src.cli.migrate_context_embedding --to qwen3-embedding:4b --context <uuid>
    python -m src.cli.migrate_context_embedding --to qwen3-embedding:4b --all --run --yes

Steps (each is a flag; ``--run`` chains the first three):

    --plan        resolve source/target, count memories        (default, read-only)
    --reembed     fill the target collection; routing untouched (idempotent, re-runnable).
                  Resource-ingested memories are rebuilt from their document, under
                  the document's point id; one that cannot be rebuilt is skipped
                  and listed
    --verify      every live memory has a point in the target; drops target points
                  whose memory was forgotten meanwhile. Resource-ingested memories
                  that cannot be rebuilt are listed, and do not fail the step.
                  With content_chunks_enabled (#2015) the re-embed also rebuilds
                  content chunk points; memories whose chunks are incomplete are
                  listed on a CHUNKS_MISSING line, and do not fail the step. When
                  a switch follows (--run, --verify --switch) their chunks are
                  rebuilt first, and the switch is refused while any are left
    --switch      flip routing + re-queue the delta since the re-embed started
    --purge       delete the source points, in the same invocation as --switch
    --purge-source MODEL  delete MODEL's points for the context in a later run,
                  after routing has moved on (the plan can no longer name the
                  old source by itself); refused while MODEL is still active
    --rollback-to MODEL   route back to MODEL and re-queue everything written
                  since the switch (its collection must still exist). The switch
                  time is the one the switch recorded; the boundary is printed
                  before the confirmation, with a WARNING when only the search
                  config's last change is known (a switch before v0.116.0)

Exit codes: 0 ok · 1 error · 2 verification found missing points (or, before a
switch, memories whose content chunks could not be completed).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.constants import EMBEDDING_MODEL_REGISTRY  # noqa: E402
from config.settings import get_settings  # noqa: E402
from db.base import get_db  # noqa: E402
from services.context_routing import resolve_context_embedding  # noqa: E402
from services.embedding_migration_service import (  # noqa: E402
    MigrationPlan,
    SwitchResult,
    VerifyResult,
    list_migratable_context_ids,
    plan_context_migration,
    purge_source_points,
    rebuild_target_chunks,
    reembed_context,
    resolve_rollback_boundary,
    rollback_context_embedding,
    switch_context_embedding,
    verify_context_migration,
)
from utils.datetime import to_naive_utc  # noqa: E402
from utils.exceptions import MemoryCloudException  # noqa: E402


def _print_plan(plan: MigrationPlan) -> None:
    print(f"context   {plan.context_id}  (workspace {plan.workspace_id})")
    print(f"  source  {plan.source_model} / {plan.source_dimensions}d  -> {plan.source_collection}")
    print(f"  target  {plan.target_model} / {plan.target_dimensions}d  -> {plan.target_collection}")
    print(f"  memories {plan.memory_count}")
    if plan.content_chunks_on:
        print(
            f"  content chunks on: the re-embed also writes ~{plan.chunk_estimate} "
            "chunk point(s), one embedding input each"
        )


def _print_unrebuildable(memory_ids: list[UUID]) -> None:
    """Resource-ingested memories the migration cannot give a vector (#1896)."""
    if not memory_ids:
        return
    shown = ", ".join(str(memory_id) for memory_id in memory_ids[:20])
    more = f" (+{len(memory_ids) - 20} more)" if len(memory_ids) > 20 else ""
    print(
        f"  UNREBUILDABLE {len(memory_ids)} resource-ingested memory(ies) have no vector "
        "in the target collection: the resource has no schema left, or the memory no "
        "longer holds a JSON document. Ingest the documents again as a newer version "
        f"to make them searchable: {shown}{more}"
    )


_CHUNKS_REMEDY_NO_SWITCH = (
    "Not switched yet: re-run --reembed (idempotent) or --run, which rebuilds them "
    "before it switches. Once switched, the migration cannot rebuild them; see the runbook"
)
_CHUNKS_REMEDY_BEFORE_SWITCH = "Rebuilding them before the switch"


def _still_missing_remedy(requeue_since: datetime | None) -> str:
    since = f" --requeue-since {requeue_since.isoformat()}" if requeue_since else ""
    return (
        "Still incomplete after the rebuild, so the context was NOT switched. "
        f"Retry just these with --verify --switch{since} (no full re-embed); if the same "
        "memories stay listed, look for content_chunks_* errors in the log"
    )


def _print_chunk_ids(label: str, ids: list[UUID], remedy: str) -> None:
    shown = ", ".join(str(memory_id) for memory_id in ids[:20])
    more = f" (+{len(ids) - 20} more)" if len(ids) > 20 else ""
    print(f"  {label} {len(ids)} memory(ies): {remedy}: {shown}{more}")


def _print_verify(verified: VerifyResult) -> None:
    print(
        f"  verify: {verified.present}/{verified.expected} present, "
        f"{verified.stale_removed} stale target point(s) removed"
    )


def _print_deferred_prune(switched: SwitchResult) -> None:
    if switched.prune_pending:
        print(
            "  stale-point pruning deferred because point writers were busy; "
            "the route is already switched. Review with "
            "python -m src.cli.sweep_orphan_vectors, then apply with "
            "python -m src.cli.sweep_orphan_vectors --apply --yes --grace-hours 0."
        )


def _print_missing(verified: VerifyResult) -> None:
    print(f"  MISSING {len(verified.missing)}: {', '.join(str(m) for m in verified.missing[:20])}")


def _print_chunks(verified: VerifyResult, remedy: str = _CHUNKS_REMEDY_NO_SWITCH) -> None:
    """Content chunk points of the target (#2015).

    ``remedy`` ends the CHUNKS_MISSING line: what this invocation does about
    them, or what the operator can do (a re-embed works only before the
    switch: afterwards the plan refuses the model the context already uses).
    """
    if verified.chunks_expected or verified.chunks_present:
        print(f"  content chunks: {verified.chunks_present}/{verified.chunks_expected} present")
    if verified.chunks_stale_removed:
        print(
            f"  removed the content chunks of {verified.chunks_stale_removed} memory(ies) "
            "that are no longer live (or the context has content_chunks_enabled off)"
        )
    if verified.chunks_missing:
        ids = verified.chunks_missing
        shown = ", ".join(str(memory_id) for memory_id in ids[:20])
        more = f" (+{len(ids) - 20} more)" if len(ids) > 20 else ""
        print(
            f"  CHUNKS_MISSING {len(ids)} memory(ies) whose number of content chunks in the "
            "target differs from what their content splits into (recall finds them through "
            "their summary meanwhile). "
            f"{remedy}: {shown}{more}"
        )


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def _one_context(db, context_id: UUID, args: argparse.Namespace) -> int:
    settings = get_settings()

    if args.rollback_to:
        # #2098: show the re-queue boundary before asking, so an operator can
        # still abort when only the search config's last change is known.
        boundary = await resolve_rollback_boundary(db, context_id, args.requeue_since)
        if boundary.source == "config_updated_at":
            print(
                f"  WARNING: no recorded switch time for {context_id} (switched before the "
                "switch time was stored); re-queueing since the search config's last change, "
                f"{boundary.since.isoformat()}. If the search settings were edited after the "
                "switch, abort and re-run with --requeue-since <switch time>."
            )
        else:
            label = (
                "--requeue-since" if boundary.source == "explicit" else "the recorded switch time"
            )
            print(f"  re-queue since {boundary.since.isoformat()} ({label})")
        if not _confirm(f"Route {context_id} back to {args.rollback_to}?", args.yes):
            print("  skipped")
            return 0
        result = await rollback_context_embedding(
            db, context_id, args.rollback_to, requeue_since=boundary.since
        )
        print(
            f"  rolled back {context_id}: {result.previous_model} -> {result.model} "
            f"(requeued {result.requeued} for the sweep, "
            f"dropped {result.stale_removed} forgotten)"
        )
        _print_deferred_prune(result)
        return 0

    if args.purge_source:
        active_model, _ = await resolve_context_embedding(db, context_id)
        plan = await plan_context_migration(
            db,
            context_id,
            active_model,
            allowlist_setting=settings.embedding_model_allowlist,
            source_model=args.purge_source,
        )
        _print_plan(plan)
        if not _confirm(
            f"Delete {context_id}'s points from {plan.source_collection}? (irreversible)",
            args.yes,
        ):
            print("  skipped")
            return 0
        deleted = await purge_source_points(db, plan)
        print(f"  purged {deleted} points from {plan.source_collection}")
        return 0

    plan = await plan_context_migration(
        db, context_id, args.to, allowlist_setting=settings.embedding_model_allowlist
    )
    _print_plan(plan)
    if not (args.reembed or args.verify or args.switch or args.purge or args.run):
        return 0  # --plan only

    requeue_since = None
    skipped: list[UUID] = []
    switch_confirmed = False
    if args.reembed or args.run:

        def _progress(done: int, total: int) -> None:
            print(f"  re-embedded {done}/{total}", flush=True)

        result = await reembed_context(db, plan, batch_size=args.batch_size, progress=_progress)
        requeue_since = result.started_at
        chunks = f", {result.chunks_written} content chunk points" if result.chunks_written else ""
        print(f"  re-embed done: {result.embedded} points in {result.batches} batches{chunks}")
        skipped = result.unrebuildable
        if not (args.verify or args.run):
            _print_unrebuildable(skipped)

    if args.verify or args.run:
        switching = bool(args.switch or args.run)
        verified = await verify_context_migration(db, plan)
        _print_verify(verified)
        # One line for both steps. Verify alone would leave out a row the
        # re-embed skipped whose point another live row keeps in the target.
        _print_unrebuildable(sorted({*skipped, *verified.unrebuildable}))
        rebuild_first = switching and verified.ok and bool(verified.chunks_missing)
        _print_chunks(
            verified,
            _CHUNKS_REMEDY_BEFORE_SWITCH if rebuild_first else _CHUNKS_REMEDY_NO_SWITCH,
        )
        if not verified.ok:
            _print_missing(verified)
            return 2
        if rebuild_first:
            # After the switch a re-embed to this target is refused (the
            # context already uses it), so chunks verify found incomplete are
            # rebuilt now: e.g. the context's content chunks became active
            # (content_chunks_enabled turned on, or #2103 the plan allows them
            # again) while the re-embed ran (verify read the flag again; the
            # plan had it off).
            # Asked before the rebuild: it is embedding spend for the switch.
            if not _confirm(
                f"Rebuild the content chunks of {len(verified.chunks_missing)} memory(ies), "
                f"then switch {context_id} to {plan.target_model}?",
                args.yes,
            ):
                print("  skipped")
                return 0
            switch_confirmed = True
            if not plan.content_chunks_on:
                print("  content chunks became active after the plan")
            switch_since = requeue_since if requeue_since is not None else args.requeue_since
            try:
                rebuilt = await rebuild_target_chunks(
                    db,
                    plan,
                    verified.chunks_missing,
                    requeue_since=switch_since,
                    batch_size=args.batch_size,
                )
            except Exception as exc:  # noqa: BLE001 - one context's failure, the run goes on
                # The session is shared by every context of --all: end a
                # transaction the failure may have left aborted.
                await db.rollback()
                # Only our own errors' text: a store or provider error can
                # quote chunk text (content_chunks_* in the log has the class).
                detail = f": {exc}" if isinstance(exc, MemoryCloudException) else ""
                print(
                    f"  chunk rebuild failed, NOT switched ({type(exc).__name__}{detail}); "
                    "retry with --verify --switch"
                )
                return 1
            print(
                f"  rebuilt the content chunks of {rebuilt.rebuilt} memory(ies): "
                f"{rebuilt.chunks_written} chunk points"
            )
            if rebuilt.still_missing:
                _print_chunk_ids(
                    "CHUNKS_MISSING",
                    rebuilt.still_missing,
                    _still_missing_remedy(switch_since),
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
        if not switch_confirmed and not _confirm(
            f"Switch {context_id} to {plan.target_model}?", args.yes
        ):
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
            f"(requeued {switched.requeued} for the sweep, "
            f"dropped {switched.stale_removed} forgotten)"
        )
        _print_deferred_prune(switched)

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
    parser.add_argument(
        "--rollback-to",
        metavar="MODEL",
        help="route back to MODEL and re-queue what was written since the switch",
    )
    parser.add_argument(
        "--purge-source",
        metavar="MODEL",
        help="delete MODEL's points for the context (a later run, after --switch)",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--requeue-since",
        # Naive = UTC; an offset is converted (the memories timestamps are naive UTC).
        type=lambda s: to_naive_utc(datetime.fromisoformat(s)),
        help=(
            "ISO timestamp (UTC unless it has an offset); with a standalone --switch, "
            "re-queue memories written since; with --rollback-to, overrides the "
            "recorded switch time (context_search_configs.embedding_switched_at; for "
            "a context switched before v0.116.0, the search config's last change: "
            "pass this if the config was edited after the switch)"
        ),
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompts")
    args = parser.parse_args(argv)
    standalone = [
        name
        for name, value in (
            ("--rollback-to", args.rollback_to),
            ("--purge-source", args.purge_source),
        )
        if value
    ]
    if len(standalone) > 1:
        parser.error(f"{' and '.join(standalone)} are separate runs; pass one")
    if standalone:
        model = args.rollback_to or args.purge_source
        if model not in EMBEDDING_MODEL_REGISTRY:
            parser.error(f"unknown model for {standalone[0]}: {model}")
        if args.to or args.reembed or args.verify or args.switch or args.purge or args.run:
            parser.error(f"{standalone[0]} is a standalone run; drop the migration flags")
    elif not args.to:
        parser.error("--to is required unless --rollback-to or --purge-source is given")
    if args.purge and (args.reembed or args.run):
        parser.error("--purge is a separate step; run it after a switch has settled")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
