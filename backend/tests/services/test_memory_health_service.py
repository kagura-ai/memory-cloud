"""#1211/#1225: per-context memory-health report grading.

Pins the acceptance contracts: a simulated judge death flips the
consolidation section to warn/fail (the #1177 class of plausible success can
no longer hide), the graph weight invariant fails deterministically (#1197
class), healthy inputs grade ok, and — Phase 2 (#1225) — grading is
context-isolated (a WARN-producing signal in context A must not change
context B's grade), context-less signals surface as an explicit
unattributed entry instead of being dropped, and notes are structured
``{code, params}`` records with no issue IDs in the payload.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.memory_health_service import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    MemoryHealthService,
)

_CTX_A = uuid.uuid4()
_CTX_B = uuid.uuid4()


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


def _codes(section) -> list[str]:
    return [n["code"] for n in section["notes"]]


def _signals(**over):
    """A healthy signal-map fixture; override per test."""
    base = {
        "windows": {},
        "backlogs": {},
        "graphs": {},
        "usage": {},
        "postures": {},
    }
    base.update(over)
    return base


class TestConsolidationGrading:
    def test_healthy_window_is_ok(self) -> None:
        section = MemoryHealthService._grade_consolidation(
            [_report(), _report()], {"count": 0, "oldest_days": None}
        )
        assert section["status"] == STATUS_OK
        assert section["notes"] == []

    def test_latest_failed_run_is_fail(self) -> None:
        """Total judge death (the #1177 class) must be a hard FAIL."""
        section = MemoryHealthService._grade_consolidation(
            [_report(status="failed", failures=5), _report()],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_FAIL
        assert "latest_sleep_failed" in _codes(section)

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
        assert "failed_runs_recovered" in _codes(section)

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
        assert "deferred_pairs" in _codes(section)
        note = next(n for n in section["notes"] if n["code"] == "deferred_pairs")
        assert note["params"] == {"count": 12}

    def test_deferred_note_survives_coexisting_warn(self) -> None:
        """Every non-ok contribution gets a note — a judge-failure WARN must
        not swallow the orthogonal deferred-pairs explanation (#1184 class)."""
        section = MemoryHealthService._grade_consolidation(
            [_report(status="degraded", failures=2, deferred=12)],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_WARN
        assert "judge_failures" in _codes(section)
        assert "deferred_pairs" in _codes(section)

    def test_old_merge_backlog_warns_with_threshold_params(self) -> None:
        section = MemoryHealthService._grade_consolidation(
            [_report()], {"count": 500, "oldest_days": 120}
        )
        assert section["status"] == STATUS_WARN
        note = next(n for n in section["notes"] if n["code"] == "merge_backlog_old")
        assert note["params"] == {"oldest_days": 120, "threshold_days": 90}

    def test_empty_window_is_ok(self) -> None:
        """No sleep runs yet is not a failure — sleep is opt-in (#558)."""
        section = MemoryHealthService._grade_consolidation([], {"count": 0, "oldest_days": None})
        assert section["status"] == STATUS_OK

    def test_no_issue_ids_in_notes(self) -> None:
        """#1225 Scope 3: issue references never leak into the payload."""
        section = MemoryHealthService._grade_consolidation(
            [_report(status="failed", failures=5), _report(deferred=3)],
            {"count": 10, "oldest_days": 200},
        )
        assert "#" not in str(section["notes"])


class TestGraphGrading:
    def test_healthy_graph_is_ok(self) -> None:
        assert MemoryHealthService._grade_graph(_healthy_graph())["status"] == STATUS_OK

    def test_weight_violation_is_fail(self) -> None:
        """Out-of-bounds edge weights are the #1197 unclamped-accumulation
        class — a deterministic invariant violation, hard FAIL."""
        section = MemoryHealthService._grade_graph(_healthy_graph(weight_violations=3))
        assert section["status"] == STATUS_FAIL
        note = next(n for n in section["notes"] if n["code"] == "edge_weight_violations")
        assert note["params"]["count"] == 3

    def test_cold_graph_with_many_memories_warns(self) -> None:
        section = MemoryHealthService._grade_graph(
            _healthy_graph(total_edges=0, edges_by_origin={}, active_memories=100)
        )
        assert section["status"] == STATUS_WARN
        assert "cold_graph" in _codes(section)

    def test_small_cold_store_is_ok(self) -> None:
        section = MemoryHealthService._grade_graph(
            _healthy_graph(total_edges=0, edges_by_origin={}, active_memories=3)
        )
        assert section["status"] == STATUS_OK

    def test_cold_check_disabled_for_unattributed_scope(self) -> None:
        """Edges always carry a context, so the unattributed bucket would
        always look cold — the heuristic is skipped there, never a false WARN."""
        section = MemoryHealthService._grade_graph(
            _healthy_graph(total_edges=0, edges_by_origin={}, active_memories=100),
            cold_check=False,
        )
        assert section["status"] == STATUS_OK

    def test_weight_violation_still_fails_without_cold_check(self) -> None:
        """Disabling the heuristic must not disable the invariant."""
        section = MemoryHealthService._grade_graph(
            _healthy_graph(weight_violations=1), cold_check=False
        )
        assert section["status"] == STATUS_FAIL


class TestRetrievalGrading:
    def test_active_usage_is_ok(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"recall": 42, "remember": 10},
            {"contexts_with_config": 1, "reinforce_enabled": 1, "use_rerank": 0},
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
        note = next(n for n in section["notes"] if n["code"] == "write_only_store")
        assert note["params"]["active_memories"] == 50

    def test_empty_store_is_ok(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {},
            {"contexts_with_config": 0, "reinforce_enabled": 0, "use_rerank": 0},
            active_memories=0,
        )
        assert section["status"] == STATUS_OK

    def test_write_only_check_disabled_for_unattributed_scope(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"remember": 5},
            {"contexts_with_config": 0, "reinforce_enabled": 0, "use_rerank": 0},
            active_memories=50,
            write_only_check=False,
        )
        assert section["status"] == STATUS_OK


class TestScopeIsolation:
    """The #1225 isolation contract: grading reads ONLY the scope's slice."""

    def _warn_signals_for_a(self):
        return _signals(
            windows={_CTX_A: [_report(status="degraded", failures=3)]},
            graphs={
                _CTX_A: _healthy_graph(weight_violations=2),
                _CTX_B: _healthy_graph(),
            },
            usage={_CTX_A: {"recall": 1}, _CTX_B: {"recall": 9}},
            postures={
                _CTX_A: {"contexts_with_config": 1, "reinforce_enabled": 1, "use_rerank": 0},
                _CTX_B: {"contexts_with_config": 1, "reinforce_enabled": 1, "use_rerank": 0},
            },
        )

    def test_warn_in_context_a_does_not_change_context_b(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        signals = self._warn_signals_for_a()

        sections_a = svc._grade_scope(signals, _CTX_A)
        sections_b = svc._grade_scope(signals, _CTX_B)

        assert sections_a["consolidation"]["status"] == STATUS_WARN
        assert sections_a["graph"]["status"] == STATUS_FAIL
        for section in sections_b.values():
            assert section["status"] == STATUS_OK

    def test_unattributed_scope_skips_heuristics_but_grades_sleep(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        signals = _signals(
            windows={None: [_report(status="failed")]},
            graphs={None: _healthy_graph(total_edges=0, edges_by_origin={}, active_memories=99)},
            usage={},
        )

        sections = svc._grade_scope(signals, None)

        assert sections["consolidation"]["status"] == STATUS_FAIL
        assert sections["graph"]["status"] == STATUS_OK  # cold-check skipped
        assert sections["retrieval"]["status"] == STATUS_OK  # write-only skipped


class TestFetchSleepWindowDefaults:
    @pytest.mark.asyncio
    async def test_pre_existing_reports_without_detail_keys_default_to_zero(self) -> None:
        """Reports written before #1198/#1184 added the detail keys (or with
        dedup_result=None) must flatten to zeros, not raise."""
        legacy_none = SimpleNamespace(
            context_id=_CTX_A,
            status="completed",
            llm_call_failures=None,
            memories_merged=None,
            dedup_result=None,
            started_at=None,
        )
        legacy_no_details = SimpleNamespace(
            context_id=_CTX_A,
            status="completed",
            llm_call_failures=0,
            memories_merged=1,
            dedup_result={"merged": 1},
            started_at=None,
        )
        result = MagicMock()
        result.all.return_value = [legacy_none, legacy_no_details]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        windows = await MemoryHealthService(db)._fetch_sleep_windows("u1")

        assert set(windows) == {_CTX_A}
        assert len(windows[_CTX_A]) == 2
        for row in windows[_CTX_A]:
            assert row["llm_call_failures"] == 0
            assert row["winner_overrides"] == 0
            assert row["deferred_pairs"] == 0
            assert row["oversize_clusters"] == 0

    @pytest.mark.asyncio
    async def test_null_context_reports_group_under_none(self) -> None:
        """Context-less sleep runs land in the unattributed bucket — never
        dropped (dropping would hide a Phase-1 WARN behind the grouping)."""
        row = SimpleNamespace(
            context_id=None,
            status="failed",
            llm_call_failures=4,
            memories_merged=0,
            dedup_result=None,
            started_at=None,
        )
        result = MagicMock()
        result.all.return_value = [row]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        windows = await MemoryHealthService(db)._fetch_sleep_windows("u1")

        assert set(windows) == {None}
        assert windows[None][0]["status"] == "failed"


def _patched(svc: MemoryHealthService, *, contexts, signals):
    return (
        patch.object(svc, "_fetch_owned_contexts", new=AsyncMock(return_value=contexts)),
        patch.object(svc, "_fetch_signals", new=AsyncMock(return_value=signals)),
    )


class TestBuildBreakdown:
    @pytest.mark.asyncio
    async def test_one_entry_per_context_and_overall_is_worst(self) -> None:
        """Judge death in ONE context makes the page-level overall non-ok
        AND names the context it came from — the #1225 acceptance criterion."""
        svc = MemoryHealthService(AsyncMock())
        signals = _signals(
            windows={_CTX_A: [_report(status="failed", failures=5)]},
            graphs={_CTX_A: _healthy_graph(), _CTX_B: _healthy_graph()},
            usage={_CTX_A: {"recall": 1}, _CTX_B: {"recall": 1}},
        )
        p1, p2 = _patched(
            svc, contexts=[(_CTX_A, "Context A"), (_CTX_B, "Context B")], signals=signals
        )
        with p1, p2:
            breakdown = await svc.build_breakdown("admin-user")

        assert breakdown["overall_status"] == STATUS_FAIL
        by_id = {e["context_id"]: e for e in breakdown["contexts"]}
        assert set(by_id) == {str(_CTX_A), str(_CTX_B)}
        assert by_id[str(_CTX_A)]["overall_status"] == STATUS_FAIL
        assert by_id[str(_CTX_A)]["name"] == "Context A"
        assert by_id[str(_CTX_A)]["sections"]["consolidation"] == STATUS_FAIL
        assert by_id[str(_CTX_B)]["overall_status"] == STATUS_OK

    @pytest.mark.asyncio
    async def test_zero_context_user_is_ok_with_empty_breakdown(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        p1, p2 = _patched(svc, contexts=[], signals=_signals())
        with p1, p2:
            breakdown = await svc.build_breakdown("admin-user")

        assert breakdown["overall_status"] == STATUS_OK
        assert breakdown["contexts"] == []
        assert breakdown["generated_at"]

    @pytest.mark.asyncio
    async def test_unattributed_entry_appears_only_when_signals_exist(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        with_null = _signals(windows={None: [_report()]})
        p1, p2 = _patched(svc, contexts=[(_CTX_A, "A")], signals=with_null)
        with p1, p2:
            breakdown = await svc.build_breakdown("admin-user")
        ids = [e["context_id"] for e in breakdown["contexts"]]
        assert ids == [str(_CTX_A), None]

        p1, p2 = _patched(svc, contexts=[(_CTX_A, "A")], signals=_signals())
        with p1, p2:
            breakdown = await svc.build_breakdown("admin-user")
        assert [e["context_id"] for e in breakdown["contexts"]] == [str(_CTX_A)]


class TestBuildContextReport:
    @pytest.mark.asyncio
    async def test_unowned_context_returns_none(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        p1, p2 = _patched(svc, contexts=[(_CTX_A, "A")], signals=_signals())
        with p1, p2:
            report = await svc.build_context_report("admin-user", _CTX_B)
        assert report is None

    @pytest.mark.asyncio
    async def test_owned_context_returns_scoped_document(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        signals = _signals(
            windows={_CTX_A: [_report(status="failed")]},
            graphs={_CTX_A: _healthy_graph()},
            usage={_CTX_A: {"recall": 2}},
        )
        p1, p2 = _patched(svc, contexts=[(_CTX_A, "Context A")], signals=signals)
        with p1, p2:
            report = await svc.build_context_report("admin-user", _CTX_A)

        assert report is not None
        assert report["context_id"] == str(_CTX_A)
        assert report["context_name"] == "Context A"
        assert report["overall_status"] == STATUS_FAIL
        assert set(report["sections"]) == {"consolidation", "graph", "retrieval"}
        assert report["sections"]["consolidation"]["notes"][0]["code"] == "latest_sleep_failed"

    @pytest.mark.asyncio
    async def test_unattributed_scope_needs_no_ownership(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        p1, p2 = _patched(svc, contexts=[], signals=_signals())
        with p1, p2:
            report = await svc.build_context_report("admin-user", None)

        assert report is not None
        assert report["context_id"] is None
        assert report["context_name"] is None
        assert report["overall_status"] == STATUS_OK
