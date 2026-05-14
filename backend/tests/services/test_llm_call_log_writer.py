"""Unit tests for LlmCallLogWriter (#474).

Mocked-DB tests — match ``test_reporter_cost_grade.py`` style so the
suite runs without a Docker container. Database-level CHECK constraints
and DDL audit live in ``tests/integration/test_llm_call_log_schema.py``.

Coverage:

- Input validation (caller / call_type / paid_by enums, forbidden 'sleep',
  nullability matrix per caller).
- Cost computation happy path (single axis, multi axis).
- Pricing-miss propagation: any unit_type miss → ``cost_usd = 0`` +
  ``call_metadata.pricing_miss = true``.
- Row construction: identity columns, usage columns, and metadata land
  on the inserted ``LLMCallLog`` instance via ``db.add(...)``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.llm_call_log import LLMCallLog
from services.llm_call_log_writer import LlmCallLogWriter


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


@pytest.fixture
def mock_pricing():
    """Pricing service mock — tests configure ``compute_cost_usd`` per case."""
    pricing = MagicMock()
    pricing.compute_cost_usd = AsyncMock()
    return pricing


@pytest.fixture
def writer(mock_db, mock_pricing):
    return LlmCallLogWriter(db=mock_db, pricing=mock_pricing)


def _added_row(mock_db) -> LLMCallLog:
    """Return the LLMCallLog instance passed to ``db.add()`` (exactly one)."""
    rows = [
        call.args[0] for call in mock_db.add.call_args_list if isinstance(call.args[0], LLMCallLog)
    ]
    assert len(rows) == 1, f"expected 1 LLMCallLog row, got {len(rows)}"
    return rows[0]


class TestInputValidation:
    """Defense-in-depth enum + nullability checks at the writer boundary."""

    @pytest.mark.asyncio
    async def test_invalid_caller_raises(self, writer):
        with pytest.raises(ValueError, match="invalid caller"):
            await writer.record(
                caller="bogus",
                call_type="completion",
                provider="anthropic",
                model="claude-sonnet-4-6",
            )

    @pytest.mark.asyncio
    async def test_forbidden_sleep_caller_raises(self, writer):
        """Sleep cost belongs in sleep_reports, not llm_call_log."""
        with pytest.raises(ValueError, match="caller 'sleep' is not allowed"):
            await writer.record(
                caller="sleep",
                call_type="completion",
                provider="anthropic",
                model="claude-sonnet-4-6",
                user_id="u",
                workspace_id=uuid4(),
                context_id=uuid4(),
            )

    @pytest.mark.asyncio
    async def test_invalid_call_type_raises(self, writer):
        with pytest.raises(ValueError, match="invalid call_type"):
            await writer.record(
                caller="admin",
                call_type="audio",
                provider="anthropic",
                model="claude-sonnet-4-6",
            )

    @pytest.mark.asyncio
    async def test_invalid_paid_by_raises(self, writer):
        with pytest.raises(ValueError, match="invalid paid_by"):
            await writer.record(
                caller="admin",
                call_type="completion",
                provider="anthropic",
                model="claude-sonnet-4-6",
                paid_by="user",
            )

    @pytest.mark.asyncio
    async def test_recall_caller_requires_full_identity(self, writer):
        """recall / rerank / ask must carry (user_id, workspace_id, context_id)."""
        with pytest.raises(ValueError, match="missing.*user_id"):
            await writer.record(
                caller="recall",
                call_type="embedding",
                provider="openai",
                model="text-embedding-3-small",
                # no user_id, no workspace_id, no context_id
            )

    @pytest.mark.asyncio
    async def test_admin_caller_allows_null_identity(self, writer, mock_pricing):
        """admin caller may legitimately run without user/workspace/context."""
        mock_pricing.compute_cost_usd.return_value = 0.0
        await writer.record(
            caller="admin",
            call_type="completion",
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        # No exception → identity-optional contract honored.

    @pytest.mark.asyncio
    async def test_negative_usage_value_rejected(self, writer):
        """Negative usage counts raise ValueError, not silent drop.

        Defends against a bad caller passing a negative token count: the
        row would otherwise INSERT with cost_usd=0 (the filter excludes
        the axis from cost computation) and no signal would surface.
        """
        with pytest.raises(ValueError, match="input_tokens=-1 must be >= 0"):
            await writer.record(
                caller="admin",
                call_type="completion",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=-1,
            )


class TestCostComputation:
    """Write-time cost snapshot, single and multi-axis."""

    @pytest.mark.asyncio
    async def test_single_axis_cost(self, writer, mock_pricing, mock_db):
        """embedding call → one pricing lookup + cost on the inserted row."""
        mock_pricing.compute_cost_usd.return_value = 0.0001

        await writer.record(
            caller="recall",
            call_type="embedding",
            provider="openai",
            model="text-embedding-3-small",
            user_id="u",
            workspace_id=uuid4(),
            context_id=uuid4(),
            embedding_tokens=512,
        )

        # Only one priced axis (embedding_tokens), so only one lookup.
        assert mock_pricing.compute_cost_usd.call_count == 1
        kwargs = mock_pricing.compute_cost_usd.call_args.kwargs
        assert kwargs["unit_type"] == "embedding_tokens"
        assert kwargs["units"] == 512

        row = _added_row(mock_db)
        assert row.cost_usd == Decimal("0.0001")
        # Without a pricing miss the metadata flag is absent.
        assert row.call_metadata is None or "pricing_miss" not in row.call_metadata

    @pytest.mark.asyncio
    async def test_multi_axis_cost_sum(self, writer, mock_pricing, mock_db):
        """completion call sums input + output + cached_input pricings."""

        # Return distinct values per axis so the test can verify ordering
        # and that all three were summed.
        async def fake_compute_cost_usd(**kwargs):
            return {
                "input_tokens": 0.5,
                "output_tokens": 1.5,
                "cache_read_tokens": 0.05,
            }[kwargs["unit_type"]]

        mock_pricing.compute_cost_usd.side_effect = fake_compute_cost_usd

        await writer.record(
            caller="ask",
            call_type="completion",
            provider="anthropic",
            model="claude-sonnet-4-6",
            user_id="u",
            workspace_id=uuid4(),
            context_id=uuid4(),
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=10,
        )

        # Three priced axes → three lookups.
        assert mock_pricing.compute_cost_usd.call_count == 3

        row = _added_row(mock_db)
        # 0.5 + 1.5 + 0.05 = 2.05
        assert row.cost_usd == Decimal("2.05")

    @pytest.mark.asyncio
    async def test_zero_and_none_axes_are_skipped(self, writer, mock_pricing):
        """Only positive non-None counts trigger a pricing lookup."""
        mock_pricing.compute_cost_usd.return_value = 0.1

        await writer.record(
            caller="admin",
            call_type="completion",
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=100,  # priced
            output_tokens=0,  # skipped (zero)
            cached_input_tokens=None,  # skipped (None)
        )

        # Only input_tokens → one lookup.
        assert mock_pricing.compute_cost_usd.call_count == 1
        assert mock_pricing.compute_cost_usd.call_args.kwargs["unit_type"] == "input_tokens"

    @pytest.mark.asyncio
    async def test_pricing_miss_collapses_cost_to_zero_with_flag(
        self, writer, mock_pricing, mock_db
    ):
        """Any miss → cost_usd=0 AND call_metadata.pricing_miss=true.

        A partial-sum return path would understate cost silently; the
        flag carries the signal instead.
        """

        # Two axes priced; the second returns None (lookup miss).
        async def partial_miss(**kwargs):
            return 0.5 if kwargs["unit_type"] == "input_tokens" else None

        mock_pricing.compute_cost_usd.side_effect = partial_miss

        await writer.record(
            caller="admin",
            call_type="completion",
            provider="anthropic",
            model="newly-released-model",
            input_tokens=100,
            output_tokens=50,
        )

        row = _added_row(mock_db)
        assert row.cost_usd == Decimal("0")
        assert row.call_metadata == {"pricing_miss": True}

    @pytest.mark.asyncio
    async def test_pricing_miss_preserves_existing_metadata(self, writer, mock_pricing, mock_db):
        """pricing_miss flag merges with caller-supplied metadata; doesn't replace."""
        mock_pricing.compute_cost_usd.return_value = None

        await writer.record(
            caller="admin",
            call_type="embedding",
            provider="cohere",
            model="embed-experimental",
            embedding_tokens=100,
            call_metadata={"request_id": "req_abc"},
        )

        row = _added_row(mock_db)
        assert row.call_metadata == {"request_id": "req_abc", "pricing_miss": True}

    @pytest.mark.asyncio
    async def test_no_priced_axes_writes_zero_cost(self, writer, mock_pricing, mock_db):
        """A degenerate call with no usage counts writes a 0-cost row (no miss flag).

        This is the documented contract — a writer call with no token
        counts at all (e.g. a completion that returned an empty result)
        is valid and stores cost=0 without setting pricing_miss.
        """
        await writer.record(
            caller="admin",
            call_type="completion",
            provider="anthropic",
            model="claude-sonnet-4-6",
        )

        assert mock_pricing.compute_cost_usd.call_count == 0
        row = _added_row(mock_db)
        assert row.cost_usd == Decimal("0")
        assert row.call_metadata is None


