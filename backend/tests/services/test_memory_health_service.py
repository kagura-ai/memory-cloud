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

from auth.workspace_roles import WorkspaceRole
from models.auth import (
    Context,
    IdentityLink,
    UsageStats,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from services.identity_link_service import linked_user_ids
from services.memory_health_service import (
    _READ_TOOLS,
    _WATCHED_ENDPOINTS,
    _WRITE_TOOLS,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    MemoryHealthService,
    _CallerScope,
)
from utils.datetime import utcnow

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

    def test_degraded_without_judge_failures_uses_degraded_runs_note(self) -> None:
        """#1229: a phase crash grades the run 'degraded' with a healthy
        judge — the note must not blame the judge (distinct code)."""
        section = MemoryHealthService._grade_consolidation(
            [_report(), _report(status="degraded", failures=0)],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_WARN
        assert "degraded_runs" in _codes(section)
        assert "judge_failures" not in _codes(section)
        note = next(n for n in section["notes"] if n["code"] == "degraded_runs")
        assert note["params"] == {"count": 1}

    def test_judge_failures_and_phase_degraded_are_independent_notes(self) -> None:
        """PR #1230 review: the two conditions are independent per the docs
        table — a window can hold BOTH a judge-failure degraded run and a
        phase-crash degraded run. The old elif hid the phase crash behind
        the judge note, and the judge note counted ALL degraded runs."""
        section = MemoryHealthService._grade_consolidation(
            [
                _report(status="degraded", failures=2),
                _report(status="degraded", failures=0),
            ],
            {"count": 0, "oldest_days": None},
        )
        assert section["status"] == STATUS_WARN
        assert "judge_failures" in _codes(section)
        assert "degraded_runs" in _codes(section)
        jf = next(n for n in section["notes"] if n["code"] == "judge_failures")
        # Only the run whose judge actually failed — not the phase-crash one.
        assert jf["params"] == {"count": 2, "degraded_runs": 1}
        dr = next(n for n in section["notes"] if n["code"] == "degraded_runs")
        assert dr["params"] == {"count": 1}

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
            heuristics=False,
        )
        assert section["status"] == STATUS_OK

    def test_weight_violation_still_fails_without_heuristics(self) -> None:
        """Disabling the heuristics must not disable the invariant."""
        section = MemoryHealthService._grade_graph(
            _healthy_graph(weight_violations=1), heuristics=False
        )
        assert section["status"] == STATUS_FAIL


_POSTURE_ON = {"has_config": True, "reinforce_enabled": True, "use_rerank": False}
_POSTURE_OFF = {"has_config": False, "reinforce_enabled": False, "use_rerank": False}


class TestRetrievalGrading:
    def test_active_usage_is_ok(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"recall": 42, "successful_reads": 42, "remember": 10, "successful_writes": 10},
            _POSTURE_ON,
            active_memories=100,
        )
        assert section["status"] == STATUS_OK
        assert section["metrics"]["recall_calls"] == 42
        assert section["metrics"]["has_config"] is True

    def test_write_only_store_warns(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"remember": 5, "successful_writes": 5}, _POSTURE_ON, active_memories=50
        )
        assert section["status"] == STATUS_WARN
        note = next(n for n in section["notes"] if n["code"] == "write_only_store")
        assert note["params"]["active_memories"] == 50

    def test_empty_store_is_ok(self) -> None:
        section = MemoryHealthService._grade_retrieval({}, _POSTURE_OFF, active_memories=0)
        assert section["status"] == STATUS_OK

    def test_write_only_check_disabled_for_unattributed_scope(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"remember": 5, "successful_writes": 5},
            _POSTURE_OFF,
            active_memories=50,
            heuristics=False,
        )
        assert section["status"] == STATUS_OK

    # #1822: a context with neither reads nor writes in the window is idle,
    # not write-only — OK with an informational note instead of a WARN.

    def test_idle_store_is_ok_with_note(self) -> None:
        section = MemoryHealthService._grade_retrieval({}, _POSTURE_ON, active_memories=50)
        assert section["status"] == STATUS_OK
        assert _codes(section) == ["idle_store"]
        note = section["notes"][0]
        assert note["params"] == {"window_days": 7, "active_memories": 50}

    def test_idle_store_ignores_non_read_non_write_calls(self) -> None:
        """explore alone is neither a read nor a write for this check."""
        section = MemoryHealthService._grade_retrieval(
            {"explore": 3}, _POSTURE_ON, active_memories=50
        )
        assert section["status"] == STATUS_OK
        assert _codes(section) == ["idle_store"]

    def test_write_only_store_has_no_idle_note(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {"remember": 1, "successful_writes": 1}, _POSTURE_ON, active_memories=50
        )
        assert section["status"] == STATUS_WARN
        assert _codes(section) == ["write_only_store"]
        assert section["metrics"]["successful_write_calls"] == 1

    def test_failed_writes_only_is_idle_not_write_only(self) -> None:
        """remember calls that all failed (quota, permission, validation)
        wrote nothing — the context is idle, not write-only."""
        section = MemoryHealthService._grade_retrieval(
            {"remember": 4}, _POSTURE_ON, active_memories=50
        )
        assert section["status"] == STATUS_OK
        assert _codes(section) == ["idle_store"]
        assert section["metrics"]["remember_calls"] == 4
        assert section["metrics"]["successful_write_calls"] == 0

    def test_empty_store_has_no_idle_note(self) -> None:
        section = MemoryHealthService._grade_retrieval({}, _POSTURE_OFF, active_memories=0)
        assert section["notes"] == []

    def test_idle_note_disabled_for_unattributed_scope(self) -> None:
        section = MemoryHealthService._grade_retrieval(
            {}, _POSTURE_OFF, active_memories=50, heuristics=False
        )
        assert section["status"] == STATUS_OK
        assert section["notes"] == []

    @pytest.mark.parametrize("lane", ["recall_nearby", "recall_upcoming"])
    def test_single_read_lane_is_ok_without_notes(self, lane: str) -> None:
        section = MemoryHealthService._grade_retrieval(
            {lane: 1, "successful_reads": 1}, _POSTURE_ON, active_memories=50
        )
        assert section["status"] == STATUS_OK
        assert section["notes"] == []

    def test_failed_reads_do_not_hide_a_write_only_store(self) -> None:
        """Review on #1822: a recall that failed (quota, crash) read nothing,
        so writes + only-failed reads is still write-only."""
        section = MemoryHealthService._grade_retrieval(
            {"recall": 3, "remember": 2, "successful_writes": 2},
            _POSTURE_ON,
            active_memories=50,
        )
        assert section["status"] == STATUS_WARN
        assert _codes(section) == ["write_only_store"]
        assert section["metrics"]["recall_calls"] == 3
        assert section["metrics"]["successful_read_calls"] == 0

    def test_idle_context_does_not_warn_the_scope(self) -> None:
        """An idle but otherwise healthy context grades OK in every section,
        so it no longer drags the page-level overall to WARN."""
        svc = MemoryHealthService(AsyncMock())
        signals = _signals(
            graphs={_CTX_A: _healthy_graph()},
            postures={_CTX_A: dict(_POSTURE_ON)},
        )

        sections = svc._grade_scope(signals, _CTX_A)

        for section in sections.values():
            assert section["status"] == STATUS_OK
        assert "idle_store" in _codes(sections["retrieval"])


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
            postures={_CTX_A: dict(_POSTURE_ON), _CTX_B: dict(_POSTURE_ON)},
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


