"""Unit tests for ``services.embedding_spend_cap_service`` (Issue #709).

Covers the cap-check / spend-record / alert-dedup contract that gates BYOK
embedding spend per-workspace. Redis is fully mocked (the resource-quota
test pattern), Email is replaced with an ``AsyncMock`` so we can assert
dispatch counts without touching the global singleton.

The ``EmbeddingSpendCapService`` itself is the unit under test — we do NOT
exercise ``EmbeddingService.embed_with_usage`` here; that's reserved for
integration tests against real Postgres + Redis.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.embedding_spend_cap_service import (
    _DAILY_TTL_SECONDS,
    _MICRO_USD,
    EmbeddingSpendCapService,
)
from utils.exceptions import EmbeddingSpendCapExceeded, RedisError


def _make_workspace(
    *,
    daily_override: Decimal | None = None,
    monthly_override: Decimal | None = None,
    tier_daily: float | None = 10.0,
    tier_monthly: float | None = 300.0,
    name: str = "ws-cap-test",
    owner_user_id: str = "user-abc",
):
    """Build a mock ``Workspace`` row exposing the cap-resolution surface.

    Only the attributes that ``EmbeddingSpendCapService`` reads are
    populated — the real model is large and this keeps the fixture focused.
    """
    ws = MagicMock()
    ws.id = uuid4()
    ws.name = name
    ws.owner_user_id = owner_user_id
    ws.embedding_daily_cap_usd = daily_override
    ws.embedding_monthly_cap_usd = monthly_override
    # Mirror Workspace.effective_* resolution: override beats tier default.
    if daily_override is not None:
        ws.effective_embedding_daily_cap_usd = Decimal(str(daily_override))
    elif tier_daily is not None:
        ws.effective_embedding_daily_cap_usd = Decimal(str(tier_daily))
    else:
        ws.effective_embedding_daily_cap_usd = None
    if monthly_override is not None:
        ws.effective_embedding_monthly_cap_usd = Decimal(str(monthly_override))
    elif tier_monthly is not None:
        ws.effective_embedding_monthly_cap_usd = Decimal(str(tier_monthly))
    else:
        ws.effective_embedding_monthly_cap_usd = None
    return ws


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def mock_email():
    email = MagicMock()
    email.send_embedding_spend_alert = AsyncMock(return_value=True)
    return email


@pytest.fixture
def service(mock_db, mock_email):
    return EmbeddingSpendCapService(mock_db, email=mock_email)


@pytest.fixture
def redis_mocks():
    """Patch ``incrby_counter`` / ``get_cache`` / ``get_redis_client`` on the service module."""
    with (
        patch(
            "services.embedding_spend_cap_service.incrby_counter",
            new_callable=AsyncMock,
        ) as mock_incr,
        patch(
            "services.embedding_spend_cap_service.get_cache",
            new_callable=AsyncMock,
        ) as mock_get,
        patch(
            "services.embedding_spend_cap_service.get_redis_client",
        ) as mock_client_factory,
    ):
        redis_client = MagicMock()
        redis_client.set = AsyncMock(return_value=True)  # SETNX wins by default
        mock_client_factory.return_value = redis_client
        yield mock_get, mock_incr, redis_client


class TestCheckCapOrRaise:
    @pytest.mark.asyncio
    async def test_no_cap_configured_is_noop(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=None, tier_monthly=None)
        mock_get, _, _ = redis_mocks
        await service.check_cap_or_raise(ws)
        # No cap → never even reads Redis
        mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_cap_passes(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=10.0)
        mock_get, _, _ = redis_mocks
        # Current spend = $4.99 (well under $10)
        mock_get.return_value = str(int(Decimal("4.99") * _MICRO_USD))
        await service.check_cap_or_raise(ws)  # no raise

    @pytest.mark.asyncio
    async def test_at_daily_cap_raises(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=10.0)
        mock_get, _, _ = redis_mocks
        mock_get.return_value = str(int(Decimal("10.00") * _MICRO_USD))
        with pytest.raises(EmbeddingSpendCapExceeded) as exc_info:
            await service.check_cap_or_raise(ws)
        assert exc_info.value.details["period"] == "daily"
        assert exc_info.value.status_code == 429
        assert exc_info.value.error_code == "QUOTA-002"

    @pytest.mark.asyncio
    async def test_over_monthly_cap_raises_even_if_daily_ok(self, service, redis_mocks, mock_db):
        ws = _make_workspace(tier_daily=10.0, tier_monthly=100.0)
        mock_get, _, _ = redis_mocks

        # First call (daily) returns under-cap, second (monthly) returns over-cap.
        # ``check_cap_or_raise`` reads daily first then monthly.
        async def side(key):
            if "daily" in key:
                return str(int(Decimal("1.00") * _MICRO_USD))
            return str(int(Decimal("100.00") * _MICRO_USD))

        mock_get.side_effect = side
        with pytest.raises(EmbeddingSpendCapExceeded) as exc_info:
            await service.check_cap_or_raise(ws)
        assert exc_info.value.details["period"] == "monthly"

    @pytest.mark.asyncio
    async def test_redis_read_failure_fails_open(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=10.0)
        mock_get, _, _ = redis_mocks
        mock_get.side_effect = RuntimeError("redis down")
        # Fails open: ``_read_spend`` returns 0 on any exception, so the call passes.
        await service.check_cap_or_raise(ws)


class TestRecordSpend:
    @pytest.mark.asyncio
    async def test_cost_zero_is_noop(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=10.0)
        _, mock_incr, _ = redis_mocks
        await service.record_spend(ws, Decimal("0"))
        mock_incr.assert_not_called()

    @pytest.mark.asyncio
    async def test_increment_under_threshold_no_alert(self, service, redis_mocks, mock_email):
        ws = _make_workspace(tier_daily=10.0, tier_monthly=300.0)
        _, mock_incr, redis_client = redis_mocks
        # Post-INCR spend = $1 (10% of daily, well under 80%)
        mock_incr.return_value = int(Decimal("1.00") * _MICRO_USD)
        await service.record_spend(ws, Decimal("1.00"))
        # Incremented twice (daily + monthly)
        assert mock_incr.call_count == 2
        # No alert email
        mock_email.send_embedding_spend_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_80_percent_threshold_fires_alert(
        self, service, redis_mocks, mock_email, mock_db
    ):
        ws = _make_workspace(tier_daily=10.0, tier_monthly=None)
        _, mock_incr, redis_client = redis_mocks
        # Post-INCR spend = $8 (80% of $10 daily)
        mock_incr.return_value = int(Decimal("8.00") * _MICRO_USD)
        # Owner email lookup
        owner_result = MagicMock()
        owner_result.scalar_one_or_none = MagicMock(return_value="owner@example.com")
        mock_db.execute.return_value = owner_result

        await service.record_spend(ws, Decimal("0.01"))
        # ``create_task`` is fire-and-forget; let the loop drain.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        mock_email.send_embedding_spend_alert.assert_awaited_once()
        kwargs = mock_email.send_embedding_spend_alert.await_args.kwargs
        assert kwargs["threshold_pct"] == 80
        assert kwargs["period"] == "daily"

    @pytest.mark.asyncio
    async def test_100_percent_threshold_fires_alert(
        self, service, redis_mocks, mock_email, mock_db
    ):
        ws = _make_workspace(tier_daily=10.0, tier_monthly=None)
        _, mock_incr, _ = redis_mocks
        mock_incr.return_value = int(Decimal("10.00") * _MICRO_USD)
        owner_result = MagicMock()
        owner_result.scalar_one_or_none = MagicMock(return_value="owner@example.com")
        mock_db.execute.return_value = owner_result

        await service.record_spend(ws, Decimal("0.01"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        kwargs = mock_email.send_embedding_spend_alert.await_args.kwargs
        # 100% wins over 80% — highest crossed threshold reports.
        assert kwargs["threshold_pct"] == 100

    @pytest.mark.asyncio
    async def test_alert_dedup_setnx_loses(self, service, redis_mocks, mock_email, mock_db):
        ws = _make_workspace(tier_daily=10.0, tier_monthly=None)
        _, mock_incr, redis_client = redis_mocks
        mock_incr.return_value = int(Decimal("8.00") * _MICRO_USD)
        # SETNX returns False: dedup key was already set (alert already sent today).
        redis_client.set.return_value = False
        owner_result = MagicMock()
        owner_result.scalar_one_or_none = MagicMock(return_value="owner@example.com")
        mock_db.execute.return_value = owner_result

        await service.record_spend(ws, Decimal("0.01"))
        await asyncio.sleep(0)

        mock_email.send_embedding_spend_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cap_still_increments_counter_for_visibility(
        self, service, redis_mocks, mock_email
    ):
        ws = _make_workspace(tier_daily=None, tier_monthly=None)
        _, mock_incr, _ = redis_mocks
        mock_incr.return_value = int(Decimal("1.00") * _MICRO_USD)
        await service.record_spend(ws, Decimal("0.50"))
        # Counters still INCR — admin "current spend" dashboards need the data
        # even for uncapped workspaces.
        assert mock_incr.call_count == 2
        # But no alert fires.
        mock_email.send_embedding_spend_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_redis_incr_failure_fails_open(self, service, redis_mocks, mock_email):
        ws = _make_workspace(tier_daily=10.0)
        _, mock_incr, _ = redis_mocks
        mock_incr.side_effect = RedisError("redis down")
        # Must not raise — cap is advisory.
        await service.record_spend(ws, Decimal("0.50"))


class TestRecordSpendFromTokens:
    @pytest.mark.asyncio
    async def test_zero_tokens_is_noop(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=10.0)
        _, mock_incr, _ = redis_mocks
        await service.record_spend_from_tokens(
            ws, provider="openai", model="text-embedding-3-small", tokens=0
        )
        mock_incr.assert_not_called()

    @pytest.mark.asyncio
    async def test_pricing_miss_is_noop(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=10.0)
        _, mock_incr, _ = redis_mocks
        with patch(
            "services.llm_pricing_service.LLMPricingService.compute_cost_usd",
            new_callable=AsyncMock,
        ) as mock_cost:
            mock_cost.return_value = None  # pricing miss
            await service.record_spend_from_tokens(
                ws,
                provider="openai",
                model="text-embedding-3-small",
                tokens=100,
            )
        mock_incr.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_pricing_triggers_record_spend(self, service, redis_mocks):
        ws = _make_workspace(tier_daily=10.0)
        _, mock_incr, _ = redis_mocks
        mock_incr.return_value = int(Decimal("0.0001") * _MICRO_USD)
        with patch(
            "services.llm_pricing_service.LLMPricingService.compute_cost_usd",
            new_callable=AsyncMock,
        ) as mock_cost:
            mock_cost.return_value = 0.0001
            await service.record_spend_from_tokens(
                ws,
                provider="openai",
                model="text-embedding-3-small",
                tokens=100,
            )
        # Daily + monthly counter increments
        assert mock_incr.call_count == 2


class TestCounterKeyFormat:
    """Pin the Redis key shape so a future rename can't silently zero counters."""

    def test_daily_key_uses_utc_date(self):
        ws_id = uuid4()
        with patch("services.embedding_spend_cap_service.utcnow") as mock_now:
            # Make ``utcnow().strftime`` return a deterministic value.
            mock_now.return_value.strftime.side_effect = lambda fmt: (
                "2026-05-18" if fmt == "%Y-%m-%d" else "2026-05"
            )
            daily_key = EmbeddingSpendCapService._counter_key("daily", ws_id)
            monthly_key = EmbeddingSpendCapService._counter_key("monthly", ws_id)
        assert daily_key == f"embed_spend:{ws_id}:daily:2026-05-18"
        assert monthly_key == f"embed_spend:{ws_id}:monthly:2026-05"

    def test_alert_dedup_key_includes_threshold(self):
        ws_id = uuid4()
        with patch("services.embedding_spend_cap_service.utcnow") as mock_now:
            mock_now.return_value.strftime.side_effect = lambda fmt: (
                "2026-05-18" if fmt == "%Y-%m-%d" else "2026-05"
            )
            key80 = EmbeddingSpendCapService._alert_dedup_key("daily", ws_id, 80)
            key100 = EmbeddingSpendCapService._alert_dedup_key("daily", ws_id, 100)
        assert key80 == f"embed_spend_alert:{ws_id}:daily:2026-05-18:80"
        assert key100 == f"embed_spend_alert:{ws_id}:daily:2026-05-18:100"
        assert key80 != key100  # distinct thresholds dedup independently


class TestTTL:
    def test_daily_ttl_covers_clock_skew_past_midnight(self):
        assert _DAILY_TTL_SECONDS == 25 * 3600
