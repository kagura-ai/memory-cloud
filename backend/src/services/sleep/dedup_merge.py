"""Sleep Maintenance Phase 2: Dedup/Merge.

Issue #101: Detect and merge duplicate memories using cosine similarity
clustering and optional LLM judgment.

Algorithm:
1. For each memory, find high-similarity neighbors in Qdrant (>= threshold)
2. Build candidate pairs, cluster with Union-Find (cap cluster size at 5);
   oversize clusters are re-split into <=5-member subclusters by descending
   pair similarity (#1184) instead of deferred wholesale
3. LLM on: batch judgment (merge/keep_both)
4. LLM off: similarity >= 0.98 → auto-merge, else → flag
5. Merge: keep winner, soft-delete losers, transfer edges, merge tags; a loser
   carrying an exempt tag (#2584, ``sleep_archive_exempt_tags``) is never
   retired — that pair is skipped

Academic notes:
- Union-Find transitivity: A~B + B~C clusters all three even if A~C is low.
  Cluster size cap (5) + LLM second gate mitigate runaway merges. Templated
  corpora collapse into one mega-cluster (Day-5: 782 pairs → 1 cluster);
  capacity-capped greedy agglomeration (#1184) restores partial progress.
- Positional bias: batch order is shuffled before LLM calls.
- ID hallucination: short labels (A, B, C) used in prompts, mapped back to UUIDs.
"""

from __future__ import annotations

import math
import random
import string
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepReporter

