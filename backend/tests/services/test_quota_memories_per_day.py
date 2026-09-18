"""Tests for the daily memory-creation quota (#1549, epic #1547).

``QuotaService.check_memories_per_day`` reserves-then-checks on a Redis
counter keyed by workspace + UTC day: INCRBY first, and if the new total
overshoots the effective limit, INCRBY back by the same amount and refuse.
Redis is patched where ``quota_service`` imports it (``incrby_counter`` /
``get_cache``), so no live Redis is needed.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.quota_service import QuotaService
from utils.exceptions import QuotaExceededError, RedisError

# Frozen clock: the counter key carries the UTC date and the refusal carries
# the next UTC midnight, so pin both.
NOW = datetime(2026, 9, 18, 12, 0, 0)
TODAY = "2026-09-18"
RESETS_AT = "2026-09-19T00:00:00Z"


def _workspace(limit: int, plan_name: str = "free") -> MagicMock:
    ws = MagicMock()
    ws.id = uuid4()
    ws.plan_name = plan_name
    ws.effective_memories_per_day = limit
    return ws


def _db_returning(workspace) -> MagicMock:
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = workspace
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture
def incrby():
    with patch("services.quota_service.incrby_counter", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch("services.quota_service.utcnow", return_value=NOW):
        yield


def _key(workspace_id) -> str:
    return f"quota:workspace:{workspace_id}:memories:{TODAY}"


class TestCheckMemoriesPerDay:
    async def test_under_limit_is_allowed_and_reserves_once(self, incrby):
        ws = _workspace(limit=50)
        incrby.return_value = 5
        service = QuotaService(_db_returning(ws))

        allowed, error = await service.check_memories_per_day(ws.id)

        assert (allowed, error) == (True, None)
        incrby.assert_awaited_once_with(_key(ws.id), 1, ttl=86400)

    async def test_exactly_at_limit_is_allowed(self, incrby):
        """The N-th memory of the day fits (limit is inclusive)."""
        ws = _workspace(limit=50)
        incrby.return_value = 50
        service = QuotaService(_db_returning(ws))

        allowed, _ = await service.check_memories_per_day(ws.id)

        assert allowed is True
        assert incrby.await_count == 1

    async def test_over_limit_is_refused_and_reservation_released(self, incrby):
        """The (N+1)-th is refused; the counter is decremented back so the
        refused attempt does not consume budget."""
        ws = _workspace(limit=50, plan_name="free")
        incrby.return_value = 51
        service = QuotaService(_db_returning(ws))

        allowed, error = await service.check_memories_per_day(ws.id)

        assert allowed is False
        assert error is not None
        assert "50" in error  # limit and today's usage
        assert RESETS_AT in error
        assert incrby.await_count == 2
        release = incrby.await_args_list[1]
        assert release.args == (_key(ws.id), -1)

    async def test_over_limit_raises_with_structured_details(self, incrby):
        ws = _workspace(limit=50, plan_name="free")
        incrby.return_value = 51
        service = QuotaService(_db_returning(ws))

        with pytest.raises(QuotaExceededError) as excinfo:
            await service.check_memories_per_day(ws.id, raise_on_exceeded=True)

        exc = excinfo.value
        assert exc.status_code == 429
        assert exc.error_code == "QUOTA-001"
        assert exc.details["quota_type"] == "memories_per_day"
        assert exc.details["limit"] == 50
        assert exc.details["used_today"] == 50
        assert exc.details["requested"] == 1
        assert exc.details["resets_at"] == RESETS_AT
        # The reservation was still released before raising.
        assert incrby.await_args_list[1].args == (_key(ws.id), -1)

    async def test_batch_that_does_not_fit_is_refused_whole(self, incrby):
        """A batch is all-or-nothing: 45 used + 10 requested > 50 → refuse all
        10 and release all 10 (no partial batch)."""
        ws = _workspace(limit=50)
        incrby.return_value = 55
        service = QuotaService(_db_returning(ws))

        allowed, error = await service.check_memories_per_day(ws.id, count=10)

        assert allowed is False
        assert error is not None
        assert "45" in error  # today's usage excludes the released reservation
        assert incrby.await_args_list[0].args == (_key(ws.id), 10)
        assert incrby.await_args_list[1].args == (_key(ws.id), -10)

    async def test_batch_that_fits_is_charged_once(self, incrby):
        ws = _workspace(limit=300)
        incrby.return_value = 100
        service = QuotaService(_db_returning(ws))

        allowed, _ = await service.check_memories_per_day(ws.id, count=100)

        assert allowed is True
        incrby.assert_awaited_once_with(_key(ws.id), 100, ttl=86400)

    async def test_redis_error_fails_open_with_warning(self, incrby):
        ws = _workspace(limit=50)
        incrby.side_effect = RedisError("redis down")
        service = QuotaService(_db_returning(ws))

        with patch("services.quota_service.logger") as mock_logger:
            allowed, error = await service.check_memories_per_day(ws.id)

        assert (allowed, error) == (True, None)
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.args[0] == "memories_per_day_redis_failed"

    async def test_release_failure_is_logged_not_raised(self, incrby):
        """If the compensating decrement fails the refusal still stands; the
        phantom reservation expires with the day key."""
        ws = _workspace(limit=50)
        incrby.side_effect = [51, RedisError("redis down")]
        service = QuotaService(_db_returning(ws))

        with patch("services.quota_service.logger") as mock_logger:
            allowed, _ = await service.check_memories_per_day(ws.id)

        assert allowed is False
        assert mock_logger.warning.call_args_list[0].args[0] == "memories_per_day_release_failed"

    async def test_zero_limit_is_refused_without_touching_redis(self, incrby):
        """Zero-floor (#569): ``memories_per_day == 0`` means the tier cannot
        create memories at all — not "unlimited"."""
        ws = _workspace(limit=0, plan_name="free")
        service = QuotaService(_db_returning(ws))

        allowed, error = await service.check_memories_per_day(ws.id)

        assert allowed is False
        assert error is not None
        incrby.assert_not_awaited()

        with pytest.raises(QuotaExceededError) as excinfo:
            await service.check_memories_per_day(ws.id, raise_on_exceeded=True)
        assert excinfo.value.details["quota_type"] == "memories_per_day"
        assert excinfo.value.details["limit"] == 0
        incrby.assert_not_awaited()

    async def test_non_positive_count_is_a_noop(self, incrby):
        ws = _workspace(limit=50)
        service = QuotaService(_db_returning(ws))

        assert await service.check_memories_per_day(ws.id, count=0) == (True, None)
        incrby.assert_not_awaited()

    async def test_workspace_not_found(self, incrby):
        service = QuotaService(_db_returning(None))
        workspace_id = uuid4()

        allowed, error = await service.check_memories_per_day(workspace_id)

        assert allowed is False
        assert str(workspace_id) in (error or "")
        incrby.assert_not_awaited()
        with pytest.raises(QuotaExceededError):
            await service.check_memories_per_day(workspace_id, raise_on_exceeded=True)


class TestCountMemoriesCreatedToday:
    @pytest.mark.parametrize(
        ("cached", "expected"),
        [("7", 7), (None, 0), ("", 0), ("garbage", 0)],
    )
    async def test_reads_counter_or_zero(self, cached, expected):
        workspace_id = uuid4()
        service = QuotaService(MagicMock())

        with patch(
            "services.quota_service.get_cache", new_callable=AsyncMock, return_value=cached
        ) as get_cache:
            assert await service.count_memories_created_today(workspace_id) == expected

        get_cache.assert_awaited_once_with(_key(workspace_id))


class TestQuotaStatusMemoriesToday:
    async def test_status_block_shape(self):
        ws = _workspace(limit=50, plan_name="free")
        ws.effective_memory_limit = 1000
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none.return_value = ws
        members_result = MagicMock()
        members_result.all.return_value = []  # no members → memory_count 0
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[workspace_result, members_result])
        service = QuotaService(db)

        with patch("services.quota_service.get_cache", new_callable=AsyncMock, return_value="40"):
            status = await service.get_quota_status(ws.id)

        assert status["memories_today"] == {
            "current": 40,
            "limit": 50,
            "percentage": 80.0,
            "warning": True,
            "exceeded": False,
            "resets_at": RESETS_AT,
        }
        # The pre-existing block is untouched.
        assert status["memory"]["current"] == 0
