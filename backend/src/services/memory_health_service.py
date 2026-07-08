"""Consolidated memory-health report (#1211) — runtime self-diagnosis.

The eval program's most transferable lesson: memory failures manifest as
plausible successes (``sleep_summary.ok=true`` while every judge call failed,
#1177). The system already emits the needed signals — ``llm_call_failures``
(#1183), ``winner_overrides`` (#1198), merge counts and cluster stats, graph
stats, soft-delete rows — but they are fragmented across the Sleep report,
graph stats, memory stats and logs. This service assembles them into ONE
thresholded document so a silent-judge-death class of failure surfaces as
WARN/FAIL within one sleep cycle instead of via an external eval program.

Scope rules (gate1/COO):

- **Label-free only**: rates that need gold labels (``stale_only``, P@k)
  belong to the eval harness / #1210 CI gates, not here.
- **No log-derived metrics**: signals that exist only in structlog output
  (e.g. ``reinforce_rerank_applied``) are excluded until they are persisted —
  not rendered as "pending".
- **Conservative thresholds**: FAIL only on deterministic facts (latest run
  failed, invariant violated); WARN on degradation signals. A false FAIL
  erodes dashboard trust (the eval-infra lesson).
- **Self-scoped** (Phase-1, same discipline as the manual sleep trigger):
  the report covers the calling admin's own data partition.
"""

from __future__ import annotations

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


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _STATUS_RANK[s]) if statuses else STATUS_OK


