"""Bounded, source-checked candidate expansion for recall (#2728)."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import UUID

from sqlalchemy import select

from models.memory import Memory
from services.memory_listing import compile_memory_filters, recall_row_predicates
from services.sleep.related_index import (
    MAX_ADDITIONS,
    MAX_SEEDS,
    candidate_links,
    current,
    index_enabled,
)
from utils.logger import get_logger
from utils.tag_normalize import TAG_GROUPS_FILTER_KEY

logger = get_logger(__name__)


async def expand_recall(
    service: Any,
    *,
    request: Any,
    hits: list[dict],
    memories: dict[str, Memory],
    user_id: str,
    context_id: UUID,
    workspace_id: UUID,
    filters: dict | None,
    cluster_memory_ids: list[UUID] | None,
) -> tuple[list[dict], dict[str, Memory]]:
    """Return eligible new candidates atomically, or an empty baseline fallback.

    Unsupported filters disable expansion instead of widening the query.
    The independent read session contains database failures/cancellation without
    invalidating the caller's transaction.
    """
    if not index_enabled(reading=True) or not hits or request.search_mode == "keyword":
        return [], {}
    from services.agent_binding_service import filter_memory_rows_by_binding
    from services.permission_service import CallerId, MemoryAuthorId, PermissionService

    try:
        timeout_ms = min(
            500, max(25, int(os.environ.get("RECALL_RELATED_INDEX_TIMEOUT_MS", "150")))
        )
        # Scope and cluster membership are already resolved by recall; apply
        # them explicitly below rather than sending them to the row compiler.
        handled_keys = {
            "tags",
            "tags_match",
            "tags_normalize",
            TAG_GROUPS_FILTER_KEY,
            "context_id",
            "workspace_id",
            "analysis_cluster",
        }
        predicates = recall_row_predicates(filters) + compile_memory_filters(
            {key: value for key, value in (filters or {}).items() if key not in handled_keys}
        )
        async with service._isolated_session() as db, asyncio.timeout(timeout_ms / 1000):
            seed_hits = hits[:MAX_SEEDS]
            seeds = [memories[h["id"]] for h in seed_hits if h["id"] in memories]
            links = await candidate_links(
                db, seeds, context_id=context_id, workspace_id=workspace_id
            )
            if not links:
                return [], {}
            seed_map = {m.id: m for m in seeds}
            pool_ids = {m.id for m in memories.values()}
            target_ids = {
                node for link in links for node in (link.source_id, link.target_id)
            } - pool_ids
            rows = list(
                (
                    await db.scalars(
                        select(Memory).where(
                            Memory.id.in_(target_ids),
                            Memory.workspace_id == workspace_id,
                            Memory.context_id == context_id,
                            Memory.deleted_at.is_(None),
                            *predicates,
                            *(
                                [Memory.id.in_(cluster_memory_ids)]
                                if cluster_memory_ids is not None
                                else []
                            ),
                        )
                    )
                ).all()
            )
            rows, _ = await filter_memory_rows_by_binding(
                db, rows, operation="recall", user_id=user_id
            )
            endpoints = {**seed_map, **{m.id: m for m in rows}}
            scores = {
                memories[h["id"]].id: h.get("hybrid_score", h["score"])
                for h in seed_hits
                if h["id"] in memories
            }
            extras: list[dict] = []
            extra_memories: dict[str, Memory] = {}
            permission = PermissionService(db)
            for link in links:
                source, target = endpoints.get(link.source_id), endpoints.get(link.target_id)
                if source is None or target is None or not current(link, source, target):
                    continue
                seed, candidate = (source, target) if source.id in seed_map else (target, source)
                if candidate.id in pool_ids or str(candidate.id) in extra_memories:
                    continue
                if not await permission.can_access_memory(
                    user_id=CallerId(user_id),
                    memory_user_id=MemoryAuthorId(candidate.user_id),
                    workspace_id=candidate.workspace_id,
                    context_id=candidate.context_id,
                    operation="recall",
                    memory_id=candidate.id,
                    memory_type=candidate.type,
                    memory_source_type=candidate.source_type,
                ):
                    continue
                score = scores[seed.id] * 0.9
                extras.append(
                    {
                        "id": str(candidate.id),
                        "score": score,
                        "hybrid_score": score,
                        "_related_index": True,
                        "payload": {"context_id": str(context_id)},
                    }
                )
                extra_memories[str(candidate.id)] = candidate
                if len(extras) >= MAX_ADDITIONS:
                    break
            # The caller runs normal shadowing/folding once on the combined
            # pool so cross-pool relations and annotations stay intact.
            return extras, extra_memories
    except Exception as exc:
        logger.info("recall_related_index_skipped", error_type=type(exc).__name__)
        return [], {}
