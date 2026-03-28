"""Tests for Rate Limit Middleware.

Issue #251: Rate limiting for REST API endpoints.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from api.middleware.rate_limit import RateLimitMiddleware
from config.plan_tiers import PlanName
from utils.exceptions import QuotaExceededError


class TestRateLimitMiddleware:
    """Test rate limit middleware functionality."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.fixture
    def mock_request(self):
        """Create mock request."""
        request = MagicMock(spec=Request)
        request.url.path = "/api/v1/memory/remember"
        request.state.user_id = "test_user_123"
        request.state.user = {"user_id": "test_user_123", "role": "user"}
        return request

    @pytest.fixture
    def mock_call_next(self):
        """Create mock call_next."""

        async def _call_next(request):
            return Response(content=b"OK", media_type="text/plain")

        return _call_next

    @pytest.mark.asyncio
    async def test_excluded_paths_skip_rate_limiting(
        self, middleware, mock_request, mock_call_next
    ):
        """Test that excluded paths skip rate limiting."""
        mock_request.url.path = "/health"

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Should pass through without rate limiting
        assert response is not None
        assert "X-RateLimit-Limit" not in response.headers

    @pytest.mark.asyncio
    async def test_unauthenticated_users_skip_rate_limiting(
        self, middleware, mock_request, mock_call_next
    ):
        """Test that unauthenticated users skip rate limiting."""
        mock_request.state.user_id = None

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Should pass through
        assert response is not None
        assert "X-RateLimit-Limit" not in response.headers

    @pytest.mark.asyncio
    async def test_admin_users_bypass_rate_limiting(self, middleware, mock_request, mock_call_next):
        """Test that system admins bypass rate limiting."""
        mock_request.state.user = {"user_id": "admin_user", "role": "admin"}
        mock_request.state.user_id = "admin_user"

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Admin should bypass
        assert response is not None
        assert "X-RateLimit-Limit" not in response.headers

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch.object(RateLimitMiddleware, "_get_user_plan")
    @patch.object(RateLimitMiddleware, "_check_daily_quota")
    async def test_tier_based_rate_limit_free_plan(
        self,
        mock_check_daily,
        mock_get_plan,
        mock_increment,
        middleware,
        mock_request,
        mock_call_next,
    ):
        """Test tier-based rate limit for Free plan (100/min)."""
        mock_get_plan.return_value = "free"
        mock_increment.return_value = 50  # Within limit
        mock_check_daily.return_value = None

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Should add rate limit headers
        assert response.headers["X-RateLimit-Limit"] == "100"
        assert response.headers["X-RateLimit-Remaining"] == "50"

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch.object(RateLimitMiddleware, "_get_user_plan")
    async def test_rate_limit_exceeded_returns_429(
        self,
        mock_get_plan,
        mock_increment,
        middleware,
        mock_request,
        mock_call_next,
    ):
        """Test that exceeding rate limit returns 429."""
        mock_get_plan.return_value = "free"
        mock_increment.return_value = 101  # Exceeded (Free: 100/min)

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Should return 429
        assert isinstance(response, JSONResponse)
        assert response.status_code == 429
        assert "X-RateLimit-Limit" in response.headers
        assert response.headers["X-RateLimit-Remaining"] == "0"

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch.object(RateLimitMiddleware, "_get_user_plan")
    @patch.object(RateLimitMiddleware, "_check_daily_quota")
    async def test_endpoint_override_auth(
        self,
        mock_check_daily,
        mock_get_plan,
        mock_increment,
        middleware,
        mock_request,
        mock_call_next,
    ):
        """Test endpoint override for auth endpoints (10/min)."""
        mock_request.url.path = "/api/v1/auth/me"
        mock_get_plan.return_value = "pro"  # Pro plan: 1000/min normally
        mock_increment.return_value = 5  # Within auth limit (10/min)
        mock_check_daily.return_value = None

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Auth override should apply (10/min, not 1000/min)
        assert response.headers["X-RateLimit-Limit"] == "10"

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch.object(RateLimitMiddleware, "_get_user_plan")
    @patch.object(RateLimitMiddleware, "_check_daily_quota")
    async def test_endpoint_override_contexts(
        self,
        mock_check_daily,
        mock_get_plan,
        mock_increment,
        middleware,
        mock_request,
        mock_call_next,
    ):
        """Test endpoint override for context endpoints (50/min)."""
        mock_request.url.path = "/api/v1/contexts"
        mock_get_plan.return_value = "free"  # Free plan: 100/min normally
        mock_increment.return_value = 30  # Within context limit (50/min)
        mock_check_daily.return_value = None

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Context override should apply (50/min, not 100/min)
        assert response.headers["X-RateLimit-Limit"] == "50"

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch.object(RateLimitMiddleware, "_get_user_plan")
    async def test_redis_failure_fails_open(
        self,
        mock_get_plan,
        mock_increment,
        middleware,
        mock_request,
        mock_call_next,
    ):
        """Test that Redis failures don't block requests (fail-open)."""
        from utils.exceptions import RedisError

        mock_get_plan.return_value = "free"
        mock_increment.side_effect = RedisError("Redis connection failed")

        response = await middleware.dispatch(mock_request, mock_call_next)

        # Should allow request despite Redis failure
        assert response is not None
        # Headers should not be added (Redis failure)
        assert "X-RateLimit-Limit" not in response.headers

    @pytest.mark.asyncio
    @patch.object(RateLimitMiddleware, "_get_user_plan")
    async def test_get_user_plan_with_workspace(self, mock_db):
        """Test _get_user_plan retrieves workspace plan."""
        middleware = RateLimitMiddleware(MagicMock())

        # Mock database query results
        with patch("api.middleware.rate_limit.get_db") as mock_get_db_func:
            mock_db_session = AsyncMock()

            # Mock workspace_id query
            workspace_id_result = MagicMock()
            workspace_id_result.scalar_one_or_none.return_value = "workspace-uuid-123"

            # Mock plan_name query
            plan_result = MagicMock()
            plan_result.scalar_one_or_none.return_value = "pro"

            mock_db_session.execute.side_effect = [workspace_id_result, plan_result]

            async def mock_db_gen():
                yield mock_db_session

            mock_get_db_func.return_value = mock_db_gen()

            plan = await middleware._get_user_plan("test_user")

            assert plan == "pro"
            assert mock_db_session.close.called

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch("api.middleware.rate_limit.get_plan_tier")
    async def test_check_daily_quota_mcp_endpoint(
        self,
        mock_get_tier,
        mock_increment,
    ):
        """Test daily quota check for MCP endpoints."""
        from config.plan_tiers import PLAN_FREE

        middleware = RateLimitMiddleware(MagicMock())
        mock_get_tier.return_value = PLAN_FREE
        mock_increment.return_value = 500  # Within MCP limit (1000/day)

        # Should not raise
        await middleware._check_daily_quota("user123", "/api/v1/memory/remember", PlanName.FREE)

        # Check that MCP key was used
        call_args = mock_increment.call_args
        assert "mcp" in call_args[0][0]

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch("api.middleware.rate_limit.get_plan_tier")
    async def test_check_daily_quota_rest_free_plan_blocked(
        self,
        mock_get_tier,
        mock_increment,
    ):
        """Test that Free plan users cannot access REST API (rest_calls_per_day=0)."""
        from config.plan_tiers import PLAN_FREE

        middleware = RateLimitMiddleware(MagicMock())
        mock_get_tier.return_value = PLAN_FREE

        # Free plan: REST API disabled
        with pytest.raises(QuotaExceededError, match="REST API is not available on Free plan"):
            await middleware._check_daily_quota("user123", "/api/v1/users/me", PlanName.FREE)

    @pytest.mark.asyncio
    @patch("api.middleware.rate_limit.increment_counter")
    @patch("api.middleware.rate_limit.get_plan_tier")
    async def test_check_daily_quota_exceeded(
        self,
        mock_get_tier,
        mock_increment,
    ):
        """Test daily quota exceeded raises error."""
        from config.plan_tiers import PLAN_FREE

        middleware = RateLimitMiddleware(MagicMock())
        mock_get_tier.return_value = PLAN_FREE
        mock_increment.return_value = 1001  # Exceeded MCP limit (1000/day)

        with pytest.raises(QuotaExceededError, match="Daily MCP quota exceeded"):
            await middleware._check_daily_quota("user123", "/api/v1/memory/remember", PlanName.FREE)
