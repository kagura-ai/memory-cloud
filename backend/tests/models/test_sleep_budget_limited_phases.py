"""#3019: the run summary names the phases its budget skipped or cut.

``budget_limited_phases`` is derived at read time from the stored phase
results, and the REST summary (hence the detail) and the MCP history rows
carry it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from models.sleep import SleepReport, budget_limited_phases


def _blob(*, skipped=False, skip_reason=None, details=None, **nested):
    return {"skipped": skipped, "skip_reason": skip_reason, "details": details, **nested}


def _report(**columns):
    base = dict.fromkeys(
        (
            "edge_discovery_result",
            "dedup_result",
            "merge_retention_result",
            "importance_result",
            "consolidation_result",
            "reindex_result",
        )
    )
    return SimpleNamespace(**{**base, **columns})


class TestBudgetLimitedPhases:
    def test_empty_report(self):
        assert budget_limited_phases(_report()) == {}

    def test_skipped_for_budget(self):
        report = _report(dedup_result=_blob(skipped=True, skip_reason="budget_exhausted"))
        assert budget_limited_phases(report) == {"dedup_merge": "skipped"}

    def test_other_skip_reasons_are_not_budget(self):
        report = _report(
            dedup_result=_blob(skipped=True, skip_reason="dedup_disabled"),
            importance_result=_blob(skipped=True, skip_reason="sleep_mode_edges_only"),
        )
        assert budget_limited_phases(report) == {}

    def test_cut(self):
        report = _report(importance_result=_blob(details={"budget_cut": True}))
        assert budget_limited_phases(report) == {"importance_reeval": "cut"}

    def test_not_cut(self):
        report = _report(importance_result=_blob(details={"budget_cut": False}))
        assert budget_limited_phases(report) == {}

    def test_old_consolidation_key(self):
        report = _report(consolidation_result=_blob(details={"llm_pass_cut_for_budget": True}))
        assert budget_limited_phases(report) == {"consolidation": "cut"}

    def test_nested_blobs_and_run_order(self):
        report = _report(
            consolidation_result=_blob(details={"budget_cut": True}),
            edge_discovery_result=_blob(
                details={"budget_cut": True},
                related_index=_blob(skipped=True, skip_reason="budget_exhausted"),
            ),
            merge_retention_result=_blob(
                details={},
                forget_retention=_blob(details={"budget_cut": True}),
                measurement_retention=_blob(skipped=True, skip_reason="budget_exhausted"),
            ),
        )
        result = budget_limited_phases(report)
        assert result == {
            "edge_discovery": "cut",
            "related_index": "skipped",
            "forget_retention": "cut",
            "measurement_retention": "skipped",
            "consolidation": "cut",
        }
        assert list(result) == [
            "edge_discovery",
            "related_index",
            "forget_retention",
            "measurement_retention",
            "consolidation",
        ]

    def test_non_dict_blobs_are_absent(self):
        report = MagicMock()
        report.dedup_result = "not a dict"
        report.importance_result = _blob(details=["budget_cut"])
        assert budget_limited_phases(report) == {}

    def test_model_property(self):
        report = SleepReport(
            user_id="u",
            importance_result=_blob(details={"budget_cut": True}),
        )
        assert report.budget_limited_phases == {"importance_reeval": "cut"}


class TestRestSummary:
    def _row(self, **extra):
        return SimpleNamespace(
            id=uuid4(),
            user_id="u",
            workspace_id=None,
            context_id=None,
            status="completed",
            source="sleep",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=None,
            memories_processed=0,
            edges_created=0,
            memories_merged=0,
            memories_promoted=0,
            memories_flagged=0,
            llm_calls_made=0,
            llm_tokens_used=0,
            llm_call_failures=0,
            **extra,
        )

    def test_summary_carries_the_map(self):
        from api.routes.sleep_reports import SleepReportSummary

        row = self._row(budget_limited_phases={"dedup_merge": "cut"})
        summary = SleepReportSummary.model_validate(row, from_attributes=True)
        assert summary.budget_limited_phases == {"dedup_merge": "cut"}

    def test_a_row_without_the_map_reads_empty(self):
        from api.routes.sleep_reports import SleepReportSummary

        row = self._row(budget_limited_phases=MagicMock())
        summary = SleepReportSummary.model_validate(row, from_attributes=True)
        assert summary.budget_limited_phases == {}

    def test_detail_inherits_it_from_the_orm_row(self):
        from api.routes.sleep_reports import SleepReportDetail

        report = SleepReport(
            id=uuid4(),
            user_id="u",
            status="completed",
            source="sleep",
            started_at=datetime(2026, 1, 1),
            memories_processed=0,
            edges_created=0,
            memories_merged=0,
            memories_promoted=0,
            memories_flagged=0,
            llm_calls_made=0,
            llm_tokens_used=0,
            embedding_calls_made=0,
            llm_call_failures=0,
            dedup_result=_blob(skipped=True, skip_reason="budget_exhausted"),
        )
        detail = SleepReportDetail.model_validate(report, from_attributes=True)
        assert detail.budget_limited_phases == {"dedup_merge": "skipped"}


class TestMcpSummary:
    def test_history_row_carries_the_map(self):
        from mcp_server.tools.sleep import _report_to_detail, _report_to_summary

        report = MagicMock()
        report.started_at = None
        report.completed_at = None
        report.importance_result = _blob(details={"budget_cut": True})
        assert _report_to_summary(report)["budget_limited_phases"] == {"importance_reeval": "cut"}
        assert _report_to_detail(report)["budget_limited_phases"] == {"importance_reeval": "cut"}

    def test_mock_report_reads_empty(self):
        from mcp_server.tools.sleep import _report_to_summary

        report = MagicMock()
        report.started_at = None
        report.completed_at = None
        assert _report_to_summary(report)["budget_limited_phases"] == {}
