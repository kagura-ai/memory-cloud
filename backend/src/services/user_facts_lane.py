"""The user facts lane of recall (#3014).

A fact about the user that matters for an answer is often unrelated to the
question in wording ("how do I carry these bags upstairs?" against a knee
injury stated days ago), so the ranked search for the question leaves it out.
When a context's ``user_facts_lane`` switch is on, :meth:`MemoryService.recall`
(for the MCP ``recall`` tool and ``POST /api/v1/memory/recall`` only) runs one
more search over the context's ``fact`` memories with a fixed query and returns
up to :data:`USER_FACTS_ROWS` of them, those not already in ``results``, under
``user_facts``.

The lane reads and never writes. Compared with a normal recall it:

* embeds its fixed query once per embedding provider, model and dimensions (a process-level
  cache, :data:`_QUERY_VECTORS`, over the embedding service's shared cache) and
  writes no llm_call_log row of its own;
* never reranks, so it calls no reranker and checks no plan feature for one;
* hides superseded and folded rows as a normal recall does, but resolves no
  supersede suggestion for its rows;
* records no access (no access counters, no ``last_accessed``, no promotion),
  runs no Hebbian pass (no association or co-activation edge), writes no
  memory access event, no would-deny audit row and no usage row, and adds no
  hint, related tag or explore hint;
* runs in a session of its own, after the recall's transaction is committed,
  within :data:`USER_FACTS_TIMEOUT_SECONDS`. Any error or timeout is logged once
  as ``user_facts_lane_failed`` and the recall returns without the key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from db.base import isolated_session
from models.memory import SOURCE_TYPE_CONNECTOR, Memory
from models.schemas import MemoryResponse, RecallRequest, RecallResponse
from utils.event_dates import event_row_kwargs
from utils.logger import get_logger
from utils.observed_at import observed_at_from_details
from utils.recall_projection import content_slice, effective_full

if TYPE_CHECKING:
    from services.memory_service import MemoryService

logger = get_logger(__name__)

# The lane's query: fixed, not a setting and not per context.
USER_FACTS_QUERY = (
    "facts about the user: health and physical limits, circumstances, constraints, needs"
)
# The most rows the lane returns.
USER_FACTS_ROWS = 2
# Hits fetched past the rows wanted: superseded or folded versions, rows a
# binding denies and rows already on the page take places in the ranked list
# without becoming rows, so the lane looks further down than it returns.
USER_FACTS_POOL_MARGIN = 10
# The memory type the lane searches.
USER_FACTS_TYPE = "fact"
# The lane's time limit. Past it the recall returns without ``user_facts``.
USER_FACTS_TIMEOUT_SECONDS = 5.0
# The caller's filters that restrict who or what may be read; they hold in the
# lane. The others (tags, importance, scope, dates, location, cluster, score
# threshold) narrow the question's topic and do not apply to the lane's query.
_ACCESS_FILTER_KEYS = ("trust_tier", "source_type", "source_uri_prefix")

# (embedding provider, model, dimensions) → the vector of USER_FACTS_QUERY. Per process;
# a new worker embeds the query once (a hit in the embedding service's shared
# cache when another worker already did).
_QUERY_VECTORS: dict[tuple[str, str, int], list[float]] = {}


def lane_applies(
    request: RecallRequest,
    *,
    search_config: Any,
    context_ids: list[UUID] | None,
    selection: bool,
    response: RecallResponse,
) -> bool:
    """Whether the lane runs for this recall (the caller opted in).

    Read from what the recall already holds: the context's search config it
    loaded at the start (no statement of its own) and the request. Skipped
    when the switch is off, the recall spans several contexts, filters by
    ``type`` (it already chose the kind of memory), orders by date (a change
    history, not a ranked page), carries a selection plan (the page is
    chosen by the caller, not ranked), or was served without the semantic arm (a keyword search for the
    fixed query has no meaning).
    """
    if search_config is None or getattr(search_config, "user_facts_lane", False) is not True:
        return False
    if context_ids or selection:
        return False
    if "type" in (request.filters or {}):
        return False
    if request.order != "relevance":
        return False
    return not response.degraded


def lane_filters(request: RecallRequest) -> dict[str, Any]:
    """The lane's filters: the caller's access filters and ``type == 'fact'``."""
    filters = request.filters or {}
    lane = {key: filters[key] for key in _ACCESS_FILTER_KEYS if key in filters}
    lane["type"] = USER_FACTS_TYPE
    return lane


async def attach_user_facts(
    service: MemoryService,
    response: RecallResponse,
    *,
    request: RecallRequest,
    user_id: str,
    context_id: UUID,
    workspace_id: UUID,
    is_shared_context_read: bool,
    content_trusted_only: bool,
) -> RecallResponse:
    """``response`` with ``user_facts`` set, or unchanged when the lane failed.

    Never raises: an error or a timeout is logged once and the recall's own
    response goes back without the key.
    """
    # The recall's transaction holds a pooled connection; release it before
    # the lane takes one of its own (read-only here: the recall committed its
    # writes, or made none).
    try:
        await service.db.commit()
        rows = await asyncio.wait_for(
            _search(
                service,
                exclude=_page_ids(response),
                main_facts=sum(1 for item in response.results if item.type == USER_FACTS_TYPE),
                request=request,
                user_id=user_id,
                context_id=context_id,
                workspace_id=workspace_id,
                is_shared_context_read=is_shared_context_read,
                content_trusted_only=content_trusted_only,
            ),
            timeout=USER_FACTS_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — the lane never breaks a recall
        logger.warning(
            "user_facts_lane_failed",
            context_id=str(context_id),
            error_class=type(exc).__name__,
        )
        return response
    response.user_facts = rows
    return response


def _page_ids(response: RecallResponse) -> set[UUID]:
    """The memories the page already shows: its rows and the older versions
    folded under them (a folded version is not a row of its own anywhere)."""
    ids: set[UUID] = set()
    for item in response.results:
        ids.add(item.memory_id)
        if item.folded is not None:
            ids.update(item.folded.memory_ids)
    return ids


async def _search(
    service: MemoryService,
    *,
    exclude: set[UUID],
    main_facts: int,
    request: RecallRequest,
    user_id: str,
    context_id: UUID,
    workspace_id: UUID,
    is_shared_context_read: bool,
    content_trusted_only: bool,
) -> list[MemoryResponse]:
    """The lane's search, hydration and rows, in a session of its own."""
    filters = lane_filters(request)
    # Rows of the page that are facts may come back here too; fetch past them
    # and past the hits that never become rows.
    k = USER_FACTS_ROWS + main_facts + USER_FACTS_POOL_MARGIN
    lane_request = request.model_copy(
        update={"filters": filters, "k": k, "include_superseded": False}
    )
    async with isolated_session(service.db) as lane_db:
        lane = type(service)(lane_db)
        degradation: dict[str, Any] = {}
        hits = await lane.search_service.hybrid_search(
            query=USER_FACTS_QUERY,
            user_id=user_id,
            workspace_id=str(workspace_id),
            context_id=str(context_id),
            k=k,
            use_rerank=False,
            filters=filters,
            search_mode="hybrid",
            is_shared_context_read=is_shared_context_read,
            degradation=degradation,
            query_vectors=_QUERY_VECTORS,
        )
        if degradation.get("degraded"):
            raise RuntimeError("user facts lane served without the semantic arm")
        if not hits:
            return []
        memories = await lane._recall_hydrate_memories(
            lane_request,
            search_results=hits,
            searched_context_id=str(context_id),
            cluster_memory_ids=None,
        )
        memories = await _binding_filter(lane_db, hits, memories, user_id=user_id)
        shadow_map, contradiction_map = await lane._apply_supersede_shadowing(
            hits, memories, include_superseded=False
        )
        await lane._apply_supersede_fold(
            hits,
            memories,
            include_superseded=False,
            user_id=user_id,
            shadow_map=shadow_map,
            filters=filters,
        )
        picked: list[tuple[dict, Memory]] = []
        seen: set[UUID] = set()
        for hit in hits:
            memory = memories.get(hit["id"])
            if memory is None or memory.id in exclude or memory.id in seen:
                continue
            if memory.type != USER_FACTS_TYPE:
                continue
            seen.add(memory.id)
            picked.append((hit, memory))
            if len(picked) == USER_FACTS_ROWS:
                break
        # Content follows the recall's own rule (#2124): none unless the call
        # carries content at all (``full`` of 0 or unset ships none); with a
        # defaulted ``full`` only trusted rows are eligible.
        with_content = effective_full(request.full, request.k) >= 1
        trusted: set[UUID] | None = None
        if (
            picked
            and with_content
            and content_trusted_only
            and (filters.get("trust_tier") != "trusted")
        ):
            trusted = await lane._recall_trusted_context_ids({m.context_id for _, m in picked})
        rows = [
            _row(
                hit,
                memory,
                contradiction_map,
                with_content=with_content,
                trusted_context_ids=trusted,
            )
            for hit, memory in picked
        ]
        await lane_db.rollback()
        return rows


