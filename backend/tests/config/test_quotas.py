"""Tests for quota configuration."""

import pytest

from config.quotas import (
    ADMIN_QUOTA,
    DEFAULT_PLAN,
    UserPlan,
    check_memory_quota,
    get_memory_quota,
    get_rate_limit,
)


class TestUserPlan:
    """Test UserPlan enum."""

    def test_plan_values(self):
        assert UserPlan.FREE == "free"
        assert UserPlan.PRO == "pro"
        assert UserPlan.ENTERPRISE == "enterprise"

    def test_plan_from_string(self):
        assert UserPlan("free") == UserPlan.FREE


class TestMemoryQuotas:
    """Test memory quota configuration."""

    def test_free_plan_quotas(self):
        quota = get_memory_quota(UserPlan.FREE)
        assert quota["total_points"] == 10_000

    def test_pro_plan_quotas(self):
        quota = get_memory_quota(UserPlan.PRO)
        assert quota["total_points"] == 100_000

    def test_enterprise_plan_quotas(self):
        quota = get_memory_quota(UserPlan.ENTERPRISE)
        assert quota["total_points"] == 1_000_000

    def test_string_plan_lookup(self):
        quota = get_memory_quota("pro")
        assert quota["total_points"] == 100_000

    def test_quota_has_required_keys(self):
        for plan in UserPlan:
            quota = get_memory_quota(plan)
            assert "total_points" in quota
            assert "working_points" in quota
            assert "persistent_points" in quota

    def test_working_less_than_total(self):
        for plan in UserPlan:
            quota = get_memory_quota(plan)
            assert quota["working_points"] < quota["total_points"]


class TestRateLimits:
    """Test rate limit configuration."""

    def test_free_plan_limits(self):
        limit = get_rate_limit(UserPlan.FREE)
        assert limit["requests_per_minute"] == 100

    def test_pro_plan_limits(self):
        limit = get_rate_limit(UserPlan.PRO)
        assert limit["requests_per_minute"] == 1_000

    def test_string_plan_lookup(self):
        limit = get_rate_limit("enterprise")
        assert limit["requests_per_minute"] == 10_000

    def test_limits_increase_with_plan(self):
        free = get_rate_limit(UserPlan.FREE)
        pro = get_rate_limit(UserPlan.PRO)
        ent = get_rate_limit(UserPlan.ENTERPRISE)
        assert free["requests_per_minute"] < pro["requests_per_minute"]
        assert pro["requests_per_minute"] < ent["requests_per_minute"]


class TestCheckMemoryQuota:
    """Test memory quota check function."""

    @pytest.mark.asyncio
    async def test_under_quota(self):
        result = await check_memory_quota("user1", UserPlan.PRO, 50_000)
        assert result is True

    @pytest.mark.asyncio
    async def test_at_quota(self):
        result = await check_memory_quota("user1", UserPlan.FREE, 10_000)
        assert result is False  # Not strictly less than

    @pytest.mark.asyncio
    async def test_over_quota(self):
        result = await check_memory_quota("user1", UserPlan.FREE, 20_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_working_scope(self):
        result = await check_memory_quota("user1", UserPlan.FREE, 1_000, scope="working")
        assert result is True

    @pytest.mark.asyncio
    async def test_persistent_scope(self):
        result = await check_memory_quota("user1", UserPlan.FREE, 7_000, scope="persistent")
        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_scope(self):
        with pytest.raises(ValueError, match="Invalid scope"):
            await check_memory_quota("user1", UserPlan.FREE, 0, scope="invalid")


class TestDefaults:
    """Test default configuration."""

    def test_default_plan(self):
        assert DEFAULT_PLAN == UserPlan.FREE

    def test_admin_quota_unlimited(self):
        assert ADMIN_QUOTA["total_points"] == float("inf")