def _solo_scope():
    """#1874: the caller scope a report resolves once — here an unlinked
    account, so the mocked session is never queried for it."""
    return patch.object(
        MemoryHealthService,
        "_resolve_caller_scope",
        new=AsyncMock(return_value=_CallerScope(owners=frozenset({"admin-user"}))),
    )


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
        with p1, p2, _solo_scope():
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
        with p1, p2, _solo_scope():
            breakdown = await svc.build_breakdown("admin-user")

        assert breakdown["overall_status"] == STATUS_OK
        assert breakdown["contexts"] == []
        assert breakdown["generated_at"]

    @pytest.mark.asyncio
    async def test_unattributed_entry_appears_only_when_signals_exist(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        with_null = _signals(windows={None: [_report()]})
        p1, p2 = _patched(svc, contexts=[(_CTX_A, "A")], signals=with_null)
        with p1, p2, _solo_scope():
            breakdown = await svc.build_breakdown("admin-user")
        ids = [e["context_id"] for e in breakdown["contexts"]]
        assert ids == [str(_CTX_A), None]

        p1, p2 = _patched(svc, contexts=[(_CTX_A, "A")], signals=_signals())
        with p1, p2, _solo_scope():
            breakdown = await svc.build_breakdown("admin-user")
        assert [e["context_id"] for e in breakdown["contexts"]] == [str(_CTX_A)]


class TestBuildContextReport:
    @pytest.mark.asyncio
    async def test_unowned_context_returns_none(self) -> None:
        """Ownership is a single-row lookup — un-owned (or soft-deleted, or
        unknown) resolves to None and the route maps that to a uniform 404."""
        svc = MemoryHealthService(AsyncMock())
        with (
            _solo_scope(),
            patch.object(svc, "_resolve_owned_context", new=AsyncMock(return_value=None)),
        ):
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
        with (
            _solo_scope(),
            patch.object(svc, "_resolve_owned_context", new=AsyncMock(return_value="Context A")),
            patch.object(svc, "_fetch_signals", new=AsyncMock(return_value=signals)) as fetched,
        ):
            report = await svc.build_context_report("admin-user", _CTX_A)

        assert report is not None
        assert report["context_id"] == str(_CTX_A)
        assert report["context_name"] == "Context A"
        assert report["overall_status"] == STATUS_FAIL
        assert set(report["sections"]) == {"consolidation", "graph", "retrieval"}
        assert report["sections"]["consolidation"]["notes"][0]["code"] == "latest_sleep_failed"
        # The detail path narrows every fetch to the one scope.
        assert fetched.await_args.kwargs.get("scope") == _CTX_A

    @pytest.mark.asyncio
    async def test_unattributed_scope_needs_no_ownership(self) -> None:
        svc = MemoryHealthService(AsyncMock())
        p1, p2 = _patched(svc, contexts=[], signals=_signals())
        with p1, p2, _solo_scope():
            report = await svc.build_context_report("admin-user", None)

        assert report is not None
        assert report["context_id"] is None
        assert report["context_name"] is None
        assert report["overall_status"] == STATUS_OK


class TestFoldOrphanScopes:
    """Signals under a non-owned scope (soft-deleted context, shared context
    created by another member) fold into the unattributed bucket — never
    silently dropped (dropping would hide a Phase-1 WARN/FAIL)."""

    def test_orphan_windows_and_graphs_fold_into_unattributed(self) -> None:
        orphan = uuid.uuid4()
        signals = _signals(
            windows={orphan: [_report(status="failed")]},
            graphs={orphan: _healthy_graph(weight_violations=2)},
            usage={orphan: {"recall": 3}},
            backlogs={orphan: {"count": 5, "oldest_days": 120}},
        )

        folded = MemoryHealthService._fold_orphan_scopes(signals, owned_ids={_CTX_A})

        assert orphan not in folded["windows"]
        assert folded["windows"][None][0]["status"] == "failed"
        assert folded["graphs"][None]["weight_violations"] == 2
        assert folded["usage"][None] == {"recall": 3}
        assert folded["backlogs"][None] == {"count": 5, "oldest_days": 120}

    def test_orphans_merge_with_existing_null_bucket(self) -> None:
        orphan = uuid.uuid4()
        signals = _signals(
            backlogs={
                None: {"count": 1, "oldest_days": 30},
                orphan: {"count": 2, "oldest_days": 200},
            },
            usage={None: {"recall": 1}, orphan: {"recall": 2, "remember": 4}},
        )

        folded = MemoryHealthService._fold_orphan_scopes(signals, owned_ids=set())

        assert folded["backlogs"][None] == {"count": 3, "oldest_days": 200}
        assert folded["usage"][None] == {"recall": 3, "remember": 4}

    def test_owned_scopes_are_untouched(self) -> None:
        signals = _signals(windows={_CTX_A: [_report()]}, usage={_CTX_A: {"recall": 1}})

        folded = MemoryHealthService._fold_orphan_scopes(signals, owned_ids={_CTX_A})

        assert set(folded["windows"]) == {_CTX_A}
        assert set(folded["usage"]) == {_CTX_A}

    @pytest.mark.asyncio
    async def test_orphan_fail_surfaces_as_unattributed_breakdown_entry(self) -> None:
        """The end-to-end guarantee: a deterministic FAIL living in a
        non-owned scope must flip the page-level overall, not vanish."""
        svc = MemoryHealthService(AsyncMock())
        orphan = uuid.uuid4()
        signals = _signals(graphs={orphan: _healthy_graph(weight_violations=1)})
        p1, p2 = _patched(svc, contexts=[(_CTX_A, "A")], signals=signals)
        with p1, p2, _solo_scope():
            breakdown = await svc.build_breakdown("admin-user")

        assert breakdown["overall_status"] == STATUS_FAIL
        unattributed = next(e for e in breakdown["contexts"] if e["context_id"] is None)
        assert unattributed["sections"]["graph"] == STATUS_FAIL


class TestFetchUsageCountsAttribution:
    """#1228: cross-context recall attribution rows (the separate
    context_read_attributions table) must merge into the per-context usage
    counts so a context read ONLY via cross-context recall stops
    false-WARNing write_only_store — the exact false-WARN class the
    grading philosophy forbids."""

    def _db_with_result_sets(self, usage_rows, attribution_rows):
        db = AsyncMock()
        first, second = MagicMock(), MagicMock()
        first.all.return_value = usage_rows
        second.all.return_value = attribution_rows
        db.execute = AsyncMock(side_effect=[first, second])
        return db

    @pytest.mark.asyncio
    async def test_attribution_rows_merge_into_usage_counts(self) -> None:
        db = self._db_with_result_sets(
            usage_rows=[(_CTX_A, "mcp:recall", True, 4), (_CTX_B, "mcp:remember", True, 5)],
            attribution_rows=[(_CTX_B, "mcp:recall", 3)],
        )
        svc = MemoryHealthService(db)

        usage = await svc._fetch_usage_counts("user-1")

        assert usage[_CTX_A] == {"recall": 4, "successful_reads": 4}
        # B keeps its own writes AND gains the attributed reads (attribution
        # rows are only written for a successful recall).
        assert usage[_CTX_B] == {
            "remember": 5,
            "successful_writes": 5,
            "recall": 3,
            "successful_reads": 3,
        }

    @pytest.mark.asyncio
    async def test_successful_writes_count_only_ok_write_rows(self) -> None:
        """#1822: only successful remember / update_memory rows are writes —
        a failed remember (quota 429, permission 403, ...) wrote nothing.
        Raw per-endpoint counts still include failures."""
        db = self._db_with_result_sets(
            usage_rows=[
                (_CTX_A, "mcp:remember", False, 3),
                (_CTX_A, "mcp:remember", True, 2),
                (_CTX_A, "mcp:update_memory", True, 1),
                (_CTX_A, "mcp:recall", False, 1),
                (_CTX_B, "mcp:remember", False, 6),
            ],
            attribution_rows=[],
        )
        svc = MemoryHealthService(db)

        usage = await svc._fetch_usage_counts("user-1")

        # The failed recall counts toward the raw total but not as a read.
        assert usage[_CTX_A] == {
            "remember": 5,
            "successful_writes": 3,
            "update_memory": 1,
            "recall": 1,
        }
        assert usage[_CTX_B] == {"remember": 6}

    @pytest.mark.asyncio
    async def test_same_context_and_endpoint_counts_sum(self) -> None:
        """A context that is BOTH the primary of some calls and attributed
        in others sums the two sources, never overwrites."""
        db = self._db_with_result_sets(
            usage_rows=[(_CTX_A, "mcp:recall", True, 4)],
            attribution_rows=[(_CTX_A, "mcp:recall", 2)],
        )
        svc = MemoryHealthService(db)

        usage = await svc._fetch_usage_counts("user-1")

        assert usage[_CTX_A] == {"recall": 6, "successful_reads": 6}

    @pytest.mark.asyncio
    async def test_attributed_only_context_no_longer_warns_write_only(self) -> None:
        """AC pin (#1228): active memories + reads arriving exclusively via
        cross-context recall attribution → retrieval grades OK, not
        write_only_store."""
        db = self._db_with_result_sets(
            usage_rows=[],
            attribution_rows=[(_CTX_B, "mcp:recall", 3)],
        )
        svc = MemoryHealthService(db)

        usage = await svc._fetch_usage_counts("user-1")
        section = MemoryHealthService._grade_retrieval(
            usage.get(_CTX_B, {}), _POSTURE_ON, active_memories=50
        )

        assert section["status"] == STATUS_OK
        assert "write_only_store" not in _codes(section)


class TestFetchUsageCountsBranches:
    """#1874: the read/write classification branches of ``_fetch_usage_counts``
    that no test pinned, plus ``remember_batch`` (#1853) as a write."""

    def _db(self, usage_rows, attribution_rows):
        db = AsyncMock()
        first, second = MagicMock(), MagicMock()
        first.all.return_value = usage_rows
        second.all.return_value = attribution_rows
        db.execute = AsyncMock(side_effect=[first, second])
        return db

    def test_watched_endpoints_cover_every_read_and_write_tool(self) -> None:
        """The SQL filter is derived from the tool sets, so a new read or
        write tool cannot be classified yet filtered out again."""
        assert set(_WATCHED_ENDPOINTS) == {
            f"mcp:{tool}" for tool in _READ_TOOLS | _WRITE_TOOLS | {"explore"}
        }
        assert "mcp:remember_batch" in _WATCHED_ENDPOINTS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("lane", ["recall_upcoming", "recall_nearby"])
    async def test_successful_read_lane_row_counts_as_a_read(self, lane: str) -> None:
        db = self._db(
            usage_rows=[(_CTX_A, f"mcp:{lane}", True, 2), (_CTX_A, f"mcp:{lane}", False, 1)],
            attribution_rows=[],
        )

        usage = await MemoryHealthService(db)._fetch_usage_counts("user-1")

        assert usage[_CTX_A] == {lane: 3, "successful_reads": 2}

    @pytest.mark.asyncio
    async def test_attribution_row_outside_read_tools_is_not_a_read(self) -> None:
        db = self._db(usage_rows=[], attribution_rows=[(_CTX_A, "mcp:explore", 4)])

        usage = await MemoryHealthService(db)._fetch_usage_counts("user-1")

        assert usage[_CTX_A] == {"explore": 4}

    @pytest.mark.asyncio
    async def test_successful_remember_batch_is_a_write(self) -> None:
        db = self._db(
            usage_rows=[
                (_CTX_A, "mcp:remember_batch", True, 2),
                (_CTX_A, "mcp:remember_batch", False, 1),
            ],
            attribution_rows=[],
        )

        usage = await MemoryHealthService(db)._fetch_usage_counts("user-1")
        section = MemoryHealthService._grade_retrieval(
            usage[_CTX_A], _POSTURE_ON, active_memories=5
        )

        assert usage[_CTX_A] == {"remember_batch": 3, "successful_writes": 2}
        assert section["status"] == STATUS_WARN
        assert _codes(section) == ["write_only_store"]
        assert section["metrics"]["remember_batch_calls"] == 3
        assert section["metrics"]["remember_calls"] == 0
        assert section["metrics"]["successful_write_calls"] == 2


# --------------------------------------------------------------------------
# DB-backed: the real SQL (endpoint filter, linked-account scope) — #1874.
# --------------------------------------------------------------------------


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _workspace(db, owner: str) -> uuid.UUID:
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"mh-ws-{uuid.uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    return ws.id


async def _private_context(db, workspace_id: uuid.UUID, creator: str) -> uuid.UUID:
    ctx = Context(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=f"mh-ctx-{uuid.uuid4().hex[:8]}",
        created_by=creator,
        is_private=True,
    )
    db.add(ctx)
    await db.flush()
    return ctx.id


async def _link(db, *accounts: str) -> None:
    for account in accounts:
        db.add(User(email=f"{account}@test.example", user_id=account, role="user"))
    await db.flush()
    group = uuid.uuid4()
    for account in accounts:
        db.add(IdentityLink(group_id=group, user_id=account, linked_by=accounts[0]))
    await db.flush()


async def _join(db, workspace_id, user_id, role, allowed=None) -> None:
    db.add(
        WorkspaceMember(
            workspace_id=workspace_id, user_id=user_id, role=role, allowed_context_ids=allowed
        )
    )
    await db.flush()


async def _memory(db, user_id: str, workspace_id, context_id) -> None:
    db.add(
        Memory(
            id=uuid.uuid4(),
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="live",
            content="c",
            type="note",
            client="pytest",
            embedding_status="success",
        )
    )
    await db.flush()


async def _usage(db, user_id: str, workspace_id, context_id, endpoint: str, status: int) -> None:
    db.add(
        UsageStats(
            user_id=user_id,
            endpoint=endpoint,
            method="POST",
            status_code=status,
            created_at=utcnow(),
            date=utcnow().date(),
            workspace_id=workspace_id,
            context_id=context_id,
        )
    )
    await db.flush()


class TestRememberBatchIsAWrite:
    """#1874: ``mcp:remember_batch`` rows reach the grading through the real
    endpoint filter."""

    async def _context_with_batch_calls(self, db, status: int) -> tuple[str, uuid.UUID]:
        user = _uid("u")
        ws = await _workspace(db, user)
        ctx = await _private_context(db, ws, user)
        await _memory(db, user, ws, ctx)
        for _ in range(2):
            await _usage(db, user, ws, ctx, "mcp:remember_batch", status)
        return user, ctx

    @pytest.mark.asyncio
    async def test_batch_only_context_grades_write_only(self, db_session) -> None:
        user, ctx = await self._context_with_batch_calls(db_session, 200)

        report = await MemoryHealthService(db_session).build_context_report(user, ctx)

        retrieval = report["sections"]["retrieval"]
        assert retrieval["status"] == STATUS_WARN
        assert _codes(retrieval) == ["write_only_store"]
        assert retrieval["metrics"]["remember_batch_calls"] == 2
        assert retrieval["metrics"]["successful_write_calls"] == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500])
    async def test_failed_batch_calls_leave_the_context_idle(self, db_session, status: int) -> None:
        user, ctx = await self._context_with_batch_calls(db_session, status)

        report = await MemoryHealthService(db_session).build_context_report(user, ctx)

        retrieval = report["sections"]["retrieval"]
        assert retrieval["status"] == STATUS_OK
        assert _codes(retrieval) == ["idle_store"]
        assert retrieval["metrics"]["remember_batch_calls"] == 2
        assert retrieval["metrics"]["successful_write_calls"] == 0


