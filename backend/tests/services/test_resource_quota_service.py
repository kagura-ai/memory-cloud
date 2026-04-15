"""Unit tests for services/resource_quota_service.

The helper is shared between HTTP and MCP ingest paths and must key the Redis
counter on ``workspace_id`` (not token_id) so combined traffic is enforced
against one ceiling.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.resource_quota_service import (
    MCP_INGEST_DEFAULT_QUOTA_PER_HOUR,
    check_event_quota,
    resolve_workspace_event_quota_per_hour,
)
from utils.exceptions import RateLimitError, RedisError

WORKSPACE_ID = uuid4()
EXPECTED_KEY = f"resource:events:ec_products:{WORKSPACE_ID}:hour"


@pytest.fixture
def redis_mocks():
    """Yield (mock_get_cache, mock_incrby_counter) patched at the service module."""
    with (
        patch("services.resource_quota_service.get_cache", new_callable=AsyncMock) as mock_get,
        patch(
            "services.resource_quota_service.incrby_counter", new_callable=AsyncMock
        ) as mock_incr,
    ):
        yield mock_get, mock_incr


class TestCheckEventQuota:
    """Behaviour of the workspace-scoped quota helper."""

    @pytest.mark.asyncio
    async def test_within_limit_reserves_count(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "50"
        mock_incr.return_value = 51

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000)

        mock_get.assert_awaited_once_with(EXPECTED_KEY)
        mock_incr.assert_awaited_once_with(EXPECTED_KEY, 1, ttl=3600)

    @pytest.mark.asyncio
    async def test_exceeded_raises_and_does_not_increment(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "1001"

        with pytest.raises(RateLimitError) as exc_info:
            await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000)

        assert "Event quota exceeded" in exc_info.value.message
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after == 3600
        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batch_count_must_fit_or_reject(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "990"  # 990 + 20 > 1000

        with pytest.raises(RateLimitError):
            await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000, count=20)

        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batch_reserves_full_amount(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "100"
        mock_incr.return_value = 150

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000, count=50)

        mock_incr.assert_awaited_once_with(EXPECTED_KEY, 50, ttl=3600)

    @pytest.mark.asyncio
    async def test_fresh_key_treated_as_zero(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = None
        mock_incr.return_value = 1

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000)

        mock_incr.assert_awaited_once_with(EXPECTED_KEY, 1, ttl=3600)

    @pytest.mark.asyncio
    async def test_get_cache_redis_error_fails_open(self, redis_mocks):
        # NOTE: db.redis.get_cache itself catches and swallows backend errors,
        # returning None — so the RedisError branch in check_event_quota
        # primarily covers incrby_counter failures. This test verifies the
        # contract holds even if get_cache leaks a RedisError.
        mock_get, mock_incr = redis_mocks
        mock_get.side_effect = RedisError("redis read failed")

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000)

        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_incr_redis_error_fails_open_after_passing_check(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "50"
        mock_incr.side_effect = RedisError("redis write failed")

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000)

    @pytest.mark.asyncio
    async def test_non_redis_error_surfaces(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "50"
        mock_incr.side_effect = ValueError("programming bug, not a Redis error")

        with pytest.raises(ValueError):
            await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000)

    @pytest.mark.asyncio
    async def test_zero_quota_disables_check(self, redis_mocks):
        mock_get, mock_incr = redis_mocks

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=0)

        mock_get.assert_not_awaited()
        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_quota_disables_check(self, redis_mocks):
        mock_get, mock_incr = redis_mocks

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=-1)

        mock_get.assert_not_awaited()
        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_count_raises(self, redis_mocks):
        """Defensive guard: a negative count would DECR the Redis counter and
        silently relax the quota. All internal callers pass len(events) >= 1,
        so negative count is never a valid input."""
        mock_get, mock_incr = redis_mocks

        with pytest.raises(ValueError, match="count must be >= 1"):
            await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000, count=-5)

        mock_get.assert_not_awaited()
        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_count_raises(self, redis_mocks):
        mock_get, mock_incr = redis_mocks

        with pytest.raises(ValueError, match="count must be >= 1"):
            await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000, count=0)

        mock_get.assert_not_awaited()
        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_boundary_exact_limit_passes(self, redis_mocks):
        """current + count == quota_per_hour is allowed (strict > test)."""
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "999"
        mock_incr.return_value = 1000

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000, count=1)

        mock_incr.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_boundary_one_over_rejects(self, redis_mocks):
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = "1000"

        with pytest.raises(RateLimitError):
            await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=1000, count=1)

        mock_incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_key_format_uses_workspace_id_not_token(self, redis_mocks):
        """Regression: the key MUST be workspace-scoped so HTTP and MCP share
        the same counter."""
        mock_get, mock_incr = redis_mocks
        mock_get.return_value = None
        mock_incr.return_value = 1

        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=10)

        called_key = mock_get.await_args.args[0]
        assert called_key == f"resource:events:ec_products:{WORKSPACE_ID}:hour"

    @pytest.mark.asyncio
    async def test_combined_http_mcp_share_counter(self, redis_mocks):
        """Two callers (one HTTP, one MCP) targeting the same workspace+resource
        hit the same Redis key and increment one counter."""
        mock_get, mock_incr = redis_mocks
        # Simulated counter progression: 0 -> 5 (HTTP) -> 8 (MCP)
        mock_get.side_effect = [None, "5"]
        mock_incr.side_effect = [5, 8]

        # HTTP-style call (5 events)
        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=10, count=5)
        # MCP-style call (3 events) — same key
        await check_event_quota("ec_products", WORKSPACE_ID, quota_per_hour=10, count=3)

        assert mock_incr.await_args_list[0].args[0] == EXPECTED_KEY
        assert mock_incr.await_args_list[1].args[0] == EXPECTED_KEY


class TestResolveWorkspaceEventQuotaPerHour:
    """Resolution of the per-hour ceiling for MCP ingest calls."""

    @pytest.mark.asyncio
    async def test_returns_max_token_quota_for_resource(self):
        mock_db = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = 5000
        mock_db.execute = AsyncMock(return_value=result_mock)

        quota = await resolve_workspace_event_quota_per_hour(mock_db, WORKSPACE_ID, "ec_products")

        assert quota == 5000

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_no_token_for_resource(self):
        mock_db = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=result_mock)

        quota = await resolve_workspace_event_quota_per_hour(mock_db, WORKSPACE_ID, "ec_products")

        assert quota == MCP_INGEST_DEFAULT_QUOTA_PER_HOUR

    @pytest.mark.asyncio
    async def test_query_filters_on_resource_id(self):
        """Regression for Copilot review: workspace-wide MAX would over-allow
        ingest into a low-quota resource if a higher-quota token exists for a
        sibling resource. The query MUST filter on resource_id."""
        from models.resource import ResourceToken

        mock_db = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = 100
        mock_db.execute = AsyncMock(return_value=result_mock)

        await resolve_workspace_event_quota_per_hour(mock_db, WORKSPACE_ID, "ec_products")

        # Inspect the compiled WHERE clause includes a resource_id predicate.
        called_stmt = mock_db.execute.await_args.args[0]
        compiled = str(called_stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "resource_id" in compiled
        assert "workspace_id" in compiled
        assert "is_active" in compiled
        # ResourceToken is referenced for the import side-effect; static
        # analyzers should not flag it as unused.
        assert ResourceToken is not None
