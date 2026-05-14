"""Cost-grade reporter tests (Issue #471).

Companions to ``test_reporter.py`` — these specifically cover the new
behaviors:

- ``LLMCallBreakdown.add_call`` accumulation semantics.
- Per-(phase, provider, model) child rows are written to
  ``sleep_report_llm_usage`` when ``llm_breakdown`` is populated.
- Embedding scalar columns on ``sleep_reports`` are populated when any
  phase reports an embedding provider/model/tokens triple.
- Legacy roll-up columns (``llm_calls_made``, ``llm_tokens_used``,
  ``embedding_calls_made``) stay populated for back-compat — the
  pre-#471 contract is preserved.

These run as plain unit tests with a mocked DB (matching the existing
``test_reporter.py`` style), so no Docker container is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.sleep import SleepReportLLMUsage
from services.sleep.reporter import (
    LLMCallBreakdown,
    PhaseResult,
    SleepReporter,
)


class TestLLMCallBreakdown:
    def test_default_values(self):
        b = LLMCallBreakdown(provider="anthropic", model="claude-sonnet-4-6")
        assert b.input_tokens == 0
        assert b.output_tokens == 0
        assert b.cached_input_tokens == 0
        assert b.calls == 0
        assert b.tokenizer_version is None
        assert b.total_tokens == 0

    def test_add_call_accumulates(self):
        b = LLMCallBreakdown(provider="anthropic", model="claude-sonnet-4-6")
        b.add_call(input_tokens=100, output_tokens=50, cached_input_tokens=10)
        b.add_call(input_tokens=200, output_tokens=80, cached_input_tokens=20)
        assert b.input_tokens == 300
        assert b.output_tokens == 130
        assert b.cached_input_tokens == 30
        assert b.calls == 2
        assert b.total_tokens == 460  # 300 + 130 + 30


class TestReporterCostGradeChildRows:
    """Reporter writes ``sleep_report_llm_usage`` rows from ``llm_breakdown``."""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        return db

    @pytest.fixture
    def reporter(self, mock_db):
        return SleepReporter(mock_db)

    @pytest.mark.asyncio
    async def test_complete_report_writes_child_rows(self, reporter, mock_db):
        """One ``SleepReportLLMUsage`` row per (phase, provider, model) breakdown."""
        report = MagicMock()
        report.id = uuid4()
        report.embedding_provider = None
        report.embedding_model = None

        edge_breakdown = LLMCallBreakdown(
            provider="openai",
            model="gpt-5-nano",
            input_tokens=400,
            output_tokens=120,
            cached_input_tokens=50,
            calls=3,
        )
        dedup_breakdown = LLMCallBreakdown(
            provider="openai",
            model="gpt-5-nano",
            input_tokens=200,
            output_tokens=60,
            cached_input_tokens=0,
            calls=2,
        )

        results = [
            PhaseResult(
                phase_name="edge_discovery",
                llm_calls_used=3,
                tokens_used=570,  # 400 + 120 + 50
                memories_processed=20,
                llm_breakdown=[edge_breakdown],
            ),
            PhaseResult(
                phase_name="dedup_merge",
                llm_calls_used=2,
                tokens_used=260,  # 200 + 60
                memories_processed=10,
                llm_breakdown=[dedup_breakdown],
            ),
            # Reindex contributes no LLM but populates embedding triple.
            PhaseResult(
                phase_name="reindex",
                embedding_calls_used=8,
                memories_processed=8,
                embedding_provider="openai",
                embedding_model="text-embedding-3-small",
                embedding_tokens=4096,
            ),
        ]

        await reporter.complete_report(report, results)

        # Two LLM child rows written (one per phase × model).
        added = [
            call.args[0]
            for call in mock_db.add.call_args_list
            if isinstance(call.args[0], SleepReportLLMUsage)
        ]
        assert len(added) == 2
        phase_names = {row.phase for row in added}
        assert phase_names == {"edge_discovery", "dedup_merge"}

        edge_row = next(r for r in added if r.phase == "edge_discovery")
        assert edge_row.provider == "openai"
        assert edge_row.model == "gpt-5-nano"
        assert edge_row.input_tokens == 400
        assert edge_row.output_tokens == 120
        assert edge_row.cached_input_tokens == 50
        assert edge_row.calls == 3

        # Embedding scalar columns populated from reindex.
        assert report.embedding_provider == "openai"
        assert report.embedding_model == "text-embedding-3-small"
        assert report.embedding_tokens == 4096

        # Legacy roll-up columns stay populated for back-compat.
        assert report.llm_calls_made == 5  # 3 + 2
        assert report.llm_tokens_used == 830  # 570 + 260
        assert report.embedding_calls_made == 8

    @pytest.mark.asyncio
    async def test_complete_report_no_breakdown_no_child_rows(self, reporter, mock_db):
        """Phases without ``llm_breakdown`` produce no child rows.

        The legacy roll-up still aggregates from
        ``llm_calls_used`` / ``tokens_used`` so legacy tests stay green
        and unmigrated phases still report sensible totals.
        """
        report = MagicMock()
        report.id = uuid4()
        report.embedding_provider = None
        report.embedding_model = None

        results = [
            PhaseResult(
                phase_name="edge_discovery",
                llm_calls_used=5,
                tokens_used=500,
                memories_processed=20,
                # No llm_breakdown — pre-#471 phase.
            ),
        ]

        await reporter.complete_report(report, results)

        # No SleepReportLLMUsage rows added.
        added_usage = [
            call.args[0]
            for call in mock_db.add.call_args_list
            if isinstance(call.args[0], SleepReportLLMUsage)
        ]
        assert added_usage == []

        # Legacy roll-up still populated (back-compat path).
        assert report.llm_calls_made == 5
        assert report.llm_tokens_used == 500

    @pytest.mark.asyncio
    async def test_complete_report_aggregates_phase_1_2_embedding(self, reporter):
        """#475 PR-1: phases 1 and 2 now contribute embedding usage to
        the cost-grade roll-up. Prior to PR-1 only reindex populated
        ``result.embedding_*`` and the roll-up was reindex-only by
        accident; this test pins the post-PR-1 contract that the
        reporter correctly sums across all phases that report embedding
        usage and that the per-phase JSONB blob carries the breakdown
        (Option C audit trail).
        """
        report = MagicMock()
        report.id = uuid4()
        report.embedding_provider = None
        report.embedding_model = None
        report.edge_discovery_result = None
        report.dedup_result = None
        report.reindex_result = None

        results = [
            PhaseResult(
                phase_name="edge_discovery",
                memories_processed=20,
                embedding_calls_used=20,
                embedding_tokens=2000,
                embedding_provider="openai",
                embedding_model="text-embedding-3-small",
            ),
            PhaseResult(
                phase_name="dedup_merge",
                memories_processed=10,
                embedding_calls_used=10,
                embedding_tokens=1000,
                embedding_provider="openai",
                embedding_model="text-embedding-3-small",
            ),
            PhaseResult(
                phase_name="reindex",
                memories_processed=8,
                embedding_calls_used=8,
                embedding_tokens=4096,
                embedding_provider="openai",
                embedding_model="text-embedding-3-small",
            ),
        ]

        await reporter.complete_report(report, results)

        # Embedding scalar columns aggregate across all three phases.
        assert report.embedding_calls_made == 38  # 20 + 10 + 8
        assert report.embedding_tokens == 7096  # 2000 + 1000 + 4096
        # Provider / model captured from the first phase with non-empty
        # pair (edge_discovery, which runs first in phase order).
        assert report.embedding_provider == "openai"
        assert report.embedding_model == "text-embedding-3-small"

        # Per-phase JSONB blobs carry the embedding breakdown (Option C).
        assert report.edge_discovery_result["embedding_calls"] == 20
        assert report.edge_discovery_result["embedding_tokens"] == 2000
        assert report.dedup_result["embedding_calls"] == 10
        assert report.dedup_result["embedding_tokens"] == 1000
        assert report.reindex_result["embedding_calls"] == 8
        assert report.reindex_result["embedding_tokens"] == 4096
