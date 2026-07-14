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
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

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

    def test_end_of_day_shifts_date_only_to_next_day(self) -> None:
        """Regression for #820: a date-only ``to`` value must be shifted
        forward by one day so the SQL ``Memory.created_at < to_dt``
        comparison effectively includes the entire calendar day. Without
        this shift, ``to=2026-05-28`` silently excludes every memory
        ingested on 2026-05-28.
        """
        assert _params_iso_to_naive_utc("2026-05-28", end_of_day=True) == datetime(
            2026, 5, 29, 0, 0, 0
        )

    def test_end_of_day_passes_datetime_inputs_through_unchanged(self) -> None:
        """A precise-time ``to`` (datetime with a time component) keeps
        exclusive-bound semantics; only date-only inputs are reinterpreted.
        """
        # Naive datetime.
        assert _params_iso_to_naive_utc("2026-05-28T15:00:00", end_of_day=True) == datetime(
            2026, 5, 28, 15, 0, 0
        )
        # Tz-aware datetime (JST → UTC normalization still happens; no
        # day shift because the input is not date-only).
        assert _params_iso_to_naive_utc("2026-05-28T15:00:00+09:00", end_of_day=True) == datetime(
            2026, 5, 28, 6, 0, 0
        )

    def test_end_of_day_none_returns_none(self) -> None:
        assert _params_iso_to_naive_utc(None, end_of_day=True) is None

    def test_default_end_of_day_false_preserves_legacy_behavior(self) -> None:
        """Callers that do not opt in (e.g. the ``from`` lower-bound)
        keep the original parse-as-is behavior — no accidental shift.
        """
        assert _params_iso_to_naive_utc("2026-05-28") == datetime(2026, 5, 28, 0, 0, 0)


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
            ) as mock_label_clusters,
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
        # Issue #542: locale fallback to "en" when no DB user
        mock_label_clusters.assert_awaited_once()
        assert mock_label_clusters.call_args.kwargs.get("locale") == "en"

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

    @pytest.mark.asyncio
    async def test_mark_failed_recovers_poisoned_session(self, db_session) -> None:
        """#1240: a DBAPIError mid-run leaves the session transaction in a
        failed state. ``_mark_failed`` must roll back FIRST — without that,
        ``persist_failure``'s ``db.refresh()`` raises PendingRollbackError
        and the row is stranded at status='running' forever.
        """
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
        await db_session.commit()

        # Poison the transaction the way a mid-run DB error does: the
        # failed statement leaves the tx aborted until a rollback.
        with pytest.raises(Exception, match="division by zero"):
            await db_session.execute(text("SELECT 1/0"))

        service = AnalysisOrchestrator(db_session)
        await service._mark_failed(analysis, "boom after poisoned tx")

        await db_session.refresh(analysis)
        assert analysis.status == "failed"
        assert "boom after poisoned tx" in (analysis.error or "")


# ---------------------------------------------------------------------------
# #1240 — start-time date validation (shared with run()'s parser)
# ---------------------------------------------------------------------------


class TestStartValidatesDateParams:
    """Malformed from/to must be rejected BEFORE any row is INSERTed.

    The MCP path delivers raw strings via ``AnalysisParams.extra`` —
    without start-time validation they explode in ``run()`` after the
    row exists at status='running' (quota slot consumed, context
    409-blocked by the idempotency guard). #1240.
    """

    async def _count_rows(self, db_session, ws_id) -> int:
        result = await db_session.execute(
            select(func.count(MemoryAnalysis.id)).where(MemoryAnalysis.workspace_id == ws_id)
        )
        return int(result.scalar() or 0)

    @pytest.mark.asyncio
    async def test_malformed_from_rejected_before_insert(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        await _seed_pricing(db_session)

        with patch(
            "services.analysis.orchestrator.assert_openai_byok_key_available",
            return_value=None,
        ):
            with pytest.raises(ValidationError, match="ISO-8601"):
                await service.start(
                    workspace_id=ws_id,
                    context_id=ctx_id,
                    user_id="u1",
                    params=AnalysisParams(extra={"from": "not-a-date"}),
                )
        assert await self._count_rows(db_session, ws_id) == 0

    @pytest.mark.asyncio
    async def test_malformed_to_rejected_before_insert(self, db_session) -> None:
        service = AnalysisOrchestrator(db_session)
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        await _seed_pricing(db_session)

        with patch(
            "services.analysis.orchestrator.assert_openai_byok_key_available",
            return_value=None,
        ):
            with pytest.raises(ValidationError, match="ISO-8601"):
                await service.start(
                    workspace_id=ws_id,
                    context_id=ctx_id,
                    user_id="u1",
                    params=AnalysisParams(extra={"to": "2026-13-99"}),
                )
        assert await self._count_rows(db_session, ws_id) == 0

    @pytest.mark.asyncio
    async def test_valid_extra_date_strings_accepted(self, db_session) -> None:
        """Valid MCP-shaped raw strings still start a run (no regression)."""
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
                params=AnalysisParams(
                    extra={"from": "2026-05-01", "to": "2026-05-28T23:59:59+09:00"}
                ),
            )
        assert analysis.status == "running"


