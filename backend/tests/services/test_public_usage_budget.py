"""Pure parts of the public usage budget (#2805): status, retry hint, thresholds, switch."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from pydantic import ValidationError

from config.settings import Settings, get_settings
from services import public_usage_budget as pub
from services.public_usage_budget import (
    PUBLIC_USAGE_BUDGET_PERIOD,
    anonymous_retry_after,
    budget_status,
    public_usage_budget_limit,
    threshold_level,
)

START = datetime(2026, 11, 10, 8, 0, 0)


def _row(used: int, *, start: datetime = START, seq: int = 3) -> SimpleNamespace:
    return SimpleNamespace(calls_used=used, period_started_at=start, period_seq=seq)


def test_constants_are_the_frozen_ones() -> None:
    assert PUBLIC_USAGE_BUDGET_PERIOD == timedelta(days=7)
    assert pub.CHARGE_LOCK_TIMEOUT_MS == 2000
    assert pub.CHARGE_TIMEOUT_SECONDS == 5.0
    assert pub.ANONYMOUS_RETRY_AFTER_CAP_SECONDS == 3600
    assert pub.THRESHOLD_LEVELS == (80, 95, 100)


class TestBudgetStatus:
    def test_disabled_reports_nulls_never_zero(self) -> None:
        status = budget_status(_row(10), limit=None, now=START)
        assert status.state == "disabled"
        assert status.model_dump() == {
            "state": "disabled",
            "limit": None,
            "period_days": 7,
            "used": None,
            "remaining": None,
            "period_started_at": None,
            "resets_at": None,
            "period_seq": None,
            "as_of": START,
        }

    def test_idle_without_a_row(self) -> None:
        status = budget_status(None, limit=25000, now=START)
        assert status.state == "idle"
        assert (status.used, status.remaining, status.limit) == (0, 25000, 25000)
        assert status.period_started_at is None
        assert status.resets_at is None
        assert status.period_seq is None

    def test_idle_exactly_at_the_period_end(self) -> None:
        status = budget_status(_row(25000), limit=25000, now=START + timedelta(days=7))
        assert status.state == "idle"
        assert (status.used, status.remaining) == (0, 25000)
        assert status.period_seq is None

    def test_active_one_microsecond_before_the_end(self) -> None:
        now = START + timedelta(days=7) - timedelta(microseconds=1)
        status = budget_status(_row(1200), limit=25000, now=now)
        assert status.state == "active"
        assert (status.used, status.remaining) == (1200, 23800)
        assert status.period_started_at == START
        assert status.resets_at == START + timedelta(days=7)
        assert status.period_seq == 3
        assert status.as_of == now

    def test_exhausted(self) -> None:
        status = budget_status(_row(25000), limit=25000, now=START + timedelta(hours=1))
        assert status.state == "exhausted"
        assert status.remaining == 0

    def test_limit_lowered_below_used(self) -> None:
        status = budget_status(_row(300), limit=100, now=START + timedelta(hours=1))
        assert status.state == "exhausted"
        assert (status.used, status.remaining) == (300, 0)

    def test_times_serialize_as_utc(self) -> None:
        body = budget_status(_row(1), limit=5, now=START).model_dump(mode="json")
        assert body["period_started_at"] == "2026-11-10T08:00:00Z"
        assert body["resets_at"] == "2026-11-17T08:00:00Z"
        assert body["as_of"] == "2026-11-10T08:00:00Z"


class TestAnonymousRetryAfter:
    END = START + timedelta(days=7)

    @pytest.mark.parametrize(
        ("left", "expected"),
        [
            (timedelta(seconds=1), 60),
            (timedelta(seconds=60), 60),
            (timedelta(seconds=61), 120),
            (timedelta(minutes=59, seconds=1), 3600),
            (timedelta(days=6), 3600),
            (timedelta(0), 60),
            (-timedelta(days=1), 60),
        ],
    )
    def test_rounded_up_to_a_minute_between_60_and_3600(self, left, expected) -> None:
        assert anonymous_retry_after(START, self.END - left) == expected


class TestThresholds:
    def test_default_limit_levels(self) -> None:
        assert threshold_level(20000, 25000) == 80
        assert threshold_level(23750, 25000) == 95
        assert threshold_level(25000, 25000) == 100
        for used in (1, 19999, 20001, 23749, 23751, 24999):
            assert threshold_level(used, 25000) is None

    def test_small_limit_reports_the_highest_coinciding_level_only(self) -> None:
        # limit 5: 80 % -> 4; 95 % -> ceil(4.75) = 5; 100 % -> 5.
        assert threshold_level(4, 5) == 80
        assert threshold_level(5, 5) == 100
        assert [threshold_level(n, 5) for n in range(1, 4)] == [None, None, None]

    def test_limit_one(self) -> None:
        assert threshold_level(1, 1) == 100


class TestSettings:
    @pytest.mark.parametrize("value", [0, -1, 1_000_000_001])
    def test_count_out_of_range_is_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, mcp_public_usage_budget_calls=value)

    def test_defaults_off_and_25000(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.mcp_public_usage_budget_enabled is False
        assert settings.mcp_public_usage_budget_calls == 25000

    def test_switch_off_means_no_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", False)
        monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 7)
        assert public_usage_budget_limit() is None

    def test_switch_on_reports_the_count(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", True)
        monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 7)
        assert public_usage_budget_limit() == 7


class TestLogEvents:
    """R7: aggregates only, the exception message never in a line."""

    WS = UUID("00000000-0000-4000-8000-000000002805")

    @staticmethod
    def _admitted(used: int, seq: int = 2) -> pub.BudgetCharge:
        return pub.BudgetCharge(
            admitted=True,
            calls_used=used,
            period_started_at=START,
            period_seq=seq,
            retry_after_seconds=None,
        )

    async def _charge(self, outcome) -> MagicMock:
        charge = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else None
        charge = charge or AsyncMock(return_value=outcome)
        with patch.object(pub, "_charge", charge), patch.object(pub, "logger") as log:
            # A failed charge raises; these tests only read what it logged.
            with contextlib.suppress(pub.PublicUsageBudgetUnavailable):
                await pub.charge_public_usage(self.WS, limit=25000)
        return log

    async def test_first_call_of_a_period_logs_period_started(self) -> None:
        log = await self._charge(self._admitted(1, seq=3))
        log.info.assert_called_once_with(
            "public_usage_budget_period_started",
            workspace_id=str(self.WS),
            period_started_at=START.isoformat(),
            period_seq=3,
            limit=25000,
        )
        log.warning.assert_not_called()

    async def test_threshold_logs_its_level(self) -> None:
        log = await self._charge(self._admitted(20000))
        log.warning.assert_called_once_with(
            "public_usage_budget_threshold",
            workspace_id=str(self.WS),
            level=80,
            calls_used=20000,
            limit=25000,
            period_seq=2,
        )
        log.info.assert_not_called()

    async def test_an_ordinary_call_logs_nothing(self) -> None:
        log = await self._charge(self._admitted(7))
        assert log.method_calls == []

    async def test_refusal_logs_nothing(self) -> None:
        refused = pub.BudgetCharge(
            admitted=False,
            calls_used=25000,
            period_started_at=START,
            period_seq=2,
            retry_after_seconds=3600,
        )
        log = await self._charge(refused)
        assert log.method_calls == []

    async def test_unavailable_never_carries_the_exception_message(self) -> None:
        log = await self._charge(RuntimeError("secret-sentinel"))
        assert log.info.call_count == 0
        log.warning.assert_called_once()
        args, kwargs = log.warning.call_args
        assert args == ("public_usage_budget_unavailable",)
        assert kwargs == {"workspace_id": str(self.WS), "error_type": "RuntimeError"}
        assert "secret-sentinel" not in repr(log.mock_calls)