class TestLinkedAccountScopeRespectsMembership:
    """#1874: a link widens ownership only. The report covers a linked
    account's private context when the caller can open it as itself —
    the rule ``PermissionService.resolve_context_for_workspace_read`` applies."""

    async def _linked_private_context(self, db) -> tuple[str, str, uuid.UUID, uuid.UUID]:
        """Caller A linked to B; B owns a workspace with a private context."""
        a, b = _uid("a"), _uid("b")
        await _link(db, a, b)
        ws = await _workspace(db, b)
        ctx = await _private_context(db, ws, b)
        await _memory(db, b, ws, ctx)
        return a, b, ws, ctx

    async def _covered(self, db, caller: str, ctx: uuid.UUID) -> bool:
        svc = MemoryHealthService(db)
        breakdown = await svc.build_breakdown(caller)
        listed = str(ctx) in {e["context_id"] for e in breakdown["contexts"]}
        detail = await svc.build_context_report(caller, ctx)
        # The breakdown and the detail path must agree.
        assert (detail is not None) is listed
        return listed

    @pytest.mark.asyncio
    async def test_non_member_does_not_see_the_linked_private_context(self, db_session) -> None:
        a, _, _, ctx = await self._linked_private_context(db_session)

        assert await self._covered(db_session, a, ctx) is False
        # The route maps this None to the uniform 404.
        assert await MemoryHealthService(db_session).build_context_report(a, ctx) is None

    @pytest.mark.asyncio
    async def test_non_member_gets_no_linked_rows_in_unattributed(self, db_session) -> None:
        """The excluded context's signals do not resurface in the caller's
        unattributed bucket: they are the linked account's rows."""
        a, _, _, _ = await self._linked_private_context(db_session)

        breakdown = await MemoryHealthService(db_session).build_breakdown(a)

        assert breakdown["contexts"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allowed", [None, [], "other"])
    async def test_member_without_an_admitting_whitelist_is_excluded(
        self, db_session, allowed
    ) -> None:
        """NULL = suspended member (Migration 042), [] = no access, and a
        whitelist naming another context omits this one."""
        a, b, ws, ctx = await self._linked_private_context(db_session)
        if allowed == "other":
            allowed = [await _private_context(db_session, ws, b)]
        await _join(db_session, ws, a, WorkspaceRole.MEMBER, allowed)

        assert await self._covered(db_session, a, ctx) is False

    @pytest.mark.asyncio
    async def test_viewer_whose_whitelist_omits_the_context_is_excluded(self, db_session) -> None:
        a, _, ws, ctx = await self._linked_private_context(db_session)
        await _join(db_session, ws, a, WorkspaceRole.VIEWER, [])

        assert await self._covered(db_session, a, ctx) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "whitelisted"),
        [
            (WorkspaceRole.OWNER, False),
            (WorkspaceRole.ADMIN, False),
            (WorkspaceRole.MEMBER, True),
            (WorkspaceRole.VIEWER, True),
            # A viewer with no whitelist reads every context (Migration 042).
            (WorkspaceRole.VIEWER, False),
        ],
    )
    async def test_linked_private_context_the_caller_can_open_is_covered(
        self, db_session, role, whitelisted
    ) -> None:
        """No regression of #1834."""
        a, _, ws, ctx = await self._linked_private_context(db_session)
        await _join(db_session, ws, a, role, [ctx] if whitelisted else None)

        assert await self._covered(db_session, a, ctx) is True
        report = await MemoryHealthService(db_session).build_context_report(a, ctx)
        # The linked account's memory is graded inside the covered context.
        assert report["sections"]["graph"]["metrics"]["active_memories"] == 1

    @pytest.mark.asyncio
    async def test_membership_of_a_deleted_workspace_does_not_count(self, db_session) -> None:
        a, _, ws, ctx = await self._linked_private_context(db_session)
        await _join(db_session, ws, a, WorkspaceRole.ADMIN)
        (await db_session.get(Workspace, ws)).deleted_at = utcnow()
        await db_session.flush()

        assert await self._covered(db_session, a, ctx) is False

    @pytest.mark.asyncio
    async def test_the_callers_own_context_needs_no_membership(self, db_session) -> None:
        """The ``created_by == caller`` branch is unchanged."""
        a, b = _uid("a"), _uid("b")
        await _link(db_session, a, b)
        ws = await _workspace(db_session, b)
        mine = await _private_context(db_session, ws, a)

        assert await self._covered(db_session, a, mine) is True

    @pytest.mark.asyncio
    async def test_link_set_and_memberships_are_resolved_once_per_report(self, db_session) -> None:
        a, _, ws, ctx = await self._linked_private_context(db_session)
        await _join(db_session, ws, a, WorkspaceRole.ADMIN)
        svc = MemoryHealthService(db_session)

        with patch(
            "services.memory_health_service.linked_user_ids", wraps=linked_user_ids
        ) as resolved:
            await svc.build_breakdown(a)
            assert resolved.await_count == 1
            await svc.build_context_report(a, ctx)
            assert resolved.await_count == 2
            await svc.build_context_report(a, None)
            assert resolved.await_count == 3