async def _binding_filter(
    db: Any, hits: list[dict], memories: dict[str, Memory], *, user_id: str
) -> dict[str, Memory]:
    """The #1299 per-memory binding filter, without the would-deny audit row.

    ``operation=None`` keeps a shadow-mode binding log-only, so the lane writes
    nothing; an enforcing binding drops the rows it denies, as in recall.
    """
    if not memories:
        return memories
    from services.agent_binding_service import filter_memory_rows_by_binding

    rows = list({m.id: m for m in memories.values()}.values())
    kept, denied = await filter_memory_rows_by_binding(db, rows, operation=None, user_id=user_id)
    if not denied:
        return memories
    kept_ids = {m.id for m in kept}
    hits[:] = [h for h in hits if h["id"] not in memories or memories[h["id"]].id in kept_ids]
    return {key: m for key, m in memories.items() if m.id in kept_ids}


def _row(
    hit: Mapping[str, Any],
    memory: Memory,
    contradiction_map: Mapping[UUID, list[UUID]],
    *,
    with_content: bool,
    trusted_context_ids: set[UUID] | None,
) -> MemoryResponse:
    """One lane row: the recall row shape, with its dates and, where the
    recall carries content, its content.

    Content follows recall's rule (#2124): none when the call ships none
    (``with_content`` False); with ``trusted_context_ids`` set (a defaulted
    ``full``), only a row of a trusted context that no connector wrote
    carries it.
    """
    eligible = with_content and (
        trusted_context_ids is None
        or (
            memory.source_type != SOURCE_TYPE_CONNECTOR and memory.context_id in trusted_context_ids
        )
    )
    content: str | None = None
    content_next_offset: int | None = None
    if eligible and memory.content:
        content, content_next_offset = content_slice(memory.content)
    return MemoryResponse(
        memory_id=memory.id,
        summary=memory.summary,
        context_summary=memory.context_summary,
        type=memory.type,
        importance=memory.importance,
        scope=memory.scope,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
        observed_at=observed_at_from_details(memory.details),
        **event_row_kwargs(memory.details),
        client=memory.client,
        tags=memory.tags or [],
        context=memory.context,
        score=hit.get("hybrid_score", hit["score"]),
        source_uri=memory.source_uri,
        source_type=memory.source_type,
        contradicts=contradiction_map.get(memory.id, []),
        version=memory.version,
        content=content,
        content_truncated=True if content_next_offset is not None else None,
        content_next_offset=content_next_offset,
    )
