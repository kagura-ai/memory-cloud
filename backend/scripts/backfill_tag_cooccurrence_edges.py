#!/usr/bin/env python3
"""Backfill tag_cooccurrence edges across existing memories (Issue #223).

The migration ``b05_223_tag_cooccurrence`` adds the schema (GIN index +
extended CHECK + ``hub_tag_cache`` table). New memories created after the
deploy automatically get tag_cooccurrence seed edges via
``_create_tag_cooccurrence_seed_edges`` in ``memory_service.py``. This script
populates the same edges for memories that existed before the deploy.

Operational order (see ``docs/deployment.md``):

    1. Deploy code containing the new seeding function and config.
    2. Run ``alembic upgrade head`` to apply migration b05_223.
    3. Sleep Maintenance runs nightly and populates ``hub_tag_cache``
       (or run ``python -m scripts.refresh_hub_tag_cache`` ad hoc — TODO).
    4. Run this script per user / globally to backfill edges.

Idempotent: re-runs are safe. ``create_edge_if_absent`` (ON CONFLICT DO
NOTHING) prevents duplicates. The script also skips memories that already
have outgoing tag_cooccurrence edges as a fast-path.

Usage:

    # Dry-run for one user (preview counts only):
    python scripts/backfill_tag_cooccurrence_edges.py --user-id <uuid>

    # Execute for one user, batched 100 memories per commit:
    python scripts/backfill_tag_cooccurrence_edges.py \
        --user-id <uuid> --execute --batch-size 100

    # Execute for all users (use sparingly — long-running on large datasets):
    python scripts/backfill_tag_cooccurrence_edges.py --all-users --execute

    # Optional: scope further to one (workspace, context):
    python scripts/backfill_tag_cooccurrence_edges.py \
        --user-id <uuid> --context-id <uuid> --execute

Default mode is **dry-run**: passing ``--execute`` is required for any write.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

# Add src to path for imports (mirrors audit_edge_context_invariant.py).
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from db.base import _get_session_factory  # noqa: E402
from models.hub_tag import HubTagCache  # noqa: E402
from models.memory import Memory  # noqa: E402
from neural.config import NeuralMemoryConfig  # noqa: E402
from repositories.neural_edge import NeuralEdgeRepository  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


async def _hub_tags_for(session: AsyncSession, workspace_id: str, context_id: str) -> set[str]:
    """Read cached hub-tag set; empty set if never computed.

    Wraps the string args in ``UUID(...)`` so the comparison against the
    ``UUID(as_uuid=True)`` columns is unambiguous (matches the precedent
    in ``memory_service.py`` which compares against ORM-loaded UUID
    objects rather than string forms).
    """
    result = await session.execute(
        select(HubTagCache.hub_tags).where(
            HubTagCache.workspace_id == UUID(workspace_id),
            HubTagCache.context_id == UUID(context_id),
        )
    )
    row = result.scalar_one_or_none()
    return set(row) if row else set()


async def _backfill_one_memory(
    session: AsyncSession,
    *,
    memory: Memory,
    config: NeuralMemoryConfig,
    hub_tags: set[str],
    edge_repo: NeuralEdgeRepository,
    execute: bool,
) -> tuple[int, str]:
    """Compute and (optionally) write tag_cooccurrence edges for one memory.

    Returns ``(edges_count, status)`` where ``status`` is one of:

    - ``"no_tags"`` — memory has no tags (or no workspace/context set);
      skipped without DB work.
    - ``"already_seeded"`` — at least one tag_cooccurrence edge already
      exists for this memory; skipped via fast-path.
    - ``"no_candidates"`` — query ran but found no qualifying neighbors
      (all tags hub-excluded, or no cooccurring memories above
      min_shared).
    - ``"seeded"`` — at least one edge was created (or, in dry-run, would
      be). The ``edges_count`` is non-zero only in this case.
    """
    if not memory.tags or not memory.workspace_id or not memory.context_id:
        return 0, "no_tags"

    workspace_id_str = str(memory.workspace_id)
    context_id_str = str(memory.context_id)

    # Fast-path skip: already seeded.
    existing = await edge_repo.get_outgoing_edges(
        user_id=memory.user_id,
        src_id=memory.id,
        edge_types=["tag_cooccurrence"],
        workspace_id=workspace_id_str,
        context_id=context_id_str,
        limit=1,
    )
    if existing:
        return 0, "already_seeded"

    query_tags = [t for t in memory.tags if t not in hub_tags]
    if len(query_tags) < config.tag_cooccurrence_min_shared:
        return 0, "no_candidates"

    # Cast `tags` (varchar[]) to text[] to match :query_tags element type —
    # see memory_service.py companion SQL for the same fix (Copilot loop 1).
    sql = text(
        """
        SELECT id, cardinality(ARRAY(
            SELECT unnest(tags::text[])
            INTERSECT
            SELECT unnest(CAST(:query_tags AS text[]))
        )) AS shared_count
        FROM memories
        WHERE user_id = :user_id
          AND workspace_id = CAST(:workspace_id AS uuid)
          AND context_id = CAST(:context_id AS uuid)
          AND deleted_at IS NULL
          AND id != CAST(:self_id AS uuid)
          AND tags::text[] && CAST(:query_tags AS text[])
          AND cardinality(ARRAY(
              SELECT unnest(tags::text[])
              INTERSECT
              SELECT unnest(CAST(:query_tags AS text[]))
          )) >= :min_shared
        ORDER BY shared_count DESC, id
        LIMIT :max_matches
        """
    )
    res = await session.execute(
        sql,
        {
            "user_id": memory.user_id,
            "workspace_id": workspace_id_str,
            "context_id": context_id_str,
            "self_id": str(memory.id),
            "query_tags": query_tags,
            "min_shared": config.tag_cooccurrence_min_shared,
            "max_matches": config.tag_cooccurrence_max_per_remember,
        },
    )
    rows = res.all()
    if not rows:
        return 0, "no_candidates"

    if not execute:
        # Dry-run: count candidates that would actually create an edge (i.e.
        # not already present via knn or other path). Cheap approximation:
        # count rows; the real count after ON CONFLICT skips may be lower.
        return len(rows), "seeded"

    degree_cap = config.tag_cooccurrence_max_degree_per_node
    written = 0
    for row in rows:
        if written >= degree_cap:
            break
        try:
            neighbor_id = UUID(str(row.id))
            shared_count = int(row.shared_count)
        except (ValueError, TypeError, AttributeError):
            continue
        weight = min(0.4, 0.15 + 0.10 * (shared_count - 1))
        confidence = min(1.0, shared_count / 4.0)
        try:
            async with session.begin_nested():
                edge = await edge_repo.create_edge_if_absent(
                    user_id=memory.user_id,
                    src_id=memory.id,
                    dst_id=neighbor_id,
                    edge_type="tag_cooccurrence",
                    weight=weight,
                    confidence=confidence,
                    workspace_id=workspace_id_str,
                    context_id=context_id_str,
                )
            if edge is not None:
                written += 1
        except Exception as e:
            logger.warning(
                "backfill_edge_failed",
                memory_id=str(memory.id),
                neighbor_id=str(row.id),
                error=str(e),
            )
    return written, ("seeded" if written > 0 else "no_candidates")


async def backfill(
    *,
    user_id: str | None,
    context_id: str | None,
    all_users: bool,
    execute: bool,
    batch_size: int,
) -> dict[str, Any]:
    """Iterate memories in scope and backfill tag_cooccurrence edges.

    Memory iteration is keyset-paginated by ``id`` (stable ordering) so the
    script is resumable across crashes — restart with the same args and any
    already-seeded memories will be fast-path skipped on the second pass.
    """
    session_factory = _get_session_factory()

    counts = {
        "memories_scanned": 0,
        "memories_skipped_no_tags": 0,
        "memories_already_seeded": 0,
        "memories_no_candidates": 0,
        "memories_seeded": 0,
        "edges_created": 0,
        "users_seen": set(),
    }
    last_id = None

    async with session_factory() as session:
        config = await NeuralMemoryConfig.from_db(session)

        # Hub-tag cache reads are scoped per (workspace, context); the cache
        # itself is shared across users in the same context.
        hub_cache: dict[tuple[str, str], set[str]] = {}

        edge_repo = NeuralEdgeRepository(session)

        while True:
            stmt = (
                select(Memory)
                .where(Memory.deleted_at.is_(None))
                .where(Memory.tags.isnot(None))
                .where(Memory.workspace_id.isnot(None))
                .where(Memory.context_id.isnot(None))
                .order_by(Memory.id)
                .limit(batch_size)
            )
            if user_id and not all_users:
                stmt = stmt.where(Memory.user_id == user_id)
            if context_id:
                stmt = stmt.where(Memory.context_id == UUID(context_id))
            if last_id is not None:
                stmt = stmt.where(Memory.id > last_id)

            res = await session.execute(stmt)
            batch = list(res.scalars().all())
            if not batch:
                break

            for memory in batch:
                counts["memories_scanned"] += 1
                last_id = memory.id
                counts["users_seen"].add(memory.user_id)

                if not memory.tags:
                    counts["memories_skipped_no_tags"] += 1
                    continue

                ws_str = str(memory.workspace_id)
                ctx_str = str(memory.context_id)
                cache_key = (ws_str, ctx_str)
                if cache_key not in hub_cache:
                    hub_cache[cache_key] = await _hub_tags_for(session, ws_str, ctx_str)

                created, status = await _backfill_one_memory(
                    session,
                    memory=memory,
                    config=config,
                    hub_tags=hub_cache[cache_key],
                    edge_repo=edge_repo,
                    execute=execute,
                )
                counts["edges_created"] += created
                if status == "already_seeded":
                    counts["memories_already_seeded"] += 1
                elif status == "no_candidates":
                    counts["memories_no_candidates"] += 1
                elif status == "seeded":
                    counts["memories_seeded"] += 1
                elif status == "no_tags":
                    # Should not happen — caller already filtered, but harmless
                    counts["memories_skipped_no_tags"] += 1

            if execute:
                await session.commit()

            logger.info(
                "backfill_batch_complete",
                last_id=str(last_id) if last_id else None,
                memories_scanned=counts["memories_scanned"],
                edges_created=counts["edges_created"],
                execute=execute,
            )

    counts["users_seen"] = len(counts["users_seen"])  # type: ignore[assignment]
    return counts


def print_report(counts: dict[str, Any], *, execute: bool) -> None:
    """Render the run summary."""
    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"\n=== tag_cooccurrence backfill ({mode}) ===")
    print(f"  Memories scanned:           {counts['memories_scanned']}")
    print(f"  Skipped (no tags):          {counts['memories_skipped_no_tags']}")
    print(f"  Already seeded (skipped):   {counts['memories_already_seeded']}")
    print(f"  No candidates found:        {counts['memories_no_candidates']}")
    print(
        f"  Seeded {'(this run)' if execute else '(would seed)'}:           {counts['memories_seeded']}"
    )
    print(f"  Distinct users touched:     {counts['users_seen']}")
    if execute:
        print(f"  Edges created:              {counts['edges_created']}")
    else:
        print(f"  Edges that WOULD be created (upper bound): {counts['edges_created']}")
        print("  Re-run with --execute to write.")


async def main_async(args: argparse.Namespace) -> int:
    if not args.user_id and not args.all_users:
        print(
            "error: pass either --user-id <uuid> or --all-users",
            file=sys.stderr,
        )
        return 2
    if args.user_id and args.all_users:
        print(
            "error: --user-id and --all-users are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    counts = await backfill(
        user_id=args.user_id,
        context_id=args.context_id,
        all_users=args.all_users,
        execute=args.execute,
        batch_size=args.batch_size,
    )
    print_report(counts, execute=args.execute)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill tag_cooccurrence edges (Issue #223)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    scope = parser.add_mutually_exclusive_group(required=False)
    scope.add_argument("--user-id", help="Restrict to one user UUID")
    scope.add_argument("--all-users", action="store_true", help="Process every user")
    parser.add_argument("--context-id", help="Further restrict to one context UUID (optional)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write edges. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Memories per commit batch (default: 200)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
