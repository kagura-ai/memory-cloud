"""#1211: consolidated memory-health report grading.

Pins the acceptance contract: a simulated judge death flips the
consolidation section to warn/fail (the #1177 class of plausible success can
no longer hide), the graph weight invariant fails deterministically (#1197
class), and healthy inputs grade ok. FAIL fires only on deterministic facts;
degradation signals produce WARN (a false FAIL erodes dashboard trust).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.memory_health_service import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    MemoryHealthService,
)


def _report(status="completed", failures=0, overrides=0, deferred=0):
    return {
        "status": status,
        "llm_call_failures": failures,
        "memories_merged": 0,
        "winner_overrides": overrides,
        "deferred_pairs": deferred,
        "oversize_clusters": 0,
        "started_at": "2026-07-08T00:00:00Z",
    }


def _healthy_graph(**over):
    stats = {
        "edges_by_origin": {"hebbian": 10, "semantic": 5},
        "total_edges": 15,
        "weight_violations": 0,
        "active_memories": 40,
        "edges_per_memory": 0.375,
    }
    stats.update(over)
    return stats


class TestConsolidationGrading:
    def test_healthy_window_is_ok(self) -> None:
        section = MemoryHealthService._grade_consolidation(
            [_report(), _report()], {"count": 0, "oldest_days": None}
        )
        assert section["status"] == STATUS_OK
        assert section["notes"] == []

    def test_latest_failed_run_is_fail(self) -> None:
        """Total judge death (#1177 class) must be a hard FAIL."""
        section = MemoryHealthService._grade_consolidation(
            [_report(status="failed", failures=5), _report()],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_FAIL
        assert any("#1177" in n for n in section["notes"])

    def test_degraded_run_in_window_is_warn(self) -> None:
        section = MemoryHealthService._grade_consolidation(
            [_report(), _report(status="degraded", failures=2)],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_WARN

    def test_latest_degraded_is_warn_not_fail(self) -> None:
        """A degraded LATEST run is partial judge death — WARN, never FAIL."""
        section = MemoryHealthService._grade_consolidation(
            [_report(status="degraded", failures=1), _report()],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_WARN

    def test_past_failed_run_with_recovered_latest_is_warn(self) -> None:
        """A failed run in the window must not hide behind a recovered latest.

        No llm_call_failures and no degraded runs on the failed row (total
        judge death records status only) — the failed status itself carries
        the WARN.
        """
        section = MemoryHealthService._grade_consolidation(
            [_report(), _report(status="failed")],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_WARN
        assert any("failed sleep run" in n for n in section["notes"])

    def test_backlog_at_threshold_is_ok_just_over_warns(self) -> None:
        """The 90-day backlog threshold is strict (>): 90d ok, 91d warn."""
        at = MemoryHealthService._grade_consolidation([_report()], {"count": 10, "oldest_days": 90})
        over = MemoryHealthService._grade_consolidation(
            [_report()], {"count": 10, "oldest_days": 91}
        )
        assert at["status"] == STATUS_OK
        assert over["status"] == STATUS_WARN

    def test_deferred_pairs_warn(self) -> None:
        section = MemoryHealthService._grade_consolidation(
            [_report(deferred=12)], {"count": 0, "oldest_days": None}
        )
        assert section["status"] == STATUS_WARN
        assert any("#1184" in n for n in section["notes"])

    def test_deferred_note_survives_coexisting_warn(self) -> None:
        """Every non-ok contribution gets a note — a judge-failure WARN must
        not swallow the orthogonal deferred-pairs (#1184) explanation."""
        section = MemoryHealthService._grade_consolidation(
            [_report(status="degraded", failures=2, deferred=12)],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_WARN
        assert any("#1183" in n for n in section["notes"])
        assert any("#1184" in n for n in section["notes"])

    def test_old_merge_backlog_warns_and_names_retention(self) -> None:
        section = MemoryHealthService._grade_consolidation(
            [_report()], {"count": 500, "oldest_days": 120}
        )
        assert section["status"] == STATUS_WARN
        assert any("sleep_merge_retention_days" in n for n in section["notes"])

    def test_empty_window_is_ok(self) -> None:
        """No sleep runs yet is not a failure — sleep is opt-in (#558)."""
        section = MemoryHealthService._grade_consolidation([], {"count": 0, "oldest_days": None})
        assert section["status"] == STATUS_OK


class TestGraphGrading:
    def test_healthy_graph_is_ok(self) -> None:
        assert MemoryHealthService._grade_graph(_healthy_graph())["status"] == STATUS_OK

    def test_weight_violation_is_fail(self) -> None:
        """Out-of-bounds edge weights are the #1197 unclamped-accumulation
        class — a deterministic invariant violation, hard FAIL."""
        section = MemoryHealthService._grade_graph(_healthy_graph(weight_violations=3))
        assert section["status"] == STATUS_FAIL
        assert any("#1197" in n for n in section["notes"])

    def test_cold_graph_with_many_memories_warns(self) -> None:
        section = MemoryHealthService._grade_graph(
            _healthy_graph(total_edges=0, edges_by_origin={}, active_memories=100)
        )
        assert section["status"] == STATUS_WARN

    def test_small_cold_store_is_ok(self) -> None:
        section = MemoryHealthService._grade_graph(
            _healthy_graph(total_edges=0, edges_by_origin={}, active_memories=3)
        )
        assert section["status"] == STATUS_OK


class TestRetrievalGrading:
    def test_active_usage_is_ok(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"recall": 42, "remember": 10},
            {"contexts_with_config": 3, "reinforce_enabled": 2, "use_rerank": 0},
            active_memories=100,
        )
        assert section["status"] == STATUS_OK
        assert section["metrics"]["recall_calls"] == 42

    def test_write_only_store_warns(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"remember": 5},
            {"contexts_with_config": 1, "reinforce_enabled": 1, "use_rerank": 0},
            active_memories=50,
        )
        assert section["status"] == STATUS_WARN

    def test_empty_store_is_ok(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {},
            {"contexts_with_config": 0, "reinforce_enabled": 0, "use_rerank": 0},
            active_memories=0,
        )
        assert section["status"] == STATUS_OK


class TestFetchSleepWindowDefaults:
    @pytest.mark.asyncio
    async def test_pre_existing_reports_without_detail_keys_default_to_zero(self) -> None:
        """Reports written before #1198/#1184 added the detail keys (or with
        dedup_result=None) must flatten to zeros, not raise."""
        legacy_none = SimpleNamespace(
            status="completed",
            llm_call_failures=None,
            memories_merged=None,
            dedup_result=None,
            started_at=None,
        )
        legacy_no_details = SimpleNamespace(
            status="completed",
            llm_call_failures=0,
            memories_merged=1,
            dedup_result={"merged": 1},
            started_at=None,
        )
        result = MagicMock()
        result.scalars.return_value.all.return_value = [legacy_none, legacy_no_details]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        window = await MemoryHealthService(db)._fetch_sleep_window("u1")

        assert len(window) == 2
        for row in window:
            assert row["llm_call_failures"] == 0
            assert row["winner_overrides"] == 0
            assert row["deferred_pairs"] == 0
            assert row["oversize_clusters"] == 0


class TestBuildReport:
    @pytest.mark.asyncio
    async def test_overall_is_worst_section(self) -> None:
        """Judge death anywhere makes the OVERALL document non-ok — the
        acceptance criterion: a broken judge flips health within one cycle."""
        svc = MemoryHealthService(AsyncMock())
        with (
            patch.object(
                svc,
                "_fetch_sleep_window",
                new=AsyncMock(return_value=[_report(status="failed", failures=5)]),
            ),
            patch.object(
                svc,
                "_fetch_merge_backlog",
                new=AsyncMock(return_value={"count": 0, "oldest_days": None}),
            ),
            patch.object(svc, "_fetch_graph_stats", new=AsyncMock(return_value=_healthy_graph())),
            patch.object(svc, "_fetch_usage_counts", new=AsyncMock(return_value={"recall": 1})),
            patch.object(
                svc,
                "_fetch_config_posture",
                new=AsyncMock(
                    return_value={
                        "contexts_with_config": 1,
                        "reinforce_enabled": 1,
                        "use_rerank": 0,
                    }
                ),
            ),
        ):
            report = await svc.build_report("admin-user")

        assert report["overall_status"] == STATUS_FAIL
        assert report["sections"]["consolidation"]["status"] == STATUS_FAIL
        assert report["sections"]["graph"]["status"] == STATUS_OK
        assert set(report["sections"]) == {"consolidation", "graph", "retrieval"}
        assert report["generated_at"]