class MemoryHealthService:
    """Builds the consolidated memory-health document for one user partition."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def build_report(self, user_id: str) -> dict[str, Any]:
        """Assemble all sections and grade them.

        Returns a JSON-shaped dict:
        ``{generated_at, overall_status, sections: {name: {status, metrics,
        notes}}}``.
        """
        consolidation = self._grade_consolidation(
            await self._fetch_sleep_window(user_id),
            await self._fetch_merge_backlog(user_id),
        )
        graph = self._grade_graph(await self._fetch_graph_stats(user_id))
        retrieval = self._grade_retrieval(
            await self._fetch_usage_counts(user_id),
            await self._fetch_config_posture(user_id),
            active_memories=graph["metrics"]["active_memories"],
        )

        sections = {
            "consolidation": consolidation,
            "graph": graph,
            "retrieval": retrieval,
        }
        return {
            "generated_at": to_utc_iso(utcnow()),
            "overall_status": _worst([s["status"] for s in sections.values()]),
            "sections": sections,
        }

    # ------------------------------------------------------------- fetchers

    async def _fetch_sleep_window(self, user_id: str) -> list[dict[str, Any]]:
        """Most recent sleep reports (newest first), flattened for grading."""
        result = await self.db.execute(
            select(SleepReport)
            .where(SleepReport.user_id == user_id)
            .order_by(SleepReport.started_at.desc())
            .limit(_REPORT_WINDOW)
        )
        reports = list(result.scalars().all())
        window: list[dict[str, Any]] = []
        for r in reports:
            dedup = r.dedup_result or {}
            details = dedup.get("details") or {}
            window.append(
                {
                    "status": r.status,
                    "llm_call_failures": r.llm_call_failures or 0,
                    "memories_merged": r.memories_merged or 0,
                    "winner_overrides": details.get("winner_overrides", 0),
                    "deferred_pairs": details.get("deferred_pairs", 0),
                    "oversize_clusters": details.get("oversize_clusters", 0),
                    "started_at": to_utc_iso(r.started_at) if r.started_at else None,
                }
            )
        return window

    async def _fetch_merge_backlog(self, user_id: str) -> dict[str, Any]:
        """Soft-deleted merge losers: count + oldest age in days."""
        result = await self.db.execute(
            select(func.count(Memory.id), func.min(Memory.deleted_at)).where(
                Memory.user_id == user_id,
                Memory.deleted_by == "sleep_maintenance",
                Memory.deleted_at.is_not(None),
            )
        )
        count, oldest = result.one()
        oldest_days = None
        if oldest is not None:
            oldest_days = max(0, (utcnow() - oldest).days)
        return {"count": int(count or 0), "oldest_days": oldest_days}

    async def _fetch_graph_stats(self, user_id: str) -> dict[str, Any]:
        """Edge composition by origin, weight-invariant violations, density."""
        origin_rows = await self.db.execute(
            select(NeuralMemoryEdge.origin, func.count(NeuralMemoryEdge.id))
            .where(NeuralMemoryEdge.user_id == user_id)
            .group_by(NeuralMemoryEdge.origin)
        )
        edges_by_origin = {origin: int(count) for origin, count in origin_rows.all()}

        violations = await self.db.execute(
            select(func.count(NeuralMemoryEdge.id)).where(
                NeuralMemoryEdge.user_id == user_id,
                (NeuralMemoryEdge.weight < _EDGE_WEIGHT_MIN)
                | (NeuralMemoryEdge.weight > _EDGE_WEIGHT_MAX),
            )
        )
        weight_violations = int(violations.scalar_one() or 0)

        memories = await self.db.execute(
            select(func.count(Memory.id)).where(
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
            )
        )
        active_memories = int(memories.scalar_one() or 0)

        total_edges = sum(edges_by_origin.values())
        return {
            "edges_by_origin": edges_by_origin,
            "total_edges": total_edges,
            "weight_violations": weight_violations,
            "active_memories": active_memories,
            "edges_per_memory": (
                round(total_edges / active_memories, 4) if active_memories else 0.0
            ),
        }

    async def _fetch_usage_counts(self, user_id: str) -> dict[str, int]:
        """MCP tool call counts over the usage window, keyed by tool name."""
        since = utcnow() - timedelta(days=_USAGE_WINDOW_DAYS)
        rows = await self.db.execute(
            select(UsageStats.endpoint, func.count(UsageStats.id))
            .where(
                UsageStats.user_id == user_id,
                UsageStats.created_at >= since,
                UsageStats.endpoint.in_(["mcp:recall", "mcp:remember", "mcp:explore"]),
            )
            .group_by(UsageStats.endpoint)
        )
        return {endpoint.removeprefix("mcp:"): int(count) for endpoint, count in rows.all()}

    async def _fetch_config_posture(self, user_id: str) -> dict[str, int]:
        """Search-config posture across the user's contexts."""
        rows = await self.db.execute(
            select(
                func.count(ContextSearchConfig.id),
                func.count(ContextSearchConfig.id).filter(
                    ContextSearchConfig.reinforce_enabled.is_(True)
                ),
                func.count(ContextSearchConfig.id).filter(ContextSearchConfig.use_rerank.is_(True)),
            )
            .select_from(ContextSearchConfig)
            .join(Context, Context.id == ContextSearchConfig.context_id)
            .where(Context.created_by == user_id)
        )
        total, reinforce_on, rerank_on = rows.one()
        return {
            "contexts_with_config": int(total or 0),
            "reinforce_enabled": int(reinforce_on or 0),
            "use_rerank": int(rerank_on or 0),
        }

    # -------------------------------------------------------------- grading

    @staticmethod
    def _grade_consolidation(
        window: list[dict[str, Any]], backlog: dict[str, Any]
    ) -> dict[str, Any]:
        """FAIL: latest run failed. WARN: degradation signals in the window."""
        notes: list[str] = []
        status = STATUS_OK

        latest = window[0] if window else None
        total_failures = sum(r["llm_call_failures"] for r in window)
        degraded = sum(1 for r in window if r["status"] == "degraded")
        failed = sum(1 for r in window if r["status"] == "failed")
        overrides = sum(r["winner_overrides"] for r in window)
        deferred = sum(r["deferred_pairs"] for r in window)

        if latest and latest["status"] == "failed":
            status = STATUS_FAIL
            notes.append(
                "latest sleep run FAILED (total judge failure — the #1177 class); "
                "check the sleep LLM configuration"
            )
        elif total_failures > 0 or degraded > 0:
            status = STATUS_WARN
            notes.append(
                f"judge failures in the window: {total_failures} across "
                f"{degraded} degraded run(s) — a partially dead judge grades "
                "'degraded', never silently 'completed' (#1183)"
            )
        if deferred > 0 and status == STATUS_OK:
            status = STATUS_WARN
            notes.append(
                f"{deferred} candidate pair(s) deferred unjudged (cluster caps) — "
                "dedup may be structurally behind (#1184 class)"
            )
        if (
            backlog["oldest_days"] is not None
            and backlog["oldest_days"] > _BACKLOG_WARN_DAYS
            and status != STATUS_FAIL
        ):
            status = _worst([status, STATUS_WARN])
            notes.append(
                f"oldest merge loser is {backlog['oldest_days']}d old "
                f"(> {_BACKLOG_WARN_DAYS}d) — consider a retention window "
                "(sleep_merge_retention_days, #1209)"
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
    def _grade_graph(stats: dict[str, Any]) -> dict[str, Any]:
        """FAIL: weight invariant violated. WARN: suspiciously cold graph."""
        notes: list[str] = []
        status = STATUS_OK

        if stats["weight_violations"] > 0:
            status = STATUS_FAIL
            notes.append(
                f"{stats['weight_violations']} edge(s) outside the weight bounds "
                f"[{_EDGE_WEIGHT_MIN}, {_EDGE_WEIGHT_MAX}] — the #1197 class of "
                "unclamped-accumulation bug"
            )
        elif stats["total_edges"] == 0 and stats["active_memories"] >= _COLD_GRAPH_MIN_MEMORIES:
            status = STATUS_WARN
            notes.append(
                f"{stats['active_memories']} active memories but zero edges — "
                "the graph never warmed (check edge-formation gates / sleep_mode)"
            )

        return {"status": status, "metrics": stats, "notes": notes}

    @staticmethod
    def _grade_retrieval(
        usage: dict[str, int],
        posture: dict[str, int],
        *,
        active_memories: int,
    ) -> dict[str, Any]:
        """Informational; WARN only when memory exists but nothing reads it."""
        notes: list[str] = []
        status = STATUS_OK
        recalls = usage.get("recall", 0)

        if recalls == 0 and active_memories > 0:
            status = STATUS_WARN
            notes.append(
                f"no recall() calls in the last {_USAGE_WINDOW_DAYS}d despite "
                f"{active_memories} active memories — the store is write-only "
                "right now"
            )

        return {
            "status": status,
            "metrics": {
                "window_days": _USAGE_WINDOW_DAYS,
                "recall_calls": recalls,
                "remember_calls": usage.get("remember", 0),
                "explore_calls": usage.get("explore", 0),
                **posture,
            },
            "notes": notes,
        }
