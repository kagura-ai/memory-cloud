"""Fold judge-verified older versions of a fact under the newer one (#2123, #2131).

The pure half of ``MemoryService._apply_supersede_fold``: which pool rows and
shadowed bridges are fold nodes, which links the resolved candidates and the
shadow map draw between them, and the grouping that rewrites the pool. No
database, no awaits — the service keeps the one I/O step (resolving the judged
nodes' stored candidates) and the fail-open around the whole fold.

Moved out of ``memory_service`` verbatim (#2131); see the service method's
docstring for the fold's rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from services.reranker_service import RERANK_UNSCORED_KEY
from services.supersede_verify import VERDICT_SUPERSEDES, stored_verification
from utils.logger import get_logger

if TYPE_CHECKING:
    from models.memory import Memory
    from models.schemas import SupersedeCandidate

logger = get_logger(__name__)

# #2123: the keys that place a row in the recall pool. A folded group's head
# takes them from the group's best-ranked row, whose place it takes.
FOLD_RANK_KEYS = ("score", "hybrid_score", "rerank_score", RERANK_UNSCORED_KEY)


def judged_supersedes(memory: Memory) -> bool:
    """Whether ``memory``'s stored candidate carries a ``supersedes`` verdict."""
    verification = stored_verification(memory.supersede_candidate)
    return verification is not None and (verification.get("verdict") == VERDICT_SUPERSEDES)


class FoldNodes(NamedTuple):
    """The memories a fold reasons about, keyed by ``Memory.id``."""

    pool: dict[UUID, Memory]
    """Hydrated pool rows."""
    bridges: dict[UUID, Memory]
    """Hydrated versions shadowing removed: link nodes, never rows."""
    judged: dict[UUID, Memory]
    """Pool rows and bridges with a ``supersedes`` verdict (resolved by the service)."""

    @property
    def nodes(self) -> dict[UUID, Memory]:
        """Every node a link may end on: pool rows and bridges."""
        return {**self.pool, **self.bridges}


def fold_nodes(
    search_results: list[dict],
    memories: Mapping[str, Memory],
    shadow_map: Mapping[UUID, UUID] | None,
) -> FoldNodes:
    """Collect the pool rows, the shadowed bridges and the judged nodes."""
    pool: dict[UUID, Memory] = {}
    for search_result in search_results:
        memory = memories.get(search_result["id"])
        if memory is not None:
            pool.setdefault(memory.id, memory)
    # Shadowed versions (hydrated, removed by shadowing): link nodes,
    # never rows. All of them, so a chain of accepted edges
    # (v4→v3→v2, v2 verified→v1) still reaches v1.
    bridges: dict[UUID, Memory] = {}
    for memory in memories.values():
        if shadow_map and memory.id in shadow_map and memory.id not in pool:
            bridges.setdefault(memory.id, memory)
    nodes = {**pool, **bridges}
    judged = {mid: m for mid, m in nodes.items() if judged_supersedes(m)}
    return FoldNodes(pool=pool, bridges=bridges, judged=judged)


def fold_links(
    fold: FoldNodes,
    candidates: Mapping[UUID, SupersedeCandidate],
    shadow_map: Mapping[UUID, UUID] | None,
) -> dict[UUID, set[UUID]]:
    """Newer → older links: shadowing edges into bridges, verified candidates."""
    bridges = fold.bridges
    nodes = fold.nodes
    links: dict[UUID, set[UUID]] = {}
    for dst, src in (shadow_map or {}).items():
        if dst in bridges and src in nodes:
            links.setdefault(src, set()).add(dst)
    for source_id, candidate in candidates.items():
        if candidate.verified is True and candidate.memory_id in nodes:
            if candidate.memory_id != source_id:
                links.setdefault(source_id, set()).add(candidate.memory_id)
    return links


def fold_linked_versions(
    search_results: list[dict],
    memories: Mapping[str, Memory],
    pool: Mapping[UUID, Memory],
    links: Mapping[UUID, set[UUID]],
) -> dict[UUID, list[UUID]]:
    """The grouping half of ``MemoryService._apply_supersede_fold`` (#2123).

    ``links`` maps a memory to the older memories a verdict or an edge
    says it supersedes; nodes outside ``pool`` only carry links. Computes
    everything before touching ``search_results``, which is replaced in
    one assignment at the end, so a failure part-way leaves the pool as
    it was.
    """

    def reachable(start: UUID) -> set[UUID]:
        seen: set[UUID] = set()
        stack = list(links.get(start, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(links.get(node, ()))
        return seen

    reach = {memory_id: reachable(memory_id) - {memory_id} for memory_id in pool}
    reached_by: dict[UUID, list[UUID]] = {}
    for source_id, targets in reach.items():
        for target_id in targets:
            if target_id in pool:
                reached_by.setdefault(target_id, []).append(source_id)
    heads = {memory_id for memory_id in pool if memory_id not in reached_by}

    def newest_key(memory_id: UUID) -> tuple[float, str]:
        created_at = pool[memory_id].created_at
        return (
            created_at.timestamp() if created_at is not None else float("-inf"),
            str(memory_id),
        )

    head_of: dict[UUID, UUID] = {}
    for folded_id, sources in reached_by.items():
        owners = [source for source in sources if source in heads]
        if owners:  # none: a cycle with no head outside it stays unfolded
            head_of[folded_id] = max(owners, key=newest_key)
    if not head_of:
        return {}
    fold_map: dict[UUID, list[UUID]] = {}
    for folded_id, head in head_of.items():
        fold_map.setdefault(head, []).append(folded_id)
    for head, members in fold_map.items():
        members.sort(key=newest_key, reverse=True)
        head_of[head] = head

    rows_of: dict[UUID, list[dict]] = {}
    for search_result in search_results:
        memory = memories.get(search_result["id"])
        if memory is not None and memory.id in head_of:
            rows_of.setdefault(head_of[memory.id], []).append(search_result)

    folded_results: list[dict] = []
    emitted: set[UUID] = set()
    for search_result in search_results:
        memory = memories.get(search_result["id"])
        head = head_of.get(memory.id) if memory is not None else None
        if head is None:
            folded_results.append(search_result)
            continue
        if head in emitted:
            continue
        emitted.add(head)
        rows = rows_of[head]
        # The head takes the place — and the ranking scores — of the
        # group's best-ranked row (this one): in a ranked pool that is
        # the group maximum, on the same scale as its neighbours (a
        # reranked pool can mix scored and unscored rows).
        head_row = dict(next(r for r in rows if memories[r["id"]].id == head))
        for key in FOLD_RANK_KEYS:
            if key in search_result:
                head_row[key] = search_result[key]
            else:
                head_row.pop(key, None)
        # The raw cosine is not a ranking key; confidence reads its
        # maximum for absence detection, which must not drop with a fold.
        raw = [
            r["semantic_score_raw"]
            for r in rows
            if isinstance(r.get("semantic_score_raw"), (int, float))
        ]
        if raw:
            head_row["semantic_score_raw"] = max(raw)
        folded_results.append(head_row)

    before = len(search_results)
    search_results[:] = folded_results
    logger.info(
        "supersede_fold_applied",
        folded=before - len(search_results),
        groups=len(fold_map),
    )
    return fold_map
