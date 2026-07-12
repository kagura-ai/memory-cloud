"""Per-context memory-health report (#1211 Phase 1, #1225 Phase 2).

The eval program's most transferable lesson: memory failures manifest as
plausible successes (``sleep_summary.ok=true`` while every judge call failed,
#1177). The system already emits the needed signals — ``llm_call_failures``
(#1183), ``winner_overrides`` (#1198), merge counts and cluster stats, graph
stats, soft-delete rows — but they are fragmented across the Sleep report,
graph stats, memory stats and logs. This service assembles them into
thresholded documents so a silent-judge-death class of failure surfaces as
WARN/FAIL within one sleep cycle instead of via an external eval program.

Scope rules (gate1/COO, #1211; per-context leaf scoping added by #1225):

- **Label-free only**: rates that need gold labels (``stale_only``, P@k)
  belong to the eval harness / #1210 CI gates, not here.
- **No log-derived metrics**: signals that exist only in structlog output
  (e.g. ``reinforce_rerank_applied``) are excluded until they are persisted —
  not rendered as "pending".
- **Conservative thresholds**: FAIL only on deterministic facts (latest run
  failed, invariant violated); WARN on degradation signals. A false FAIL
  erodes dashboard trust (the eval-infra lesson).
- **Self-scoped**: reports cover the calling admin's own data partition,
  broken down per owned context (#1225). Signals recorded without a
  ``context_id`` (account-wide sleep runs, cross-context recalls, legacy
  rows) surface as one explicit *unattributed* entry rather than being
  silently dropped — dropping them would hide Phase-1 WARNs behind the
  new grouping (the same plausible-success shape this report exists to
  prevent).
- Workspace-level rollup across members is deliberately Phase 3
  (needs ``PermissionService.check_workspace_access()`` semantics).

Notes are structured ``{code, params}`` records (#1225); the frontend maps
codes to localized strings. Issue references stay in code comments and
``docs/ops/memory-health-report.md`` — never in the payload.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, UsageStats
from models.config import ContextSearchConfig
from models.memory import Memory, NeuralMemoryEdge
from models.sleep import SleepReport
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

_STATUS_RANK = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_FAIL: 2}

# Sleep reports considered "the recent window" for consolidation grading.
_REPORT_WINDOW = 20
# Soft-deleted merge losers older than this warn (retention likely wanted).
_BACKLOG_WARN_DAYS = 90
# A graph with this many active memories and zero edges is suspiciously cold.
_COLD_GRAPH_MIN_MEMORIES = 25
# Retrieval activity window.
_USAGE_WINDOW_DAYS = 7

_EDGE_WEIGHT_MIN = 0.0
_EDGE_WEIGHT_MAX = 3.0

# Scope key for signals recorded without a context_id. NeuralMemoryEdge
# rows always carry a context (NOT NULL CHECK), but SleepReport, Memory and
# UsageStats are nullable.
_UNATTRIBUTED: uuid.UUID | None = None

_EMPTY_BACKLOG: dict[str, Any] = {"count": 0, "oldest_days": None}
_EMPTY_POSTURE: dict[str, int] = {
    "contexts_with_config": 0,
    "reinforce_enabled": 0,
    "use_rerank": 0,
}


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _STATUS_RANK[s]) if statuses else STATUS_OK


def _note(code: str, **params: Any) -> dict[str, Any]:
    """One structured section note. Codes are a stable contract with the
    frontend message catalogs and docs/ops/memory-health-report.md."""
    return {"code": code, "params": params}


def _empty_graph_stats() -> dict[str, Any]:
    return {
        "edges_by_origin": {},
        "total_edges": 0,
        "weight_violations": 0,
        "active_memories": 0,
        "edges_per_memory": 0.0,
    }


class MemoryHealthService:
    """Builds per-context memory-health documents for one user partition."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------- builders

    async def build_breakdown(self, user_id: str) -> dict[str, Any]:
        """One graded entry per owned context, plus an unattributed entry
        when context-less signals exist.

        Returns ``{generated_at, overall_status, contexts: [{context_id,
        name, overall_status, sections: {name: status}}]}``. No metric is
        summed across contexts; the page-level overall is the worst entry.
        A user with zero contexts and no unattributed signals gets an ok
        overall with an empty list.
        """
        contexts = await self._fetch_owned_contexts(user_id)
        signals = await self._fetch_signals(user_id)

        scopes: list[tuple[uuid.UUID | None, str | None]] = list(contexts)
        if self._has_unattributed_signals(signals):
            scopes.append((_UNATTRIBUTED, None))

        entries: list[dict[str, Any]] = []
        for context_id, name in scopes:
            sections = self._grade_scope(signals, context_id)
            entries.append(
                {
                    "context_id": str(context_id) if context_id else None,
                    "name": name,
                    "overall_status": _worst([s["status"] for s in sections.values()]),
                    "sections": {k: v["status"] for k, v in sections.items()},
                }
            )

        return {
            "generated_at": to_utc_iso(utcnow()),
            "overall_status": _worst([e["overall_status"] for e in entries]),
            "contexts": entries,
        }

    async def build_context_report(
        self, user_id: str, context_scope: uuid.UUID | None
    ) -> dict[str, Any] | None:
        """The 3-section detailed document for a single scope.

        ``context_scope=None`` targets the unattributed bucket (signals
        recorded without a context). A UUID scope is validated against the
        caller's owned, non-deleted contexts; returns ``None`` when not
        owned (the route maps that to a uniform 404 — no existence
        disclosure).
        """
        context_name: str | None = None
        if context_scope is not None:
            owned = dict(await self._fetch_owned_contexts(user_id))
            if context_scope not in owned:
                return None
            context_name = owned[context_scope]

        signals = await self._fetch_signals(user_id)
        sections = self._grade_scope(signals, context_scope)
        return {
            "generated_at": to_utc_iso(utcnow()),
            "context_id": str(context_scope) if context_scope else None,
            "context_name": context_name,
            "overall_status": _worst([s["status"] for s in sections.values()]),
            "sections": sections,
        }

    # -------------------------------------------------------------- scoping

    async def _fetch_signals(self, user_id: str) -> dict[str, Any]:
        """All grouped signal maps, one query per signal (no per-context
        fan-out — the gate1 N+1 concern)."""
        return {
            "windows": await self._fetch_sleep_windows(user_id),
            "backlogs": await self._fetch_merge_backlogs(user_id),
            "graphs": await self._fetch_graph_stats(user_id),
            "usage": await self._fetch_usage_counts(user_id),
            "postures": await self._fetch_config_postures(user_id),
        }

    @staticmethod
    def _has_unattributed_signals(signals: dict[str, Any]) -> bool:
        graphs = signals["graphs"].get(_UNATTRIBUTED) or _empty_graph_stats()
        return bool(
            signals["windows"].get(_UNATTRIBUTED)
            or signals["backlogs"].get(_UNATTRIBUTED, _EMPTY_BACKLOG)["count"]
            or graphs["active_memories"]
            or signals["usage"].get(_UNATTRIBUTED)
        )

    def _grade_scope(self, signals: dict[str, Any], context_id: uuid.UUID | None) -> dict[str, Any]:
        """Grade the 3 sections from one scope's slice of the signal maps.

        The isolation contract (#1225): only rows recorded under this
        ``context_id`` contribute — a WARN-producing signal in context A
        cannot change context B's grade.
        """
        graph_stats = signals["graphs"].get(context_id) or _empty_graph_stats()
        # The unattributed bucket skips the heuristic checks that assume a
        # real context partition: edges always carry a context (NOT NULL),
        # so legacy context-less memories would always look "cold", and
        # cross-context recalls land here making write-only checks noise.
        # False WARNs erode dashboard trust more than a missing heuristic.
        heuristics = context_id is not None
        return {
            "consolidation": self._grade_consolidation(
                signals["windows"].get(context_id, []),
                signals["backlogs"].get(context_id, _EMPTY_BACKLOG),
            ),
            "graph": self._grade_graph(graph_stats, cold_check=heuristics),
            "retrieval": self._grade_retrieval(
                signals["usage"].get(context_id, {}),
                signals["postures"].get(context_id, _EMPTY_POSTURE),
                active_memories=graph_stats["active_memories"],
                write_only_check=heuristics,
            ),
        }

    # ------------------------------------------------------------- fetchers

    async def _fetch_owned_contexts(self, user_id: str) -> list[tuple[uuid.UUID, str]]:
        """Owned, non-deleted contexts — the Phase-1 self-scope predicate
        (same as the config-posture join); workspace semantics are Phase 3."""
        rows = await self.db.execute(
            select(Context.id, Context.name, Context.display_name)
            .where(Context.created_by == user_id, Context.deleted_at.is_(None))
            .order_by(Context.name)
        )
        return [(cid, display_name or name) for cid, name, display_name in rows.all()]

    async def _fetch_sleep_windows(
        self, user_id: str
    ) -> dict[uuid.UUID | None, list[dict[str, Any]]]:
        """Most recent sleep reports per context (newest first), flattened.

        One query: row_number() partitioned by context_id caps every scope
        at the same window the Phase-1 report used.
        """
        rn = (
            func.row_number()
            .over(
                partition_by=SleepReport.context_id,
                order_by=SleepReport.started_at.desc(),
            )
            .label("rn")
        )
        subq = (
            select(
                SleepReport.context_id,
                SleepReport.status,
                SleepReport.llm_call_failures,
                SleepReport.memories_merged,
                SleepReport.dedup_result,
                SleepReport.started_at,
                rn,
            )
            .where(SleepReport.user_id == user_id)
            .subquery()
        )
        result = await self.db.execute(
            select(subq)
            .where(subq.c.rn <= _REPORT_WINDOW)
            .order_by(subq.c.context_id, subq.c.started_at.desc())
        )
        windows: dict[uuid.UUID | None, list[dict[str, Any]]] = defaultdict(list)
        for row in result.all():
            dedup = row.dedup_result or {}
            details = dedup.get("details") or {}
            windows[row.context_id].append(
                {
                    "status": row.status,
                    "llm_call_failures": row.llm_call_failures or 0,
                    "memories_merged": row.memories_merged or 0,
                    "winner_overrides": details.get("winner_overrides", 0),
                    "deferred_pairs": details.get("deferred_pairs", 0),
                    "oversize_clusters": details.get("oversize_clusters", 0),
                    "started_at": to_utc_iso(row.started_at) if row.started_at else None,
                }
            )
        return dict(windows)

    async def _fetch_merge_backlogs(self, user_id: str) -> dict[uuid.UUID | None, dict[str, Any]]:
        """Soft-deleted merge losers per context: count + oldest age (days)."""
        rows = await self.db.execute(
            select(
                Memory.context_id,
                func.count(Memory.id),
                func.min(Memory.deleted_at),
            )
            .where(
                Memory.user_id == user_id,
                Memory.deleted_by == "sleep_maintenance",
                Memory.deleted_at.is_not(None),
            )
            .group_by(Memory.context_id)
        )
        now = utcnow()
        backlogs: dict[uuid.UUID | None, dict[str, Any]] = {}
        for context_id, count, oldest in rows.all():
            oldest_days = max(0, (now - oldest).days) if oldest is not None else None
            backlogs[context_id] = {"count": int(count or 0), "oldest_days": oldest_days}
        return backlogs

    async def _fetch_graph_stats(self, user_id: str) -> dict[uuid.UUID | None, dict[str, Any]]:
        """Edge composition, weight-invariant violations and density, per
        context. Edges always carry a context; active memories may not."""
        origin_rows = await self.db.execute(
            select(
                NeuralMemoryEdge.context_id,
                NeuralMemoryEdge.origin,
                func.count(NeuralMemoryEdge.id),
            )
            .where(NeuralMemoryEdge.user_id == user_id)
            .group_by(NeuralMemoryEdge.context_id, NeuralMemoryEdge.origin)
        )
        edges_by_scope: dict[uuid.UUID | None, dict[str, int]] = defaultdict(dict)
        for context_id, origin, count in origin_rows.all():
            edges_by_scope[context_id][origin] = int(count)

        violation_rows = await self.db.execute(
            select(NeuralMemoryEdge.context_id, func.count(NeuralMemoryEdge.id))
            .where(
                NeuralMemoryEdge.user_id == user_id,
                (NeuralMemoryEdge.weight < _EDGE_WEIGHT_MIN)
                | (NeuralMemoryEdge.weight > _EDGE_WEIGHT_MAX),
            )
            .group_by(NeuralMemoryEdge.context_id)
        )
        violations = {cid: int(count or 0) for cid, count in violation_rows.all()}

        memory_rows = await self.db.execute(
            select(Memory.context_id, func.count(Memory.id))
            .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
            .group_by(Memory.context_id)
        )
        memories = {cid: int(count or 0) for cid, count in memory_rows.all()}

        stats: dict[uuid.UUID | None, dict[str, Any]] = {}
        for scope in set(edges_by_scope) | set(violations) | set(memories):
            edges_by_origin = edges_by_scope.get(scope, {})
            total_edges = sum(edges_by_origin.values())
            active = memories.get(scope, 0)
            stats[scope] = {
                "edges_by_origin": edges_by_origin,
                "total_edges": total_edges,
                "weight_violations": violations.get(scope, 0),
                "active_memories": active,
                "edges_per_memory": round(total_edges / active, 4) if active else 0.0,
            }
        return stats

    async def _fetch_usage_counts(self, user_id: str) -> dict[uuid.UUID | None, dict[str, int]]:
        """MCP tool call counts per context over the usage window."""
        since = utcnow() - timedelta(days=_USAGE_WINDOW_DAYS)
        rows = await self.db.execute(
            select(
                UsageStats.context_id,
                UsageStats.endpoint,
                func.count(UsageStats.id),
            )
            .where(
                UsageStats.user_id == user_id,
                UsageStats.created_at >= since,
                UsageStats.endpoint.in_(
                    ["mcp:recall", "mcp:recall_upcoming", "mcp:remember", "mcp:explore"]
                ),
            )
            .group_by(UsageStats.context_id, UsageStats.endpoint)
        )
        usage: dict[uuid.UUID | None, dict[str, int]] = defaultdict(dict)
        for context_id, endpoint, count in rows.all():
            usage[context_id][endpoint.removeprefix("mcp:")] = int(count)
        return dict(usage)

    async def _fetch_config_postures(self, user_id: str) -> dict[uuid.UUID | None, dict[str, int]]:
        """Search-config posture per owned context (0/1 flags per context)."""
        rows = await self.db.execute(
            select(
                ContextSearchConfig.context_id,
                ContextSearchConfig.reinforce_enabled,
                ContextSearchConfig.use_rerank,
            )
            .join(Context, Context.id == ContextSearchConfig.context_id)
            .where(Context.created_by == user_id, Context.deleted_at.is_(None))
        )
        postures: dict[uuid.UUID | None, dict[str, int]] = {}
        for context_id, reinforce_on, rerank_on in rows.all():
            postures[context_id] = {
                "contexts_with_config": 1,
                "reinforce_enabled": int(bool(reinforce_on)),
                "use_rerank": int(bool(rerank_on)),
            }
        return postures

    # -------------------------------------------------------------- grading

    @staticmethod
    def _grade_consolidation(
        window: list[dict[str, Any]], backlog: dict[str, Any]
    ) -> dict[str, Any]:
        """FAIL: latest run failed. WARN: degradation signals in the window."""
        notes: list[dict[str, Any]] = []
        status = STATUS_OK

        latest = window[0] if window else None
        total_failures = sum(r["llm_call_failures"] for r in window)
        degraded = sum(1 for r in window if r["status"] == "degraded")
        failed = sum(1 for r in window if r["status"] == "failed")
        overrides = sum(r["winner_overrides"] for r in window)
        deferred = sum(r["deferred_pairs"] for r in window)

        if latest and latest["status"] == "failed":
            # Total judge failure — the #1177 class; check the sleep LLM config.
            status = STATUS_FAIL
            notes.append(_note("latest_sleep_failed"))
        else:
            if total_failures > 0 or degraded > 0:
                # A partially dead judge grades 'degraded', never silently
                # 'completed' (#1183).
                status = STATUS_WARN
                notes.append(_note("judge_failures", count=total_failures, degraded_runs=degraded))
            if failed > 0:
                # The latest run recovered, but recent instability is worth a look.
                status = STATUS_WARN
                notes.append(_note("failed_runs_recovered", count=failed))
        if deferred > 0:
            # Cluster caps deferring pairs unjudged — dedup may be
            # structurally behind (the #1184 class).
            status = _worst([status, STATUS_WARN])
            notes.append(_note("deferred_pairs", count=deferred))
        if (
            backlog["oldest_days"] is not None
            and backlog["oldest_days"] > _BACKLOG_WARN_DAYS
            and status != STATUS_FAIL
        ):
            # A retention window is likely wanted (sleep_merge_retention_days, #1209).
            status = _worst([status, STATUS_WARN])
            notes.append(
                _note(
                    "merge_backlog_old",
                    oldest_days=backlog["oldest_days"],
                    threshold_days=_BACKLOG_WARN_DAYS,
                )
            )

        return {
            "status": status,
            "metrics": {
                "reports_in_window": len(window),
                "latest_status": latest["status"] if latest else None,
                "llm_call_failures": total_failures,
                "degraded_runs": degraded,
                "failed_runs": failed,
                "winner_overrides": overrides,
                "deferred_pairs": deferred,
                "merge_backlog_count": backlog["count"],
                "merge_backlog_oldest_days": backlog["oldest_days"],
            },
            "notes": notes,
        }

    @staticmethod
    def _grade_graph(stats: dict[str, Any], *, cold_check: bool = True) -> dict[str, Any]:
        """FAIL: weight invariant violated. WARN: suspiciously cold graph."""
        notes: list[dict[str, Any]] = []
        status = STATUS_OK

        if stats["weight_violations"] > 0:
            # The #1197 class of unclamped-accumulation bug.
            status = STATUS_FAIL
            notes.append(
                _note(
                    "edge_weight_violations",
                    count=stats["weight_violations"],
                    min=_EDGE_WEIGHT_MIN,
                    max=_EDGE_WEIGHT_MAX,
                )
            )
        elif (
            cold_check
            and stats["total_edges"] == 0
            and stats["active_memories"] >= _COLD_GRAPH_MIN_MEMORIES
        ):
            # The graph never warmed — check edge-formation gates / sleep_mode.
            status = STATUS_WARN
            notes.append(_note("cold_graph", active_memories=stats["active_memories"]))

        return {"status": status, "metrics": stats, "notes": notes}

    @staticmethod
    def _grade_retrieval(
        usage: dict[str, int],
        posture: dict[str, int],
        *,
        active_memories: int,
        write_only_check: bool = True,
    ) -> dict[str, Any]:
        """Informational; WARN only when memory exists but nothing reads it."""
        notes: list[dict[str, Any]] = []
        status = STATUS_OK
        recalls = usage.get("recall", 0)
        recall_upcoming = usage.get("recall_upcoming", 0)

        if write_only_check and recalls + recall_upcoming == 0 and active_memories > 0:
            status = STATUS_WARN
            notes.append(
                _note(
                    "write_only_store",
                    window_days=_USAGE_WINDOW_DAYS,
                    active_memories=active_memories,
                )
            )

        return {
            "status": status,
            "metrics": {
                "window_days": _USAGE_WINDOW_DAYS,
                "recall_calls": recalls,
                "recall_upcoming_calls": recall_upcoming,
                "remember_calls": usage.get("remember", 0),
                "explore_calls": usage.get("explore", 0),
                **posture,
            },
            "notes": notes,
        }