from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from db.qdrant import retrieve_dense_vectors, search_memories_qdrant
from models.memory import (
    DELETED_BY_SLEEP_MERGE,
    MEMORY_TYPE_TIME,
    Memory,
    not_pinned_predicate,
    not_resource_owned_predicate,
    not_tool_triggered_predicate,
    pinned_predicate,
    resource_owned_predicate,
    tool_triggered_predicate,
)
from repositories.neural_edge import NeuralEdgeRepository
from services.deferred_point_deletes import PointDelete, queue_point_deletes
from services.embedding_service import EmbeddingService
from services.llm_service import LLMService, billed_usage
from services.resource_indexer import unshared_point_id
from services.sleep.coverage import (
    BUDGET,
    FAILURE,
    PASSED,
    SETTLED,
    DedupCoverage,
    DedupLanes,
    fetch_window,
    sweep_call_share,
)
from services.sleep.judge_lane import judge_platform_only
from services.sleep.prompts import (
    DEDUP_JUDGE_SYSTEM,
    DEDUP_JUDGE_USER,
    wrap_untrusted_content,
)
from services.sleep.reporter import (
    EmbeddingSpendTracker,
    LLMCallBreakdown,
    PhaseResult,
    SleepBudget,
    accumulate_llm_response,
    phase_outcome,
)
from utils.datetime import utcnow
from utils.exceptions import (
    ConfigurationError,
    EmbeddingSpendCapExceeded,
    FeatureNotAvailableError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Per-memory Qdrant neighbour cap for candidate discovery (#1231 documents the
# saturation it can hit). #1523 widens it by the pinned rows in scope, bounded so
# a context with thousands of pins cannot turn every search into a full scan.
NEIGHBOR_LIMIT = 10
MAX_EXTRA_NEIGHBORS = 50

# Maximum cluster size to process in one run.
# Larger clusters are deferred to the next sleep cycle.
MAX_CLUSTER_SIZE = 5

# #3019: ids per statement when checking a search hit outside the window, and
# per statement and vector read when loading a cluster's outside members.
ELIGIBILITY_CHECK_CHUNK = 1000
MEMBER_LOAD_CHUNK = 100

# #3019: embedding refusals that are never one row's own — the workspace's
# embedding spend cap, a provider key or a plan that is not there. A row
# refused this way holds its lane like an outage, whatever else answered.
_REFUSALS_NOT_ROW_SPECIFIC: tuple[type[Exception], ...] = (
    EmbeddingSpendCapExceeded,
    ConfigurationError,
    FeatureNotAvailableError,
)


def dedup_eligible(
    user_id: str, workspace_id: str | None, context_id: str | None
) -> ColumnElement[bool]:
    """The one rule for which memories dedup may compare and merge (#3019).

    The run's user's live rows in the run's workspace and context, minus the
    deterministic lanes: pinned rows (#1519, ``load_pinned()``), tool
    guardrails (``load_guardrails``), time memories (#1524,
    ``recall_upcoming``) and rows the resource indexer owns (#2996). The
    window fetch, the check of a search hit outside the window and the load
    of a cluster's outside members all build their WHERE from this function,
    so a row the window would exclude can never come in as a neighbour. No
    age, scope or adoption gate: a memory of any age can lose a merge to a
    newly written near-duplicate.
    """
    return and_(
        Memory.user_id == user_id,
        Memory.deleted_at.is_(None),
        # #1523: the shared exemption every automated deleter uses.
        not_pinned_predicate(),
        # Tool guardrails: same exemption, the guardrail lane.
        not_tool_triggered_predicate(),
        # #1524: time memories are served by their trigger window, not their
        # summary — a same-summary occurrence is not a duplicate.
        Memory.type != MEMORY_TYPE_TIME,
        # #2996: a row the resource indexer owns is the source's: two
        # documents that read alike are two records, and the source alone
        # removes either.
        not_resource_owned_predicate(),
        Memory.workspace_id == UUID(workspace_id) if workspace_id else true(),
        Memory.context_id == UUID(context_id) if context_id else true(),
    )


def _pair_key(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Canonical unordered key for a memory pair.

    Ordered by ``str`` so ``(a, b)`` and ``(b, a)`` collide on one entry.

    The 2-tuple return type is the point (#1456 review): built inline as
    ``tuple(sorted([...]))`` the keys infer as ``tuple[UUID, ...]``, under which
    a future 1- or 3-tuple key type-checks and then silently misses every
    lookup — ``pair_scores.get(key, 0.0)`` would hand the judge and the audit
    row a 0.0 that looks like a real score.
    """
    x, y = sorted([a, b], key=str)
    return x, y


def _fmt_minute(dt: datetime | None) -> str | None:
    """Minute-precision recency string, defined once (#1198/#1209/#1229).

    The audit's winner/loser recency must stay byte-comparable with the
    ``last_updated=`` the judge prompt shows — one shared formatter keeps
    the post-hoc verification honest.
    """
    return f"{dt:%Y-%m-%d %H:%M}" if dt else None


# Similarity threshold for auto-merge without LLM (LLM-off mode)
AUTO_MERGE_THRESHOLD = 0.98


class UnionFind:
    """Union-Find (disjoint set) with path compression and union by rank.

    Used to cluster memories that are pairwise similar above threshold.
    """

    def __init__(self) -> None:
        self.parent: dict[UUID, UUID] = {}
        self.rank: dict[UUID, int] = {}

    def find(self, x: UUID) -> UUID:
        """Find root with path compression."""
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: UUID, y: UUID) -> None:
        """Union by rank."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1

    def clusters(self) -> list[set[UUID]]:
        """Return all clusters as list of sets."""
        groups: dict[UUID, set[UUID]] = {}
        for x in self.parent:
            root = self.find(x)
            groups.setdefault(root, set()).add(x)
        return list(groups.values())


def _split_oversize_cluster(
    cluster: set[UUID],
    pairs: list[tuple[UUID, UUID, float]],
    max_size: int = MAX_CLUSTER_SIZE,
) -> tuple[list[set[UUID]], int]:
    """Split an oversize union-find cluster into judgeable subclusters (#1184).

    Templated corpora (daily standups, status logs) push cross-unit similarity
    over the candidate threshold, union-find collapses everything into one
    mega-cluster (Day-5 eval: 782 pairs → 1 cluster), and the old wholesale
    deferral disabled dedup for exactly the corpora that need it most.

    Greedy capacity-capped agglomeration: walk the cluster's internal pairs in
    DESCENDING similarity and union two components only while the merged size
    stays within ``max_size``. The tightest duplicates coalesce first, every
    component stays judgeable in one LLM batch, and skipped cross-component
    pairs are re-candidates next run (post-merge, the landscape shrinks).
    Deterministic: pair endpoints are CANONICALIZED (str-ascending) before
    ties break on the stringified ids — ``_find_similar_pairs`` emits
    ``(memory.id, hit_id)`` in Qdrant-iteration orientation, so sorting on
    the raw orientation would make equal-score splits vary run-to-run
    (Copilot, PR #1188).

    Args:
        cluster: member ids of one oversize union-find cluster.
        pairs: ALL candidate pairs from the run; only pairs internal to
            ``cluster`` are considered.
        max_size: per-subcluster member cap (defaults to MAX_CLUSTER_SIZE).

    Returns:
        (subclusters with >= 2 members, count of union attempts blocked by
        the size cap — an order-of-magnitude PROXY for candidate pairs left
        unjudged this run, not an exact count of final cross-subcluster
        edges: an early cap-blocked pair may still end up same-component via
        other edges, and same-component skips are never counted).
    """

    def _ordered(pair: tuple[UUID, UUID, float]) -> tuple[UUID, UUID, float]:
        # Explicit 3-tuple instead of ``(*sorted(...), score)``: the splat form
        # widens every slot to ``UUID | float``, which makes ``-p[2]`` untypable
        # even though the score is always a float (#1442).
        a, b = sorted((pair[0], pair[1]), key=str)
        return a, b, pair[2]

    internal = sorted(
        (_ordered(p) for p in pairs if p[0] in cluster and p[1] in cluster),
        key=lambda p: (-p[2], str(p[0]), str(p[1])),
    )

    parent: dict[UUID, UUID] = {m: m for m in cluster}
    comp_size: dict[UUID, int] = dict.fromkeys(cluster, 1)

    def find(x: UUID) -> UUID:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    skipped_pairs = 0
    for id_a, id_b, _score in internal:
        root_a, root_b = find(id_a), find(id_b)
        if root_a == root_b:
            continue
        if comp_size[root_a] + comp_size[root_b] > max_size:
            skipped_pairs += 1
            continue
        parent[root_b] = root_a
        comp_size[root_a] += comp_size[root_b]

    groups: dict[UUID, set[UUID]] = {}
    for member in cluster:
        groups.setdefault(find(member), set()).add(member)
    return [g for g in groups.values() if len(g) >= 2], skipped_pairs


@dataclass
class _MergeTally:
    """Per-run merge outcome counters (#1456).

    These were five loose locals threaded through a 197-line loop. They are
    NOT interchangeable and the details payload keeps them apart on purpose:
    ``guarded`` is a pure sub-threshold veto (#1229), ``unverifiable`` means no
    score could be computed at all (#1231, fail-closed), ``rescued`` counts
    pairs the top-10 candidate cap would have false-positive vetoed, and
    ``settled_skipped`` counts judge re-nominations of pairs a supersedes edge
    already settled (#1232). Folding any of them together would destroy the
    signal the others exist to isolate.
    """

    merged: int = 0
    guarded: int = 0
    rescued: int = 0
    unverifiable: int = 0
    settled_skipped: int = 0
    # #1519: pairs whose winner or loser was pinned (delivery_mode='always'),
    # deleted or gone between the candidate fetch and merge execution — the
    # execution-time recheck refused them (neither mode mutated anything).
    skipped_pinned: int = 0
    # #2996: pairs the same recheck refused because a side is a row the
    # resource indexer owns (counted here, not in ``skipped_pinned``, when any
    # reason of the refusal is that one).
    skipped_resource_owned: int = 0
    # #2584: eligible merges not planned because the loser carries an exempt
    # tag (an open review memo). Kept apart from the vetoes above: the judge
    # was right that the pair is a duplicate.
    skipped_exempt: int = 0


class DedupMergePhase(EmbeddingSpendTracker):
    """Detect and merge duplicate memories."""

    # #3019: what ``_find_similar_pairs`` saw per window row, read by
    # ``execute`` for ``details.no_action_reasons``. Class defaults so a
    # stubbed search (many tests) reads zeros.
    _rows_without_neighbour: int = 0
    _rows_search_failed: int = 0
    _hits_not_eligible: int = 0
    # #3019: the window rows whose embed and search succeeded (None: the
    # search was stubbed, every row counts as searched).
    _searched_ok: set[UUID] | None = None
    # #3019: the step each failed window row failed at ("embed", "search", or
    # "refused" for a refusal that is never the row's own), and how many
    # embeds the provider answered. A vector from the cache says nothing about
    # the provider, so it is no evidence that a failed embed was the row's own.
    _search_failures: dict[UUID, str] | None = None
    _provider_embeds: int = 0

    def __init__(
        self,
        db: AsyncSession,
        llm_service: LLMService,
        embedding_model: str | None = None,
        collection_name: str | None = None,
        *,
        coverage: DedupLanes | None = None,
    ):
        self.db = db
        self.llm_service = llm_service
        self.edge_repo = NeuralEdgeRepository(db)
        self.embedding_service = EmbeddingService(db, model=embedding_model)
        self.collection_name = collection_name
        # #3019: the partition's lane positions as the last run left them
        # (None: a first run), and this run's window with each row's outcome,
        # which the orchestrator turns into the next positions. A stubbed
        # fetch leaves the window None: rows are judged in list order and the
        # positions are carried unchanged.
        self._lanes = coverage or DedupLanes()
        self.coverage_outcome: DedupCoverage | None = None
        # #475: embedding cost-grade accumulators (init here so
        # ``_find_similar_pairs`` can be unit-tested without execute()).
        self._embedding_calls_used: int = 0
        self._embedding_tokens_used: int = 0
        # #1183: judge-failure counter (init here so ``_llm_judge`` can be
        # unit-tested without execute()).
        self._llm_failures: int = 0
        # #2996: recheck refusals with a resource-owned side (init here so
        # the executors can be unit-tested without execute()).
        self._resource_owned_refusals: int = 0
        # #1195/#1198: newer-wins override counter (init here so
        # ``_judge_cluster`` can be unit-tested without execute()).
        self._winner_overrides: int = 0
        # #1209: per-reason override breakdown ("recency" | "source") and
        # per-decision audit metadata (merge_reason from the judge, override
        # info), keyed by the FINAL (winner_id, loser_id). Kept beside the
        # tuple-shaped decision lists so the public decision shape — which
        # existing tests and call sites rely on — stays unchanged.
        self._override_reasons: dict[str, int] = {}
        self._decision_meta: dict[tuple[UUID, UUID], dict[str, Any]] = {}
        # #1231: per-run summary-vector cache filled by
        # ``_find_similar_pairs`` (init here so the veto's on-demand direct
        # check can be unit-tested without execute()). Keyed by memory id;
        # missing entries mean the embed failed and the pair stays
        # unverifiable (fail-closed).
        self._summary_vectors: dict[UUID, list[float]] = {}
        # #1232: pair keys already settled by a supersedes edge, recorded
        # by ``_filter_already_superseded_pairs`` (shadow mode only). The
        # judge can re-nominate these via third-party co-clustering; they
        # must be skipped — never guarded, never rescued into a re-merge.
        self._settled_pair_keys: set[tuple[UUID, UUID]] = set()
        # #2565: the merge losers' points, queued in the phase's savepoint;
        # the orchestrator deletes them after its caller commits.
        self.pending_point_deletes: list[PointDelete] = []

    def _search_reasons(self) -> dict[str, int]:
        """The per-row reasons the candidate search recorded (#3019)."""
        return {
            "no_similar_neighbour": self._rows_without_neighbour,
            "search_failed": self._rows_search_failed,
            "neighbour_not_eligible": self._hits_not_eligible,
        }

    def _backfill_cluster_scores(
        self,
        cluster_memories: list[Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
        backfilled_keys: set[tuple[UUID, UUID]],
    ) -> None:
        """Complete the in-cluster pairwise scores BEFORE judging (#1231).

        The per-memory top-10 neighbor search saturates in dense duplicate
        clusters, so genuinely >=threshold pairs can be missing from
        ``pair_scores`` — the LLM judge would be shown 0.0 (biasing keep_both)
        and the rule-based path could never nominate them. Clusters are capped
        at ``MAX_CLUSTER_SIZE``, so this is at most C(5,2)=10 local cosines over
        vectors already embedded during discovery.

        Settled pairs (#1208/#1232) stay unscored on purpose: their succession
        is already recorded.

        Mutates ``pair_scores`` and ``backfilled_keys`` in place.
        """
        for i, mem_x in enumerate(cluster_memories):
            for mem_y in cluster_memories[i + 1 :]:
                key = _pair_key(mem_x.id, mem_y.id)
                if key in pair_scores or key in self._settled_pair_keys:
                    continue
                direct = self._direct_pair_similarity(mem_x.id, mem_y.id)
                if direct is not None:
                    pair_scores[key] = direct
                    backfilled_keys.add(key)

    def _plan_cluster_merges(
        self,
        merge_decisions: list[tuple[UUID, UUID]],
        memory_map: dict[UUID, Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
        backfilled_keys: set[tuple[UUID, UUID]],
        threshold: float,
        tally: _MergeTally,
        snapshot_audit: bool,
        *,
        exempt_tags: frozenset[str],
    ) -> list[tuple[UUID, UUID, Memory | None, Memory | None, dict[str, Any] | None]]:
        """Pass 1: filter the judge's picks and snapshot their inputs (#1229).

        Eligibility is deterministic and judged on the SCORE, not on cluster
        membership: union-find clusters chain transitively AND candidate
        generation can regress, so the judge can nominate pairs whose true
        pairwise cosine is far below the threshold — observed 0.43-0.66 under
        0.92, destroying distinct facts. A merge executes only when the pair's
        direct similarity meets the configured threshold.

        #2584: a loser carrying one of ``exempt_tags`` (exact match) is never
        retired, in either mode. The pair is skipped, not flipped: merge folds
        only tags into the winner, so making the exempt side win would discard
        the other side's (often newer) content — why #1519 kept pinned rows out
        of merges instead of making them absolute winners. An exempt winner
        still absorbs a non-exempt loser; two exempt sides never merge.

        Every snapshot MUST be taken BEFORE any merge in the cluster executes
        (#1229): decisions share members (one winner, several losers), and a
        merge UPDATE fires ``onupdate=func.now()``, expiring attributes on the
        shared in-session instances — a later read would attempt a synchronous
        refresh, raising ``MissingGreenlet`` under the async engine and killing
        the whole phase. Returning an ordered work list pairs each snapshot
        structurally with its merge, so no keyed lookup can fall out of sync.

        Returns:
            ``(winner_id, loser_id, winner, loser, audit)`` tuples to execute.
        """
        merge_jobs: list[
            tuple[UUID, UUID, Memory | None, Memory | None, dict[str, Any] | None]
        ] = []
        for winner_id, loser_id in merge_decisions:
            pair_key = _pair_key(winner_id, loser_id)
            if pair_key in self._settled_pair_keys:
                # #1232: a supersedes edge already settles this pair — the judge
                # re-nominated it because members co-cluster via third parties.
                # Skipping is correct, but it is neither a sub-threshold guard
                # nor unverifiable; executing would re-merge (re-writing audit
                # rows and breaking undo's prior_edge snapshot) on every run.
                tally.settled_skipped += 1
                logger.debug(
                    "dedup_merge_skipped_already_settled",
                    winner_id=str(winner_id),
                    loser_id=str(loser_id),
                )
                continue

            pair_similarity = pair_scores.get(pair_key)
            if pair_similarity is None:
                # #1231 defense-in-depth: the per-cluster pre-fill normally
                # leaves only vector-unavailable pairs unscored, but judge
                # nominations outside the cluster's member list land here too.
                pair_similarity = self._direct_pair_similarity(winner_id, loser_id)
                if pair_similarity is not None:
                    # Backfill so the audit record carries the true score
                    # instead of the 0.0 fallback.
                    pair_scores[pair_key] = pair_similarity
                    backfilled_keys.add(pair_key)
            if pair_similarity is None:
                # Fail-closed, but distinct from a genuine sub-threshold veto:
                # without vectors the score is unknowable this run, and folding
                # these into llm_merge_guarded would dilute the cross-merge
                # signal that counter exists to expose (#1231).
                tally.unverifiable += 1
                logger.warning(
                    "dedup_merge_vetoed_vector_unavailable",
                    winner_id=str(winner_id),
                    loser_id=str(loser_id),
                    threshold=threshold,
                )
                continue
            if pair_similarity < threshold:
                tally.guarded += 1
                logger.warning(
                    "dedup_merge_vetoed_below_threshold",
                    winner_id=str(winner_id),
                    loser_id=str(loser_id),
                    pair_similarity=pair_similarity,
                    threshold=threshold,
                )
                continue
            loser = memory_map.get(loser_id)
            if loser is not None and not exempt_tags.isdisjoint(loser.tags or ()):
                # #2584: checked after the vetoes above so their counters keep
                # meaning what they say; the tag is read from the candidate fetch.
                tally.skipped_exempt += 1
                logger.info(
                    "dedup_merge_skipped_exempt_loser",
                    winner_id=str(winner_id),
                    loser_id=str(loser_id),
                )
                continue
            if pair_key in backfilled_keys:
                tally.rescued += 1
                logger.info(
                    "dedup_merge_rescued_by_direct_check",
                    winner_id=str(winner_id),
                    loser_id=str(loser_id),
                    pair_similarity=pair_similarity,
                    threshold=threshold,
                )

            winner = memory_map.get(winner_id)
            audit: dict[str, Any] | None = None
            if snapshot_audit and winner and loser:
                audit = {
                    "winner_tags": list(winner.tags or []),
                    "loser_tags": list(loser.tags or []),
                    "loser_summary": (loser.summary or "")[:200],
                    "winner_source": getattr(winner, "source_type", None),
                    "loser_source": getattr(loser, "source_type", None),
                    "winner_recency": _fmt_minute(self._recency_key(winner)),
                    "loser_recency": _fmt_minute(self._recency_key(loser)),
                }
            merge_jobs.append((winner_id, loser_id, winner, loser, audit))
        return merge_jobs

    async def _execute_merge_jobs(
        self,
        merge_jobs: list[tuple[UUID, UUID, Memory | None, Memory | None, dict[str, Any] | None]],
        *,
        shadow_mode: bool,
        user_id: str,
        workspace_id: Any,
        context_id: Any,
        pair_scores: dict[tuple[UUID, UUID], float],
        result: PhaseResult,
        tally: _MergeTally,
        reporter: Any,
        report_id: Any,
    ) -> None:
        """Pass 2: run the planned merges and record their audit rows.

        #1208: shadow mode records a supersedes edge and leaves the loser
        alive-but-shadowed instead of soft-deleting it — the non-destructive
        counterpart of update-by-removal. Nothing on either row changes, so no
        reindex is needed; remove mode marks the winner changed.
        """
        for winner_id, loser_id, winner, loser, audit in merge_jobs:
            prior_edge: dict[str, Any] | None = None
            resource_owned_before = self._resource_owned_refusals
            if shadow_mode:
                executed, prior_edge = await self._execute_shadow_merge(
                    winner, loser, user_id, workspace_id, context_id
                )
            else:
                executed = await self._execute_merge(
                    winner, loser, user_id, workspace_id, context_id
                )
            # #1519: the executor re-checks pin state under a row lock and
            # returns False when it refused — no audit row, no changed id,
            # not counted as merged (rollback would otherwise "restore" a
            # merge that never happened).
            if not executed:
                if self._resource_owned_refusals > resource_owned_before:
                    tally.skipped_resource_owned += 1
                else:
                    tally.skipped_pinned += 1
                continue
            if not shadow_mode:
                # The winner's content is merged: both sets (#2015 D5).
                result.changed_memory_ids.add(winner_id)
                if result.content_changed_memory_ids is not None:
                    result.content_changed_memory_ids.add(winner_id)
            tally.merged += 1

            if audit is None:
                continue
            pair_key = _pair_key(winner_id, loser_id)
            meta = self._decision_meta.get((winner_id, loser_id), {})
            await reporter.add_action(
                report_id=report_id,
                phase="dedup_merge",
                action_type="merge",
                memory_id=winner_id,
                target_id=loser_id,
                details={
                    "similarity": pair_scores.get(pair_key, 0.0),
                    "winner_tags": audit["winner_tags"],
                    "loser_tags": audit["loser_tags"],
                    "loser_summary": audit["loser_summary"],
                    "merge_reason": meta.get("merge_reason", "unspecified"),
                    "judge_confidence": meta.get("judge_confidence"),
                    "winner_override": "override_reason" in meta,
                    "override_reason": meta.get("override_reason"),
                    "winner_source": audit["winner_source"],
                    "loser_source": audit["loser_source"],
                    "winner_recency": audit["winner_recency"],
                    "loser_recency": audit["loser_recency"],
                    # #1208: how this merge disposed of the loser, plus the
                    # pre-merge state of any edge the shadow upsert retyped —
                    # undo restores it from here.
                    "mode": "shadow" if shadow_mode else "remove",
                    **({"prior_edge": prior_edge} if shadow_mode else {}),
                },
            )

    async def execute(
        self,
        config: NeuralMemoryConfig,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
        *,
        reporter: SleepReporter | None = None,
        report_id: UUID | None = None,
    ) -> PhaseResult:
        """Run dedup/merge phase.

        Args:
            config: NeuralMemoryConfig with sleep params
            user_id: Target user
            workspace_id: Target workspace
            context_id: Target context
            budget: Shared budget tracker

        Returns:
            PhaseResult with merge statistics and changed memory IDs
        """
        # #2015 D5: classified — a merge winner is the only content change.
        result = PhaseResult(phase_name="dedup_merge", content_changed_memory_ids=set())
        llm_calls_before = budget.llm_calls_used
        self._tokens_used = 0
        # #1183: judge calls that raised (feeds run-status grading).
        self._llm_failures = 0
        # #1195/#1198: LLM winner choices flipped by the deterministic
        # newer-wins post-check (each one is a judge misjudgment caught).
        self._winner_overrides = 0
        # #1209: reset the per-reason breakdown + per-decision audit metadata.
        self._override_reasons = {}
        self._decision_meta = {}
        # #471: per-(provider, model) accumulator (lazy-init).
        self._llm_breakdown: LLMCallBreakdown | None = None
        # #475: reset embedding accumulators between sleep cycles.
        self._embedding_calls_used = 0
        self._embedding_tokens_used = 0
        # #1232: reset the settled-pair record between sleep cycles.
        self._settled_pair_keys = set()
        # #3019: reset the search's per-row counters (a stubbed search leaves them).
        self._rows_without_neighbour = 0
        self._rows_search_failed = 0
        self._hits_not_eligible = 0
        self._searched_ok = None
        self._search_failures = None
        self._provider_embeds = 0
        self.coverage_outcome = None
        calls_at_entry = max(0, budget.max_llm_calls - budget.llm_calls_used)

        if not config.sleep_dedup_enabled:
            result.skipped = True
            result.skip_reason = "dedup_disabled"
            return result

        llm_enabled = config.sleep_llm_provider != ""
        threshold = config.sleep_dedup_similarity_threshold
        # #2584: a loser carrying one of these tags is never retired.
        exempt_tags = frozenset(config.sleep_archive_exempt_tags)

        # Step 1: Fetch active memories
        memories = await self._fetch_active_memories(
            user_id, workspace_id, context_id, config.sleep_max_memories_per_run
        )
        # #3019: one window row is searched too — its neighbours can be
        # anywhere in the partition.
        if not memories:
            result.details = {
                "message": "not_enough_memories",
                "count": len(memories),
                **phase_outcome(len(memories)),
            }
            return result

        # Step 2: Find similar pairs via Qdrant. #1523: pinned rows are not
        # candidates but still answer vector search, so widen the neighbour cap
        # by their count or they can saturate an unpinned memory's slots.
        pinned_in_scope = await self._count_pinned(user_id, workspace_id, context_id)
        pairs = await self._find_similar_pairs(
            memories,
            user_id,
            workspace_id,
            context_id,
            threshold,
            extra_neighbors=pinned_in_scope,
        )
        if not pairs:
            self._settle_outcomes(memories)
            result.details = {
                "message": "no_duplicate_candidates",
                **phase_outcome(len(memories), self._search_reasons()),
            }
            # #2060: the candidate search above already billed its embeddings.
            self.record_spend(result)
            return result

        # #1208: in shadow mode losers stay active, so already-settled pairs
        # (a supersedes edge exists between them) would be re-judged every
        # run — filter them out before clustering. Remove mode is untouched
        # (soft-deleted losers never reach _fetch_active_memories).
        settled_pairs_skipped = 0
        if getattr(config, "sleep_dedup_supersede_enabled", False):
            pairs, settled_pairs_skipped = await self._filter_already_superseded_pairs(
                pairs, user_id
            )
            if not pairs:
                self._settle_outcomes(memories)
                result.details = {
                    "message": "all_candidates_already_superseded",
                    "settled_pairs_skipped": settled_pairs_skipped,
                    **phase_outcome(
                        len(memories),
                        {**self._search_reasons(), "already_superseded": settled_pairs_skipped},
                    ),
                }
                self.record_spend(result)  # #2060: embeddings already billed
                return result

        # Step 3: Cluster with Union-Find
        uf = UnionFind()
        for id_a, id_b, _score in pairs:
            uf.union(id_a, id_b)

        clusters = uf.clusters()
        # Filter to clusters with 2+ members, cap at MAX_CLUSTER_SIZE
        processable = [c for c in clusters if 2 <= len(c) <= MAX_CLUSTER_SIZE]
        oversize = [c for c in clusters if len(c) > MAX_CLUSTER_SIZE]

        # #1184: oversize clusters used to be deferred WHOLESALE — on a
        # templated corpus everything union-finds into one mega-cluster and
        # dedup goes structurally inert ("0 merges" indistinguishable from
        # "nothing to merge"). Split them into judgeable subclusters instead;
        # cross-subcluster pairs are counted as deferred, not silently lost.
        split_subclusters = 0
        deferred_pairs = 0
        # #3019: the original oversize cluster each subcluster came from.
        origin_of: dict[int, int] = {}
        for big_index, big in enumerate(oversize):
            subclusters, skipped = _split_oversize_cluster(big, pairs)
            split_subclusters += len(subclusters)
            deferred_pairs += skipped
            for subcluster in subclusters:
                origin_of[len(processable)] = big_index
                processable.append(subcluster)

        if oversize:
            logger.info(
                "dedup_oversize_clusters_split",
                count=len(oversize),
                sizes=[len(c) for c in oversize],
                split_subclusters=split_subclusters,
                deferred_pairs=deferred_pairs,
            )

        # Step 4: Process each cluster
        memory_map = {m.id: m for m in memories}
        # #3019: a cluster's members that are not window rows are loaded (full
        # row and stored vector) right before it is judged; on the rule path,
        # which judges every cluster, all at once.
        if not llm_enabled:
            await self._load_outside_members(
                [mid for cluster in processable for mid in cluster],
                memory_map,
                user_id,
                workspace_id,
                context_id,
            )
        pair_scores = {_pair_key(a, b): s for a, b, s in pairs}
        tally = _MergeTally()
        # #1231: pair keys whose score came from the on-demand direct check
        # rather than candidate discovery — merges on these are "rescues"
        # the top-10 candidate cap would previously have vetoed.
        backfilled_keys: set[tuple[UUID, UUID]] = set()
        # #3019: clusters judged with no merge, and whose judge call failed;
        # what happened to each cluster (for its window rows' outcomes).
        kept_both = 0
        failed_clusters = 0
        judge_answered = False
        cluster_state: dict[int, str] = {}
        merged_oversize: set[int] = set()
        shadow_mode = bool(getattr(config, "sleep_dedup_supersede_enabled", False))

        for order, cap in self._judge_stages(
            processable, memories, llm_enabled=llm_enabled, calls=calls_at_entry
        ):
            for index in order:
                if index in cluster_state:
                    continue
                if cap is not None and budget.llm_calls_used - llm_calls_before >= cap:
                    break
                if not budget.can_afford(llm_calls=1 if llm_enabled else 0):
                    break
                cluster = processable[index]
                if llm_enabled:
                    await self._load_outside_members(
                        cluster, memory_map, user_id, workspace_id, context_id
                    )
                # A member that no longer loads (forgotten, pinned, moved) is
                # left out of the cluster.
                cluster_memories = [memory_map[mid] for mid in cluster if mid in memory_map]
                if len(cluster_memories) < 2:
                    cluster_state[index] = "dropped"
                    continue

                self._backfill_cluster_scores(cluster_memories, pair_scores, backfilled_keys)

                failures_before = self._llm_failures
                merge_decisions = await self._judge_cluster(
                    cluster_memories,
                    pair_scores,
                    llm_enabled,
                    user_id,
                    context_id,
                    workspace_id,
                    budget,
                    config,
                )
                if self._llm_failures > failures_before:
                    failed_clusters += 1
                    cluster_state[index] = "failed"
                else:
                    judge_answered = True
                    cluster_state[index] = "judged"
                    if not merge_decisions:
                        kept_both += 1

                merge_jobs = self._plan_cluster_merges(
                    merge_decisions,
                    memory_map,
                    pair_scores,
                    backfilled_keys,
                    threshold,
                    tally,
                    snapshot_audit=bool(reporter and report_id),
                    exempt_tags=exempt_tags,
                )
                merged_before = tally.merged
                await self._execute_merge_jobs(
                    merge_jobs,
                    shadow_mode=shadow_mode,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    context_id=context_id,
                    pair_scores=pair_scores,
                    result=result,
                    tally=tally,
                    reporter=reporter,
                    report_id=report_id,
                )
                if tally.merged > merged_before and index in origin_of:
                    merged_oversize.add(origin_of[index])

                result.memories_processed += len(cluster_memories)

        unjudged_clusters = len(processable) - len(cluster_state)
        self._settle_outcomes(
            memories,
            processable=processable,
            cluster_state=cluster_state,
            oversize=oversize,
            merged_oversize=merged_oversize,
            judge_answered=judge_answered,
        )

        result.details = {
            "candidates": len(pairs),
            "clusters": len(processable),
            # #1184: 'deferred_clusters' now counts ORIGINAL oversize clusters
            # (kept under its pre-split key for narrative/reader continuity);
            # their members are re-clustered into judgeable subclusters below
            # rather than skipped wholesale.
            "deferred_clusters": len(oversize),
            "oversize_clusters": len(oversize),
            "oversize_max_size": max((len(c) for c in oversize), default=0),
            "split_subclusters": split_subclusters,
            # Cap-blocked union attempts — a PROXY for candidate volume left
            # unjudged this run (see _split_oversize_cluster docstring).
            # Distinguishes "0 merges: nothing matched" from "0 merges:
            # work was deferred".
            "deferred_pairs": deferred_pairs,
            "merged": tally.merged,
            # #1208: pairs skipped because a supersedes edge already settled
            # them (shadow mode only; always 0 in remove mode).
            "settled_pairs_skipped": settled_pairs_skipped,
            "llm_call_failures": self._llm_failures,  # #1183
            # #1229: judge merge picks vetoed because the pair's direct
            # similarity is below threshold (transitive-chain cross-merges).
            # Since #1231 this is a pure sub-threshold signal: pairs missing
            # from the candidate list get an on-demand direct check first.
            "llm_merge_guarded": tally.guarded,
            # #1231: judge picks missing from the candidate list whose
            # on-demand direct cosine met the threshold — merges the top-10
            # candidate cap used to false-positive veto.
            "llm_merge_rescued": tally.rescued,
            # #1231: vetoes where no direct score could be computed (vector
            # unavailable — embed failed). Fail-closed, kept out of
            # llm_merge_guarded so that counter stays sub-threshold-only.
            "llm_merge_unverifiable": tally.unverifiable,
            # #1232: judge re-nominations of pairs a supersedes edge already
            # settled (shadow mode) — skipped, never guarded/rescued.
            "settled_decisions_skipped": tally.settled_skipped,
            # #1519: planned merges refused at execution because a row was
            # pinned/deleted after the candidate fetch (both modes).
            "skipped_pinned": tally.skipped_pinned,
            # #2996: the same refusal for a row the resource indexer owns
            # (it took a ``remember(external_id)`` row over since the fetch).
            "skipped_resource_owned": tally.skipped_resource_owned,
            # #2584: merges not planned because the loser carries an exempt
            # tag (sleep_archive_exempt_tags, default status:open).
            "skipped_exempt": tally.skipped_exempt,
            # #1195/#1198: LLM winner picks flipped by the deterministic
            # winner rules — a direct measure of residual judge misdirection
            # the prompt alone would have let through.
            "winner_overrides": self._winner_overrides,
            # #1209: per-rule breakdown ("recency" | "source") of the above.
            "winner_override_reasons": dict(self._override_reasons),
            **phase_outcome(
                len(memories),
                {
                    **self._search_reasons(),
                    "already_superseded": settled_pairs_skipped,
                    "deferred_oversize": deferred_pairs,
                    "judge_kept_both": kept_both,
                    "judge_failed": failed_clusters,
                    "not_judged_budget": unjudged_clusters,
                    "below_threshold": tally.guarded,
                    "unverifiable": tally.unverifiable,
                    "already_settled": tally.settled_skipped,
                    "exempt_tag": tally.skipped_exempt,
                    "changed_since_fetch": tally.skipped_pinned,
                    "resource_owned": tally.skipped_resource_owned,
                },
                budget_cut=unjudged_clusters > 0,
            ),
        }

        result.llm_calls_used = budget.llm_calls_used - llm_calls_before
        self.record_spend(result)

        logger.info(
            "dedup_merge_phase_completed",
            candidates=len(pairs),
            merged=tally.merged,
            llm_calls=result.llm_calls_used,
        )

        return result

    def _judge_stages(
        self,
        processable: list[set[UUID]],
        memories: list[Memory],
        *,
        llm_enabled: bool,
        calls: int,
    ) -> list[tuple[list[int], int | None]]:
        """The order the clusters are judged in, as stages (#3019).

        Each stage is (cluster indexes, a cap on the calls the phase may have
        used when the stage judges its next cluster, or None). ``C`` holds the
        clusters with a changed-lane row, ordered by their earliest such row;
        ``S`` those with a sweep-lane row, by their earliest sweep row (a
        cluster of both lanes is in both and judged once). With a judge, ``C``
        first until the sweep's share of the calls is all that is left, then
        ``S`` with every call left, then the rest of ``C``: neither lane can use
        the other's share. On the rule path every cluster is judged, ``C`` then
        ``S``. Without a window (a stubbed fetch) the clusters are judged in the
        order of their earliest row in the fetched list.
        """
        window = self.coverage_outcome

        def ordered(position: dict[UUID, int]) -> list[int]:
            first = {
                index: min(position[mid] for mid in cluster if mid in position)
                for index, cluster in enumerate(processable)
                if any(mid in position for mid in cluster)
            }
            return sorted(first, key=lambda index: first[index])

        if window is None:
            listed = {m.id: i for i, m in enumerate(memories)}
            last = len(memories)
            return [
                (
                    sorted(
                        range(len(processable)),
                        key=lambda index: min(listed.get(mid, last) for mid in processable[index]),
                    ),
                    None,
                )
            ]
        changed = ordered({key[1]: i for i, key in enumerate(window.changed)})
        sweep = ordered({key[1]: i for i, key in enumerate(window.sweep)})
        if not llm_enabled:
            return [(changed, None), (sweep, None)]
        sweep_calls = sweep_call_share(calls, self._lanes.turn)
        return [(changed, calls - sweep_calls), (sweep, None), (changed, None)]

    def _settle_outcomes(
        self,
        memories: list[Memory],
        *,
        processable: list[set[UUID]] | None = None,
        cluster_state: dict[int, str] | None = None,
        oversize: list[set[UUID]] | None = None,
        merged_oversize: set[int] | None = None,
        judge_answered: bool = False,
    ) -> None:
        """Each window row's outcome in this run (#3019), on ``coverage_outcome``.

        ``settled``: its search succeeded and it has no eligible pair, or its
        cluster was judged (the judge answered, or the rule path), or could not
        be judged because fewer than two members loaded. ``budget``: its cluster
        was not reached for lack of calls, or a merge executed in the original
        oversize cluster it belongs to (the rest is judged next run). A failure
        of the row's own — its search, or its cluster's judge call — is
        ``passed`` when the same kind of work succeeded for another row in this
        run, else ``failure`` (an outage holds the lane). For a failed search
        the kind is the step that raised: an embed needs another embed the
        provider answered (a vector from the cache proves nothing about the
        provider), a vector search another search that answered. A refusal that
        is never one row's own (the spend cap, a missing key or plan) is always
        a ``failure``.
        """
        window = self.coverage_outcome
        if window is None:
            return
        searched = self._searched_ok
        failed_at = self._search_failures or {}
        evidence = {
            "embed": self._provider_embeds > 0,
            "search": bool(searched),
        }
        cluster_of = {mid: index for index, c in enumerate(processable or []) for mid in c}
        oversize_of = {mid: index for index, c in enumerate(oversize or []) for mid in c}
        states = cluster_state or {}
        merged = merged_oversize or set()
        for memory in memories:
            mid = memory.id
            if searched is not None and mid not in searched:
                outcome = PASSED if evidence.get(failed_at.get(mid, ""), False) else FAILURE
            elif oversize_of.get(mid) in merged:
                outcome = BUDGET
            elif mid in cluster_of:
                state = states.get(cluster_of[mid])
                if state in ("judged", "dropped"):
                    outcome = SETTLED
                elif state == "failed":
                    outcome = PASSED if judge_answered else FAILURE
                else:
                    outcome = BUDGET
            else:
                outcome = SETTLED
            window.outcomes[mid] = outcome

    async def _fetch_active_memories(
        self,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        limit: int = 500,
    ) -> list[Memory]:
        """Fetch the window: up to ``limit`` eligible memories (``dedup_eligible``).

        #3019: the rows come from two lanes (``services.sleep.coverage``): the
        rows changed since the changed lane's position (newest first when more
        changed than fit), then the sweep over older rows in stable
        ``(created_at, id)`` order, so every row of the partition is read in a
        bounded number of runs. The window and each row's outcome are kept on
        ``coverage_outcome`` for the orchestrator.

        #1519: ``delivery_mode='always'`` rows are the deterministic
        ``load_pinned()`` lane (#886) and never enter dedup candidacy. A pinned
        loser would silently drop out of that lane, and a pinned *winner* is no
        better — merge folds only tags, so it would discard the newer unpinned
        fact instead of absorbing it. The same rule (``dedup_eligible``) decides
        which search hits outside the window are compared (#3019), so a pinned
        neighbour cannot re-enter that way either.

        #1524: ``type='time'`` rows are the other deterministic lane
        (``recall_upcoming``, keyed on ``details.trigger``). Two occurrences of a
        recurring window share a summary and embed identically, and merge folds
        only tags — the loser's trigger window would vanish from that lane.

        Tool guardrails (``details.tool_trigger``) are the third deterministic
        lane (``load_guardrails``): merge folds only tags, so a guardrail loser
        would silently stop firing in every client hook.

        #2996: rows the resource indexer owns (``resource_owned_predicate``)
        belong to their source, which alone removes or replaces them. They are
        also no longer embedded here, one embedding call each per run.
        """
        memories, window = await fetch_window(
            self.db, dedup_eligible(user_id, workspace_id, context_id), self._lanes, limit
        )
        self.coverage_outcome = window
        return memories

    async def _eligible_outside_ids(
        self,
        ids: list[UUID],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> set[UUID]:
        """The search hits outside the window that dedup may compare (#3019).

        An id-only check under ``dedup_eligible``, plus a successful
        embedding: a neighbour's score was computed against its stored vector,
        which matches its text only when its last embedding succeeded. A hit
        that does not come back (pinned, a guardrail, a time memory,
        resource-owned, forgotten, another user's or context's, a point id that
        is no memory, not embedded) is not compared.
        """
        eligible: set[UUID] = set()
        for start in range(0, len(ids), ELIGIBILITY_CHECK_CHUNK):
            chunk = ids[start : start + ELIGIBILITY_CHECK_CHUNK]
            rows = await self.db.execute(
                select(Memory.id).where(
                    Memory.id.in_(chunk),
                    dedup_eligible(user_id, workspace_id, context_id),
                    Memory.embedding_status == "success",
                )
            )
            eligible.update(rows.scalars().all())
        return eligible

    async def _load_cluster_members(
        self,
        ids: list[UUID],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> list[Memory]:
        """Load outside-window cluster members and their stored vectors (#3019).

        The rows are read under ``dedup_eligible`` and a successful
        embedding; a member that no longer qualifies does not load. Each
        loaded row's stored dense vector (its point: ``summary_embedding_id``,
        else its id — the embedding of its summary, the text the window rows
        are embedded from) goes into ``_summary_vectors``, so the direct-score
        veto scores its pairs like a window row's. A vector read that fails
        leaves the rows without vectors: their pairs are unverifiable.
        """
        loaded: list[Memory] = []
        collection = self.collection_name or "kagura_memories"
        for start in range(0, len(ids), MEMBER_LOAD_CHUNK):
            chunk = ids[start : start + MEMBER_LOAD_CHUNK]
            rows = list(
                (
                    await self.db.execute(
                        select(Memory).where(
                            Memory.id.in_(chunk),
                            dedup_eligible(user_id, workspace_id, context_id),
                            Memory.embedding_status == "success",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                continue
            loaded.extend(rows)
            points = {row.id: str(row.summary_embedding_id or row.id) for row in rows}
            try:
                vectors = await retrieve_dense_vectors(collection, list(points.values()))
            except Exception as e:
                logger.warning("dedup_member_vectors_unavailable", count=len(rows), error=str(e))
                continue
            for memory_id, point_id in points.items():
                vector = vectors.get(point_id)
                if vector is not None:
                    self._summary_vectors[memory_id] = vector
        return loaded

    async def _load_outside_members(
        self,
        member_ids: Iterable[UUID],
        memory_map: dict[UUID, Memory],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> None:
        """Add the members of ``member_ids`` not yet in ``memory_map`` (#3019)."""
        missing = sorted({mid for mid in member_ids if mid not in memory_map}, key=str)
        if not missing:
            return
        for row in await self._load_cluster_members(missing, user_id, workspace_id, context_id):
            memory_map[row.id] = row

    async def _count_pinned(
        self,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> int:
        """Live pinned or tool-triggered rows in the run's scope (#1523).

        Pinned rows are out of the candidate set but still in Qdrant, so they
        compete for the per-memory neighbour slots in ``_find_similar_pairs``.
        The count widens that search by exactly the rows that can crowd it.
        Tool guardrails leave the candidate set the same way (the fetch above),
        so they are counted here too, and so are rows the resource indexer
        owns (#2996: out of the candidate set, but their points answer the
        search); the method keeps its name because the rows it counts are "the
        exempt rows that still answer Qdrant".
        """
        stmt = select(func.count()).where(
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
            or_(pinned_predicate(), tool_triggered_predicate(), resource_owned_predicate()),
        )
        if workspace_id:
            stmt = stmt.where(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            stmt = stmt.where(Memory.context_id == UUID(context_id))
        return int((await self.db.execute(stmt)).scalar_one() or 0)

    async def _find_similar_pairs(
        self,
        memories: list[Memory],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        threshold: float,
        *,
        extra_neighbors: int = 0,
    ) -> list[tuple[UUID, UUID, float]]:
        """Find pairs of memories with cosine similarity >= threshold.

        For each window memory, embed its summary and search Qdrant for
        high-similarity neighbors. Deduplicates pairs by sorted ID tuple.

        #3019: a hit outside the window is kept when ``_eligible_outside_ids``
        returns it (the same eligibility rule as the window, plus a successful
        embedding), checked once after the search loop and before clustering,
        so an excluded row can neither be merged nor chain two clusters. Pairs
        come in window order; every pair has a window row.

        ``extra_neighbors`` (#1523) widens the per-memory neighbour cap by the
        number of pinned rows in scope (capped at ``MAX_EXTRA_NEIGHBORS``):
        pinned near-duplicates are filtered out below but still occupy Qdrant
        hits, and without the widening they could take every slot and hide an
        unpinned memory's true duplicate (the #1231 saturation shape).
        """
        neighbor_limit = NEIGHBOR_LIMIT + max(0, min(extra_neighbors, MAX_EXTRA_NEIGHBORS))
        memory_ids = {m.id for m in memories}
        hits_by_row: list[tuple[UUID, list[tuple[UUID, float]]]] = []
        # #1231: cache every successfully embedded vector for the veto's
        # on-demand direct check. Reset here (not in execute()) so the
        # cache's lifecycle matches the candidate search that fills it.
        self._summary_vectors = {}
        # #3019: per-row reasons for ``details.no_action_reasons``.
        self._rows_without_neighbour = 0
        self._rows_search_failed = 0
        self._hits_not_eligible = 0
        self._searched_ok = set()
        self._search_failures = {}
        self._provider_embeds = 0

        for memory in memories:
            row_hits: list[tuple[UUID, float]] = []
            # Where the vector came from, as the service reports it (#1989).
            embedded: dict[str, Any] = {}
            step = "embed"
            try:
                vector, tokens = await self.embedding_service.embed_with_usage(
                    memory.summary,
                    user_id=user_id,
                    context_id=context_id,
                    workspace_id=workspace_id,
                    outcome=embedded,
                )
                if embedded.get("cache_hit") is False:
                    self._provider_embeds += 1
                self._embedding_calls_used += 1
                self._embedding_tokens_used += tokens
                self._summary_vectors[memory.id] = vector

                step = "search"
                results = await search_memories_qdrant(
                    user_id=user_id,
                    query_vector=vector,
                    workspace_id=workspace_id or "",
                    context_id=context_id or "",
                    limit=neighbor_limit,
                    filters={"score_threshold": threshold},
                    collection_name=self.collection_name or "kagura_memories",
                )
                for hit in results:
                    hit_id = UUID(str(hit["id"]))
                    if hit_id == memory.id:
                        continue
                    row_hits.append((hit_id, hit["score"]))
            except Exception as e:
                self._rows_search_failed += 1
                self._search_failures[memory.id] = (
                    "refused" if isinstance(e, _REFUSALS_NOT_ROW_SPECIFIC) else step
                )
                logger.warning(
                    "dedup_search_failed",
                    memory_id=str(memory.id),
                    error=str(e),
                )
                continue
            self._searched_ok.add(memory.id)
            hits_by_row.append((memory.id, row_hits))
            if not row_hits:
                self._rows_without_neighbour += 1

        outside = sorted(
            {hit_id for _, hits in hits_by_row for hit_id, _ in hits if hit_id not in memory_ids},
            key=str,
        )
        eligible = (
            await self._eligible_outside_ids(outside, user_id, workspace_id, context_id)
            if outside
            else set()
        )
        pairs: list[tuple[UUID, UUID, float]] = []
        seen: set[tuple[UUID, UUID]] = set()
        for memory_id, hits in hits_by_row:
            for hit_id, score in hits:
                if hit_id not in memory_ids and hit_id not in eligible:
                    self._hits_not_eligible += 1
                    continue
                pair_key = _pair_key(memory_id, hit_id)
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                pairs.append((memory_id, hit_id, score))
        return pairs

    def _direct_pair_similarity(self, id_a: UUID, id_b: UUID) -> float | None:
        """Cosine similarity from this run's cached summary vectors (#1231).

        Used when a judge-nominated pair is missing from ``pair_scores``
        (the top-10 neighbor cap saturates in dense duplicate clusters).
        The vectors come from the same embeddings ``_find_similar_pairs``
        already computed, so the score definition matches Qdrant's cosine
        exactly and no extra embedding or search calls are made.

        Returns None when either vector is unavailable (embed failed) or
        degenerate (zero norm, dimension mismatch) — the caller stays
        fail-closed and vetoes.
        """
        vec_a = self._summary_vectors.get(id_a)
        vec_b = self._summary_vectors.get(id_b)
        if vec_a is None or vec_b is None or len(vec_a) != len(vec_b):
            return None
        dot = 0.0
        norm_a_sq = 0.0
        norm_b_sq = 0.0
        for x, y in zip(vec_a, vec_b, strict=True):
            dot += x * y
            norm_a_sq += x * x
            norm_b_sq += y * y
        if norm_a_sq == 0.0 or norm_b_sq == 0.0:
            return None
        similarity = dot / math.sqrt(norm_a_sq * norm_b_sq)
        # A NaN/inf component (degraded embedding provider) propagates to a
        # NaN similarity, and `NaN < threshold` is False — the veto would
        # fail OPEN. Treat non-finite as unverifiable instead.
        if not math.isfinite(similarity):
            return None
        return similarity

    async def _judge_cluster(
        self,
        cluster_memories: list[Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
        llm_enabled: bool,
        user_id: str,
        context_id: str | None,
        workspace_id: str | None,
        budget: SleepBudget,
        config: NeuralMemoryConfig,
    ) -> list[tuple[UUID, UUID]]:
        """Judge a cluster and return merge decisions as (winner_id, loser_id) pairs."""
        decisions: list[tuple[UUID, UUID]] = []

        if llm_enabled and budget.can_afford(llm_calls=1):
            decisions = await self._llm_judge(
                cluster_memories,
                pair_scores,
                user_id,
                context_id,
                workspace_id,
                budget,
                config,
            )
            # #1195/#1198/#1209: the LLM's winner is prompt-guided only —
            # enforce newer-wins + the equal-recency source tie-break
            # deterministically (rule-based path is correct by construction
            # and needs no post-check).
            decisions = self._enforce_winner_rules(decisions, cluster_memories)
        else:
            # Rule-based fallback
            decisions = self._rule_based_judge(cluster_memories, pair_scores)

        return decisions

    async def _llm_judge(
        self,
        cluster_memories: list[Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
        user_id: str,
        context_id: str | None,
        workspace_id: str | None,
        budget: SleepBudget,
        config: NeuralMemoryConfig,
    ) -> list[tuple[UUID, UUID]]:
        """Use LLM to judge duplicates in a cluster."""
        # Build label map (A, B, C...) — mitigates ID hallucination
        labels = list(string.ascii_uppercase[: len(cluster_memories)])
        label_to_id = {label: mem.id for label, mem in zip(labels, cluster_memories, strict=True)}
        id_to_label = {mem.id: label for label, mem in zip(labels, cluster_memories, strict=True)}

        # Shuffle to mitigate positional bias
        shuffled = list(zip(labels, cluster_memories, strict=True))
        random.shuffle(shuffled)
        labels_shuffled = [s[0] for s in shuffled]
        mems_shuffled = [s[1] for s in shuffled]

        # Format memories. The summary is untrusted (issue #919) — wrap it so an
        # embedded instruction cannot steer the merge judgment. last_updated= is
        # trusted DB metadata and stays OUTSIDE the wrapper: the judge needs it
        # to pick the newer version of an updated fact as winner (#1195).
        # Minute precision so same-day update pairs stay orderable; max of
        # created_at/updated_at so in-place edits count as recency (#1198).
        # source= is trusted #887 server-stamped provenance (#1209): the judge
        # sees which side is human-authored ('manual') vs ingested, and the
        # deterministic source tie-break is verifiable against it post-hoc.
        memory_lines = []
        for label, mem in zip(labels_shuffled, mems_shuffled, strict=True):
            recency = self._recency_key(mem)
            recency_s = _fmt_minute(recency) or "unknown"
            source_s = getattr(mem, "source_type", None) or "unknown"
            memory_lines.append(
                f"[{label}] type={mem.type}, importance={mem.importance:.2f}, "
                f"last_updated={recency_s}, source={source_s}\n"
                f"    summary:\n{wrap_untrusted_content(mem.summary)}"
            )

        # Format pairs
        pair_lines = []
        ids = [m.id for m in cluster_memories]
        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1 :]:
                sorted_ids = sorted([id_a, id_b], key=str)
                key = (sorted_ids[0], sorted_ids[1])
                score = pair_scores.get(key, 0.0)
                pair_lines.append(
                    f"  ({id_to_label[id_a]}, {id_to_label[id_b]}): similarity={score:.3f}"
                )

        prompt = DEDUP_JUDGE_USER.format(
            memories="\n".join(memory_lines),
            pairs="\n".join(pair_lines),
        )

        try:
            llm_resp = await self.llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=DEDUP_JUDGE_SYSTEM,
                context_id=context_id,
                workspace_id=workspace_id,
                model=config.sleep_llm_model,
                provider=config.sleep_llm_provider,
                platform_only=judge_platform_only(config),  # #1569
            )
        except Exception as e:
            self._llm_failures += 1  # #1183
            logger.warning("dedup_llm_judge_failed", error=str(e))
            if (usage := billed_usage(e)) is not None:
                # #2038: the failed call still spent these tokens — count them in the
                # run's totals and budget, but not as a successful call.
                budget.consume(llm_calls=1)
                self._tokens_used += usage.total_tokens
                self._llm_breakdown = accumulate_llm_response(
                    self._llm_breakdown, usage, count_call=False
                )
            return []

        # The call answered and was billed either way.
        budget.consume(llm_calls=1)
        self._tokens_used += llm_resp.total_tokens
        # #2060: parse OUTSIDE the LLM try, so each call is counted exactly
        # once. A parse exception used to count as a success AND a failure;
        # now an answer that cannot be parsed is a billed failure, like the
        # unparseable JSON LLMService raises on: tokens count, the call does
        # not, and it still feeds run grading (#1183).
        try:
            decisions = self._parse_dedup_response(llm_resp.parsed, label_to_id)
        except Exception as e:
            self._llm_failures += 1
            logger.warning("dedup_llm_response_unparseable", error=str(e))
            self._llm_breakdown = accumulate_llm_response(
                self._llm_breakdown, llm_resp, count_call=False
            )
            return []
        self._llm_breakdown = accumulate_llm_response(self._llm_breakdown, llm_resp)
        return decisions

    def _parse_dedup_response(
        self,
        response: dict,
        label_to_id: dict[str, UUID],
    ) -> list[tuple[UUID, UUID]]:
        """Parse LLM dedup response, validating all labels.

        #1209: the judge's per-judgment ``reason`` (already part of the JSON
        schema, previously discarded) is captured into ``_decision_meta`` so
        every merge action is explainable in the audit log. Parsed leniently —
        a missing/odd reason never invalidates an otherwise valid judgment.
        """
        self._ensure_audit_state()
        decisions: list[tuple[UUID, UUID]] = []
        judgments = response.get("judgments", [])
        # #1209: dedupe judgments by canonical pair — a malformed response
        # listing the same pair twice (same or reversed order) would
        # double-merge AND cross-contaminate the audit metadata when the
        # winner rules re-key a flipped decision.
        seen_pairs: set[tuple[UUID, UUID]] = set()

        for j in judgments:
            if j.get("verdict") != "merge":
                continue

            winner_label = j.get("winner")
            pair = j.get("pair", [])

            if len(pair) != 2:
                continue

            # Validate labels exist
            if pair[0] not in label_to_id or pair[1] not in label_to_id:
                logger.warning(
                    "dedup_invalid_label",
                    pair=pair,
                    valid_labels=list(label_to_id.keys()),
                )
                continue

            if winner_label not in label_to_id:
                continue

            loser_label = pair[0] if pair[1] == winner_label else pair[1]
            decision = (label_to_id[winner_label], label_to_id[loser_label])
            canonical = tuple(sorted(decision, key=str))
            if canonical in seen_pairs:
                logger.warning(
                    "dedup_duplicate_judgment_skipped",
                    pair=[str(decision[0]), str(decision[1])],
                )
                continue
            seen_pairs.add(canonical)
            decisions.append(decision)

            reason = j.get("reason")
            confidence = j.get("confidence")
            self._decision_meta[decision] = {
                "merge_reason": (str(reason)[:300] if reason else "unspecified"),
                "judge_confidence": (
                    float(confidence) if isinstance(confidence, (int, float)) else None
                ),
            }

        return decisions

    @staticmethod
    def _recency_key(mem: Memory) -> datetime | None:
        """Last time this memory's content was current: max(created_at,
        updated_at) over whichever are real datetimes, else None.

        created_at alone is WRONG for recency (#1198 review): an in-place
        edit preserves created_at and bumps updated_at, so the row holding
        the current fact can carry the older created_at.
        """
        stamps = [
            v
            for v in (getattr(mem, "created_at", None), getattr(mem, "updated_at", None))
            if isinstance(v, datetime)
        ]
        return max(stamps) if stamps else None

    @classmethod
    def _is_newer(cls, candidate: Memory, other: Memory) -> bool:
        """True iff both carry a recency key and candidate's is strictly newer."""
        c = cls._recency_key(candidate)
        o = cls._recency_key(other)
        return c is not None and o is not None and c > o

    @staticmethod
    def _is_user_authored(mem: Memory) -> bool:
        """#1209: 'manual' provenance = human-authored (#887 source_type)."""
        return getattr(mem, "source_type", None) == "manual"

    def _ensure_audit_state(self) -> None:
        """Lazy-init the #1209 audit state.

        Several unit tests construct the phase via ``__new__`` (bypassing
        ``__init__``) to call individual judge methods — the audit state must
        not make those methods construction-order dependent.
        """
        if not hasattr(self, "_decision_meta"):
            self._decision_meta = {}
        if not hasattr(self, "_override_reasons"):
            self._override_reasons = {}
        if not hasattr(self, "_winner_overrides"):
            self._winner_overrides = 0

    def _record_override(
        self,
        original: tuple[UUID, UUID],
        flipped: tuple[UUID, UUID],
        reason: str,
    ) -> None:
        """Count an override and re-key the decision's audit metadata."""
        self._ensure_audit_state()
        self._winner_overrides += 1
        self._override_reasons[reason] = self._override_reasons.get(reason, 0) + 1
        meta = self._decision_meta.pop(original, {})
        meta["override_reason"] = reason
        self._decision_meta[flipped] = meta
        logger.warning(
            "dedup_winner_overridden",
            llm_winner_id=str(original[0]),
            enforced_winner_id=str(flipped[0]),
            override_reason=reason,
        )

    @classmethod
    def _recency_minute(cls, mem: Memory) -> datetime | None:
        """Recency key truncated to the minute — the precision the judge saw.

        The prompt renders ``last_updated=`` at minute precision, so the
        deterministic rules compare at the same precision: sub-minute deltas
        are write-ordering noise, not a recency signal, and must neither
        trigger the newer-wins flip nor defeat the source tie-break (#1209).
        """
        key = cls._recency_key(mem)
        return key.replace(second=0, microsecond=0) if key else None

    def _enforce_winner_rules(
        self,
        decisions: list[tuple[UUID, UUID]],
        cluster_memories: list[Memory],
    ) -> list[tuple[UUID, UUID]]:
        """Deterministic post-checks on LLM merge decisions (#1195/#1198/#1209).

        The prompt asks for these rules but cannot guarantee them — the judge
        picked the older memory in 10-18% of update pairs in the Day-5 eval.
        The duplicate verdict is trusted; the direction is enforced, in order,
        at MINUTE precision (matching the ``last_updated=`` signal the judge
        was shown):

        1. **Newer wins** (#1198): a winner strictly older (by minute) than
           its loser is flipped ("recency" override).
        2. **Human-authored wins at equal recency** (#1209): when the
           minute-precision recency keys are EQUAL (including both-unknown),
           a non-manual winner never deletes a manual (human-authored, #887
           provenance) loser — the "trusted over ingested" tie-break.
           Recency stays primary: this rule never fires when one side is
           strictly newer ("source" override).
        """
        id_to_mem = {m.id: m for m in cluster_memories}
        corrected: list[tuple[UUID, UUID]] = []
        for winner_id, loser_id in decisions:
            winner = id_to_mem.get(winner_id)
            loser = id_to_mem.get(loser_id)
            if winner is None or loser is None:
                corrected.append((winner_id, loser_id))
                continue
            winner_key = self._recency_minute(winner)
            loser_key = self._recency_minute(loser)
            if winner_key is not None and loser_key is not None and loser_key > winner_key:
                self._record_override((winner_id, loser_id), (loser_id, winner_id), "recency")
                corrected.append((loser_id, winner_id))
            elif (
                winner_key == loser_key
                and self._is_user_authored(loser)
                and not self._is_user_authored(winner)
            ):
                self._record_override((winner_id, loser_id), (loser_id, winner_id), "source")
                corrected.append((loser_id, winner_id))
            else:
                corrected.append((winner_id, loser_id))
        return corrected

    def _rule_based_judge(
        self,
        cluster_memories: list[Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
    ) -> list[tuple[UUID, UUID]]:
        """Rule-based dedup: auto-merge only at very high similarity."""
        self._ensure_audit_state()
        decisions: list[tuple[UUID, UUID]] = []
        ids = [m.id for m in cluster_memories]
        id_to_mem = {m.id: m for m in cluster_memories}

        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1 :]:
                sorted_ids = sorted([id_a, id_b], key=str)
                key = (sorted_ids[0], sorted_ids[1])
                score = pair_scores.get(key, 0.0)
                if score >= AUTO_MERGE_THRESHOLD:
                    # Keep the one with higher importance; at equal importance
                    # the NEWER memory wins (#1195 — was: arbitrary cluster
                    # order, which could delete the updated version of a fact).
                    # Residual: at equal importance AND identical recency keys
                    # (or missing timestamps) the tie still falls to cluster
                    # order — acceptable, both versions carry the same stamp.
                    mem_a = id_to_mem[id_a]
                    mem_b = id_to_mem[id_b]
                    if mem_a.importance > mem_b.importance:
                        decision = (id_a, id_b)
                    elif mem_b.importance > mem_a.importance:
                        decision = (id_b, id_a)
                    elif self._is_newer(mem_b, mem_a):
                        decision = (id_b, id_a)
                    else:
                        decision = (id_a, id_b)
                    decisions.append(decision)
                    # #1209: rule-path merges are explainable too.
                    self._decision_meta[decision] = {
                        "merge_reason": f"rule:cosine>={AUTO_MERGE_THRESHOLD}",
                        "judge_confidence": None,
                    }

        return decisions

    async def _execute_shadow_merge(
        self,
        winner: Memory | None,
        loser: Memory | None,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """#1208 shadow-mode merge: record succession, mutate nothing.

        Creates a ``supersedes`` edge (src=winner, dst=loser,
        origin='semantic' — machine-inferred by the dedup judge, not
        user-asserted). The loser row, vector, tags and edges are all left
        untouched: recall shadows it out by the edge alone, so undoing the
        merge restores full visibility. Upserts via create_or_update_edge:
        ``unique_edge`` is keyed on (user, src, dst) regardless of edge_type,
        so a pre-existing edge between the pair (e.g. a Hebbian association
        between near-duplicates — the common case) must be retyped to
        ``supersedes``, not silently left as-is while the action is audited
        as mode=shadow. A pre-existing origin='declared' row keeps its
        origin (sticky-origin CASE in the upsert). Idempotent —
        re-judging the same pair on a later run converges to the same row
        (and already-superseded pairs are filtered out before judging to
        avoid burning judge budget on settled pairs).

        Returns:
            ``(executed, prior_edge)``. ``executed`` is False when the #1519
            execution-time recheck refused the pair (a row was pinned or
            deleted after the candidate fetch) — nothing was written.
            ``prior_edge`` is a snapshot of the pre-merge edge state
            (type/origin/weight/confidence/metadata) when the upsert retyped
            an existing non-supersedes edge, else None. The caller stamps it
            into the merge action's details as ``prior_edge`` so undo/rollback
            can RESTORE the original association instead of deleting the row.
        """
        if not winner or not loser:
            return False, None
        if await self._pinned_or_gone_since_fetch(winner, loser):
            return False, None
        from models.memory import EDGE_ORIGIN_SEMANTIC, EDGE_TYPE_SUPERSEDES

        prior = await self.edge_repo.get_edge(
            user_id, winner.id, loser.id, workspace_id, context_id
        )
        prior_edge: dict[str, Any] | None = None
        if prior is not None and prior.edge_type != EDGE_TYPE_SUPERSEDES:
            prior_edge = {
                "edge_type": prior.edge_type,
                "origin": prior.origin,
                "weight": prior.weight,
                "confidence": prior.confidence,
                "edge_metadata": prior.edge_metadata,
            }

        await self.edge_repo.create_or_update_edge(
            user_id=user_id,
            src_id=winner.id,
            dst_id=loser.id,
            edge_type=EDGE_TYPE_SUPERSEDES,
            weight=1.0,
            confidence=1.0,
            workspace_id=workspace_id,
            context_id=context_id,
            origin=EDGE_ORIGIN_SEMANTIC,
            return_fresh_edge=False,
        )
        logger.info(
            "dedup_shadow_merge",
            winner_id=str(winner.id),
            loser_id=str(loser.id),
            retyped_prior_edge=prior_edge is not None,
        )
        return True, prior_edge

    async def _filter_already_superseded_pairs(
        self,
        pairs: list[tuple[UUID, UUID, float]],
        user_id: str,
    ) -> tuple[list[tuple[UUID, UUID, float]], int]:
        """#1208: drop candidate pairs already linked by a supersedes edge.

        In shadow mode the loser stays active, so the same near-duplicate
        pair would be re-detected and re-judged on EVERY sleep run — burning
        LLM budget on pairs whose succession is already settled. Filtering
        is direction-agnostic (either orientation settles the pair).

        Side effect (#1232): records the settled pair keys in
        ``self._settled_pair_keys`` — the merge loop's settled guard reads
        it to skip judge re-nominations. Tests that stub this method with a
        passthrough leave the record empty, silently disabling that guard;
        include settled pairs only via the real method (or set the
        attribute explicitly).

        Returns:
            (remaining pairs, count of pairs skipped as already settled).
        """
        if not pairs:
            return pairs, 0
        from models.memory import EDGE_TYPE_SUPERSEDES, NeuralMemoryEdge

        all_ids = {mid for a, b, _ in pairs for mid in (a, b)}
        rows = await self.db.execute(
            select(NeuralMemoryEdge.src_id, NeuralMemoryEdge.dst_id).where(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
                NeuralMemoryEdge.src_id.in_(all_ids),
                NeuralMemoryEdge.dst_id.in_(all_ids),
            )
        )
        settled = {_pair_key(src, dst) for src, dst in rows.all()}
        # #1232: remember WHICH pairs are settled, not just how many were
        # filtered — the judge can re-nominate a settled pair via
        # third-party co-clustering, and the merge loop must recognize it
        # (skip; do not count as guarded, do not rescue into a re-merge).
        self._settled_pair_keys = settled
        if not settled:
            return pairs, 0
        remaining = [(a, b, s) for a, b, s in pairs if _pair_key(a, b) not in settled]
        return remaining, len(pairs) - len(remaining)

    async def _execute_merge(
        self,
        winner: Memory | None,
        loser: Memory | None,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> bool:
        """Execute a merge: soft-delete loser, transfer edges and tags to winner.

        Returns:
            True when the merge ran; False when it was refused because a row
            was pinned/deleted after the candidate fetch (#1519) or a side is
            missing. Nothing is written on False.
        """
        if not winner or not loser:
            return False
        if await self._pinned_or_gone_since_fetch(winner, loser):
            return False

        winner_tags = set(winner.tags or [])
        loser_tags = set(loser.tags or [])
        merged_tags = list(winner_tags | loser_tags)

        await self.db.execute(
            update(Memory)
            .where(Memory.id == winner.id)
            .values(tags=merged_tags, updated_at=utcnow())
        )

        # Soft-delete loser in PostgreSQL
        await self.db.execute(
            update(Memory)
            .where(Memory.id == loser.id)
            .values(
                deleted_at=utcnow(),
                deleted_by=DELETED_BY_SLEEP_MERGE,
            )
        )

        # Delete loser from Qdrant to prevent orphan vectors (cf. BUG FIX #83-10).
        # #2065: by forget's rule — a resource point another live row still
        # names stays with that row (``unshared_point_id``).
        # #2565: queued beside the soft delete, deleted after the caller's
        # commit (``SleepOrchestrator.delete_committed_points``): a run that
        # rolls back leaves the loser live with its point.
        try:
            point_id = await unshared_point_id(self.db, loser.id, loser.point_id)
        except Exception as e:
            point_id = None
            logger.warning(
                "dedup_qdrant_delete_failed",
                loser_id=str(loser.id),
                error=str(e),
            )
        if point_id is not None:
            entry = (loser.id, point_id, self.collection_name or "kagura_memories")
            await queue_point_deletes(self.db, [entry])
            self.pending_point_deletes.append(entry)

        # Transfer edges from loser to winner
        await self.edge_repo.transfer_edges(
            from_node_id=loser.id,
            to_node_id=winner.id,
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
        )

        logger.info(
            "dedup_merge_executed",
            winner_id=str(winner.id),
            loser_id=str(loser.id),
            merged_tags=len(merged_tags),
        )
        return True

    async def _pinned_or_gone_since_fetch(self, winner: Memory, loser: Memory) -> bool:
        """#1519: re-check both rows under a row lock right before writing.

        ``_fetch_active_memories`` excludes pinned rows, but embedding and the
        optional LLM judge run between that fetch and execution — long enough
        for another session to pin (``delivery_mode='always'``) or forget one
        side. ``SELECT ... FOR UPDATE`` inside the phase transaction serializes
        against a concurrent ``update_memory``/``forget`` so the state read here
        is the state the merge would act on. Returns True (refuse) when either
        row is now pinned, soft-deleted, missing, (#1524) a time memory —
        ``update_memory`` can flip ``type`` in the same window, and a merge would
        drop the loser's trigger window from ``recall_upcoming`` — (#2996) a
        row the resource indexer owns (it re-points a ``remember(external_id)``
        row it matches by natural key), or (#3019) a row whose user or context
        is no longer the one it was fetched with (moved to another context).
        """
        rows = (
            await self.db.execute(
                select(
                    Memory.id,
                    pinned_predicate().label("pinned"),
                    tool_triggered_predicate().label("tool_triggered"),
                    resource_owned_predicate().label("resource_owned"),
                    Memory.deleted_at,
                    Memory.type,
                    # #3019: the partition the row was fetched in.
                    Memory.user_id,
                    Memory.context_id,
                )
                .where(Memory.id.in_([winner.id, loser.id]))
                .with_for_update()
            )
        ).all()
        state = {row.id: row for row in rows}
        reasons: list[str] = []
        for label, memory in (("winner", winner), ("loser", loser)):
            row = state.get(memory.id)
            if row is None:
                reasons.append(f"{label}_missing")
            elif row.deleted_at is not None:
                reasons.append(f"{label}_deleted")
            elif row.pinned:
                reasons.append(f"{label}_pinned")
            elif row.tool_triggered:
                reasons.append(f"{label}_tool_triggered")
            elif row.resource_owned:
                reasons.append(f"{label}_resource_owned")
            elif row.type == MEMORY_TYPE_TIME:
                reasons.append(f"{label}_time")
            elif row.user_id != memory.user_id or row.context_id != memory.context_id:
                # #3019: moved to another context (or user) since the fetch.
                reasons.append(f"{label}_moved")
        if not reasons:
            return False
        if any(reason.endswith("_resource_owned") for reason in reasons):
            self._resource_owned_refusals += 1
        logger.info(
            "dedup_merge_skipped_pinned",
            winner_id=str(winner.id),
            loser_id=str(loser.id),
            reasons=reasons,
        )
        return True