class TestRowConstruction:
    """Identity + usage columns land on the inserted LLMCallLog instance."""

    @pytest.mark.asyncio
    async def test_uuid_string_coerced_to_uuid(self, writer, mock_pricing, mock_db):
        """workspace_id / context_id accept str AND UUID; both end up as UUID."""
        mock_pricing.compute_cost_usd.return_value = 0.0

        ws_uuid = uuid4()
        ctx_uuid = uuid4()
        await writer.record(
            caller="recall",
            call_type="embedding",
            provider="openai",
            model="text-embedding-3-small",
            user_id="u",
            workspace_id=str(ws_uuid),  # passed as str
            context_id=ctx_uuid,  # passed as UUID
            embedding_tokens=10,
        )

        row = _added_row(mock_db)
        assert row.workspace_id == ws_uuid
        assert row.context_id == ctx_uuid

    @pytest.mark.asyncio
    async def test_occurred_at_defaults_to_now(self, writer, mock_pricing, mock_db):
        """No occurred_at → writer fills in current UTC."""
        mock_pricing.compute_cost_usd.return_value = 0.0

        await writer.record(
            caller="admin",
            call_type="completion",
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=10,
        )

        row = _added_row(mock_db)
        # ``utcnow()`` returns a naive datetime, not None.
        assert row.occurred_at is not None
        assert row.occurred_at.tzinfo is None

    @pytest.mark.asyncio
    async def test_explicit_occurred_at_passed_to_pricing_and_row(
        self, writer, mock_pricing, mock_db
    ):
        """Caller-supplied occurred_at is used for both pricing lookup and row."""
        mock_pricing.compute_cost_usd.return_value = 0.5
        ts = datetime(2026, 1, 1, 12, 0, 0)

        await writer.record(
            caller="admin",
            call_type="completion",
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=100,
            occurred_at=ts,
        )

        row = _added_row(mock_db)
        assert row.occurred_at == ts
        # Pricing was looked up at the same timestamp (snapshot semantics).
        assert mock_pricing.compute_cost_usd.call_args.kwargs["started_at"] == ts

    @pytest.mark.asyncio
    async def test_flush_called_so_caller_can_read_id(self, writer, mock_pricing, mock_db):
        """db.flush() is awaited so the caller can access row.id."""
        mock_pricing.compute_cost_usd.return_value = 0.0
        await writer.record(
            caller="admin",
            call_type="embedding",
            provider="openai",
            model="text-embedding-3-small",
            embedding_tokens=1,
        )
        mock_db.flush.assert_awaited_once()