# ---------------------------------------------------------------------------
# #1240 — one-running-run invariant enforced by the DB, not just a SELECT
# ---------------------------------------------------------------------------


def _make_running_row(ws_id, ctx_id, pricing_id, status="running") -> MemoryAnalysis:
    return MemoryAnalysis(
        workspace_id=ws_id,
        context_id=ctx_id,
        triggered_by="u1",
        model_id=pricing_id,
        model_snapshot={},
        embedding_model="em",
        params={},
        input_count=0,
        status=status,
        paid_by="byok",
    )


class TestOneRunningPartialUniqueIndex:
    @pytest.mark.asyncio
    async def test_second_running_insert_blocked_by_index(self, db_session) -> None:
        """The partial unique index rejects a second 'running' row even
        when the SELECT-then-INSERT guard was raced past (#1240 TOCTOU).
        """
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)

        db_session.add(_make_running_row(ws_id, ctx_id, pricing.id))
        await db_session.flush()
        db_session.add(_make_running_row(ws_id, ctx_id, pricing.id))
        with pytest.raises(IntegrityError, match="uq_memory_analyses_one_running"):
            await db_session.flush()
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_terminal_rows_not_blocked(self, db_session) -> None:
        """Only 'running' participates in the partial index — history rows
        (succeeded/failed/cancelled) may accumulate freely.
        """
        ws_id = uuid4()
        ctx_id = uuid4()
        await _seed_workspace_context(db_session, ws_id, ctx_id)
        pricing = await _seed_pricing(db_session)

        db_session.add(_make_running_row(ws_id, ctx_id, pricing.id, status="succeeded"))
        db_session.add(_make_running_row(ws_id, ctx_id, pricing.id, status="succeeded"))
        db_session.add(_make_running_row(ws_id, ctx_id, pricing.id, status="failed"))
        db_session.add(_make_running_row(ws_id, ctx_id, pricing.id, status="running"))
        await db_session.flush()

    @pytest.mark.asyncio
    async def test_start_translates_integrity_error_to_conflict(
        self, db_session, monkeypatch
    ) -> None:
        """When start() loses the race at flush time (unique violation),
        the caller sees the same 409 ConflictError as the SELECT guard.
        """
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

        async def racing_flush(*args, **kwargs):
            raise IntegrityError(
                "INSERT INTO memory_analyses ...",
                {},
                Exception(
                    "duplicate key value violates unique constraint "
                    '"uq_memory_analyses_one_running"'
                ),
            )

        monkeypatch.setattr(db_session, "flush", racing_flush)
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


class TestStartIntegrityErrorScoping:
    @pytest.mark.asyncio
    async def test_foreign_key_integrity_error_not_translated_to_conflict(
        self, db_session, monkeypatch
    ) -> None:
        """#1240 review: only the one-running unique index means "lost the
        race". An FK violation (context deleted mid-request, pricing row
        gone) must NOT become a 409 telling the user to wait for a run
        that does not exist — it re-raises for the generic 500 path.
        """
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

        async def fk_violating_flush(*args, **kwargs):
            raise IntegrityError(
                "INSERT INTO memory_analyses ...",
                {},
                Exception(
                    'insert or update on table "memory_analyses" violates '
                    'foreign key constraint "memory_analyses_context_id_fkey"'
                ),
            )

        monkeypatch.setattr(db_session, "flush", fk_violating_flush)
        with patch(
            "services.analysis.orchestrator.assert_openai_byok_key_available",
            return_value=None,
        ):
            with pytest.raises(IntegrityError):
                await service.start(
                    workspace_id=ws_id,
                    context_id=ctx_id,
                    user_id="u1",
                    params=AnalysisParams(),
                )


class TestStartIntegrityErrorStructuredDiagnostics:
    @pytest.mark.asyncio
    async def test_structured_constraint_name_translates_to_conflict(
        self, db_session, monkeypatch
    ) -> None:
        """Copilot review (#1249): the asyncpg/psycopg structured path —
        ``orig.constraint_name`` — must drive the 409 translation without
        relying on message-substring matching."""
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

        orig = MagicMock()
        orig.constraint_name = "uq_memory_analyses_one_running"
        orig.__str__ = lambda self: "unique violation (message deliberately nameless)"

        async def racing_flush(*args, **kwargs):
            raise IntegrityError("INSERT INTO memory_analyses ...", {}, orig)

        monkeypatch.setattr(db_session, "flush", racing_flush)
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
