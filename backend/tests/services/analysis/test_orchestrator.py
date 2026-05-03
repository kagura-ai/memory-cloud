"""Tests for services/analysis/orchestrator.py.

Covers parameter normalization, pricing resolution, start/run lifecycle,
and failure handling.  Heavy dependencies (vector pull, clustering,
labeler, persist) are mocked.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from models.analysis import MemoryAnalysis
from models.auth import Context, Workspace
from models.llm_pricing import LLMPricing
from services.analysis.orchestrator import (
    AnalysisOrchestrator,
    AnalysisParams,
    _params_iso_to_naive_utc,
    _resolve_pricing_row,
)
from utils.exceptions import ConfigurationError, ConflictError, ValidationError


@pytest_asyncio.fixture(autouse=True)
async def cleanup_llm_pricing(db_session):
    """Reset llm_pricing so tests that assume an empty table or insert
    default rows are deterministic regardless of execution order.
    """
    await db_session.execute(text("TRUNCATE TABLE llm_pricing RESTART IDENTITY CASCADE"))
    await db_session.commit()
    yield


async def _seed_workspace_context(db_session, ws_id, ctx_id, owner_user_id="u1"):
    """Create minimal Workspace + Context rows to satisfy FKs."""
    db_session.add(
        Workspace(
            id=ws_id,
            name="Test Workspace",
            owner_user_id=owner_user_id,
            plan_name="free",
        )
    )
    await db_session.flush()
    db_session.add(
        Context(
            id=ctx_id,
            workspace_id=ws_id,
            name="test-context",
        )
    )
    await db_session.flush()


async def _seed_pricing(db_session) -> LLMPricing:
    """Create a minimal LLMPricing row and return it."""
    # Use a random model name so repeated calls across tests do not collide
    # on the ``uq_llm_pricing_lookup_key`` unique constraint.
    row = LLMPricing(
        provider="openai",
        model=f"gpt-test-{uuid4().hex[:8]}",
        unit_type="input_tokens",
        price_per_unit="0.001",
        currency="USD",
        effective_from=datetime(2024, 1, 1),
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ---------------------------------------------------------------------------
# _params_iso_to_naive_utc
# ---------------------------------------------------------------------------


class TestParamsIsoToNaiveUtc:
    def test_none_returns_none(self) -> None:
        assert _params_iso_to_naive_utc(None) is None

    def test_naive_string_unchanged(self) -> None:
        assert _params_iso_to_naive_utc("2024-01-15T10:30:00") == datetime(2024, 1, 15, 10, 30, 0)

    def test_tz_aware_converted_to_utc(self) -> None:
        assert _params_iso_to_naive_utc("2024-01-15T10:30:00+09:00") == datetime(
            2024, 1, 15, 1, 30, 0
        )

    def test_utc_suffix_strips_tzinfo(self) -> None:
        assert _params_iso_to_naive_utc("2024-01-15T10:30:00+00:00") == datetime(
            2024, 1, 15, 10, 30, 0
        )


# ---------------------------------------------------------------------------
# _resolve_pricing_row
# ---------------------------------------------------------------------------


class TestResolvePricingRow:
    @pytest.mark.asyncio
    async def test_model_id_lookup_miss_raises(self, db_session) -> None:
        with pytest.raises(ValidationError, match="No LLM pricing row"):
            await _resolve_pricing_row(db_session, model_id=99999)

    @pytest.mark.asyncio
    async def test_model_id_lookup_hit(self, db_session) -> None:
        row = LLMPricing(
            provider="openai",
            model="gpt-test",
            unit_type="input_tokens",
            price_per_unit="0.001",
            currency="USD",
            effective_from=datetime(2024, 1, 1),
        )
        db_session.add(row)
        await db_session.flush()

        primary, snapshot = await _resolve_pricing_row(db_session, model_id=row.id)
        assert primary.id == row.id
        assert snapshot["provider"] == "openai"
        assert snapshot["rates"]["input_tokens"] == 0.001

    @pytest.mark.asyncio
    async def test_default_path_empty_raises(self, db_session) -> None:
        with pytest.raises(ConfigurationError, match="Default LLM pricing row not seeded"):
            await _resolve_pricing_row(db_session, model_id=None)

    @pytest.mark.asyncio
    async def test_sibling_rate_filtering(self, db_session) -> None:
        base = datetime(2024, 1, 1)
        for unit_type, price in [("input_tokens", "0.001"), ("output_tokens", "0.002")]:
            db_session.add(
                LLMPricing(
                    provider="openai",
                    model="gpt-5-nano",
                    unit_type=unit_type,
                    price_per_unit=price,
                    currency="USD",
                    effective_from=base,
                )
            )
        # stale historical row with different effective_from
        db_session.add(
            LLMPricing(
                provider="openai",
                model="gpt-5-nano",
                unit_type="input_tokens",
                price_per_unit="0.0001",
                currency="USD",
                effective_from=datetime(2023, 1, 1),
            )
        )
        await db_session.flush()

        primary, snapshot = await _resolve_pricing_row(db_session, model_id=None)
        assert snapshot["rates"]["input_tokens"] == 0.001
        assert snapshot["rates"]["output_tokens"] == 0.002
        assert "cache_read_tokens" not in snapshot["rates"]


# ---------------------------------------------------------------------------
# AnalysisOrchestrator.start
# ---------------------------------------------------------------------------


class TestAnalysisOrchestratorStart:
    @pytest.mark.asyncio
    async def test_byok_missing_raises_configuration_error(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        with patch(
            "services.analysis.orchestrator.assert_openai_byok_key_available",
            side_effect=ConfigurationError("No BYOK key"),
        ):
            with pytest.raises(ConfigurationError, match="No BYOK key"):
                await service.start(
                    workspace_id=uuid4(),
                    context_id=uuid4(),
                    user_id="u1",
                    params=AnalysisParams(),
                )

    @pytest.mark.asyncio
    async def test_idempotency_conflict(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)

        prior = MemoryAnalysis(
            workspace_id=ws_id,
            context_id=ctx_id,
            triggered_by="u1",
            model_id=pricing.id,
            model_snapshot={},
            embedding_model="em",
            params={},
            input_count=0,
            status="running",
            paid_by="byok",
        )
        db_session.add(prior)
        await db_session.flush()

        with patch(
            "services.analysis.orchestrator.assert_openai_byok_key_available",
            return_value=None,
        ):
            with pytest.raises(ConflictError, match="already in progress"):
                await service.start(
                    workspace_id=ws_id,
                    context_id=ctx_id,
                    user_id="u1",
                    params=AnalysisParams(),
                )

    @pytest.mark.asyncio
    async def test_success_creates_running_row(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)

        row = LLMPricing(
            provider="openai",
            model="gpt-5-nano",
            unit_type="input_tokens",
            price_per_unit="0.001",
            currency="USD",
            effective_from=datetime(2024, 1, 1),
        )
        db_session.add(row)
        await db_session.flush()

        with patch(
            "services.analysis.orchestrator.assert_openai_byok_key_available",
            return_value=None,
        ):
            analysis = await service.start(
                workspace_id=ws_id,
                context_id=ctx_id,
                user_id="u1",
                params=AnalysisParams(),
            )

        assert analysis.status == "running"
        assert analysis.paid_by == "byok"
        assert analysis.workspace_id == ws_id


# ---------------------------------------------------------------------------
# AnalysisOrchestrator.run
# ---------------------------------------------------------------------------


class TestAnalysisOrchestratorRun:
    @pytest.mark.asyncio
    async def test_analysis_not_found_raises(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        with pytest.raises(ConflictError, match="not found"):
            await service.run(analysis_id=uuid4())

    @pytest.mark.asyncio
    async def test_wrong_status_raises(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)
        analysis = MemoryAnalysis(
            workspace_id=ws_id,
            context_id=ctx_id,
            triggered_by="u1",
            model_id=pricing.id,
            model_snapshot={},
            embedding_model="em",
            params={},
            input_count=0,
            status="failed",
            paid_by="byok",
        )
        db_session.add(analysis)
        await db_session.flush()

        with pytest.raises(ConflictError, match="expected 'running'"):
            await service.run(analysis_id=analysis.id)

    @pytest.mark.asyncio
    async def test_vector_pull_success_commits(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)
        analysis = MemoryAnalysis(
            workspace_id=ws_id,
            context_id=ctx_id,
            triggered_by="u1",
            model_id=pricing.id,
            model_snapshot={"model": "gpt-5-nano"},
            embedding_model="em",
            params={},
            input_count=0,
            status="running",
            paid_by="byok",
        )
        db_session.add(analysis)
        await db_session.flush()

        fake_pull = MagicMock()
        fake_pull.memories = [MagicMock(), MagicMock()]
        fake_pull.embeddings = [[0.1]]
        fake_pull.embedding_model = "test-model"

        with (
            patch(
                "services.analysis.orchestrator.pull_memories_with_vectors",
                new=AsyncMock(return_value=fake_pull),
            ),
            patch(
                "services.analysis.orchestrator.cluster_high_dim",
                return_value=MagicMock(
                    labels=[],
                    centroids=[],
                    n_clusters=0,
                    silhouette=0.0,
                    size_variance=0.0,
                    outlier_ratio=0.0,
                ),
            ),
            patch(
                "services.analysis.orchestrator.project_to_2d",
                return_value=[],
            ),
            patch(
                "services.analysis.orchestrator.estimate_cost",
                return_value=MagicMock(estimated_cost_cents=42),
            ),
            patch(
                "services.analysis.orchestrator.analysis_labeler.label_clusters",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "services.analysis.orchestrator.persist_results",
                new=AsyncMock(),
            ) as mock_persist,
        ):
            await service.run(analysis_id=analysis.id)

        assert analysis.input_count == 2
        assert analysis.embedding_model == "test-model"
        assert analysis.cost_estimated_cents == 42
        mock_persist.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embedding_mismatch_marks_failed_and_reraises(self, db_session) -> None:
        from services.analysis.vector_pull import EmbeddingMismatchError

        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)
        analysis = MemoryAnalysis(
            workspace_id=ws_id,
            context_id=ctx_id,
            triggered_by="u1",
            model_id=pricing.id,
            model_snapshot={"model": "gpt-5-nano"},
            embedding_model="em",
            params={},
            input_count=0,
            status="running",
            paid_by="byok",
        )
        db_session.add(analysis)
        await db_session.flush()

        with (
            patch(
                "services.analysis.orchestrator.pull_memories_with_vectors",
                new=AsyncMock(side_effect=EmbeddingMismatchError("dims differ")),
            ),
            patch(
                "services.analysis.orchestrator.persist_failure",
                new=AsyncMock(),
            ) as mock_fail,
        ):
            with pytest.raises(EmbeddingMismatchError, match="dims differ"):
                await service.run(analysis_id=analysis.id)

        mock_fail.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generic_exception_marks_failed_and_reraises(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)
        analysis = MemoryAnalysis(
            workspace_id=ws_id,
            context_id=ctx_id,
            triggered_by="u1",
            model_id=pricing.id,
            model_snapshot={"model": "gpt-5-nano"},
            embedding_model="em",
            params={},
            input_count=0,
            status="running",
            paid_by="byok",
        )
        db_session.add(analysis)
        await db_session.flush()

        with (
            patch(
                "services.analysis.orchestrator.pull_memories_with_vectors",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "services.analysis.orchestrator.persist_failure",
                new=AsyncMock(),
            ) as mock_fail,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await service.run(analysis_id=analysis.id)

        mock_fail.assert_awaited_once()


# ---------------------------------------------------------------------------
# _mark_failed
# ---------------------------------------------------------------------------


class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_persist_failure_called_and_committed(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)
        analysis = MemoryAnalysis(
            workspace_id=ws_id,
            context_id=ctx_id,
            triggered_by="u1",
            model_id=pricing.id,
            model_snapshot={},
            embedding_model="em",
            params={},
            input_count=0,
            status="running",
            paid_by="byok",
        )
        db_session.add(analysis)
        await db_session.flush()

        with patch(
            "services.analysis.orchestrator.persist_failure",
            new=AsyncMock(),
        ) as mock_persist:
            await service._mark_failed(analysis, "it broke")

        mock_persist.assert_awaited_once_with(
            db_session, analysis=analysis, error_message="it broke"
        )
