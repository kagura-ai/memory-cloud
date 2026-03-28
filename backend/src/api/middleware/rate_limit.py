"""Rate Limiting Middleware for API request throttling.

Issue #251: Rate limiting for REST API endpoints.

Implements two-layer quota enforcement:
1. Per-minute rate limiting (burst protection)
2. Daily quota enforcement (MCP vs REST separation)

Features:
- Tier-based limits (Free: 100/min, Basic: 300/min, Pro: 1000/min)
- Per-endpoint overrides (Auth: 10/min, Context: 50/min)
- Admin bypass (System admins skip rate limiting)
- Fail-open resilience (Redis failures don't block requests)
- X-RateLimit-* headers
"""

import time
from collections.abc import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from config.plan_tiers import PlanName, get_plan_tier
from config.rate_limits import (
    RATE_LIMIT_EXCLUDED_PATHS,
    get_rate_limit_for_endpoint,
)
from db.base import get_db
from db.redis import increment_counter
from models.auth import User, Workspace
from utils.datetime import utcnow
from utils.exceptions import QuotaExceededError, RedisError
from utils.logger import get_logger

logger = get_logger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce rate limits based on user plan tier.

    Middleware that:
    1. Checks per-minute rate limit (burst protection)
    2. Checks daily quota (MCP vs REST separation)
    3. Adds X-RateLimit-* headers to responses
    4. Returns 429 with Retry-After header on limit exceeded

    Args:
        app: FastAPI application

    Example:
        >>> app.add_middleware(RateLimitMiddleware)
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with rate limiting.

        Args:
            request: FastAPI request object
            call_next: Next middleware/handler in chain

        Returns:
            Response with X-RateLimit-* headers or 429 if exceeded
        """
        path = request.url.path

        # 1. Skip excluded paths (health checks, docs, static files)
        if path in RATE_LIMIT_EXCLUDED_PATHS or path.startswith("/static"):
            return await call_next(request)

        # 2. Get user from request.state (set by SessionMiddleware)
        user = getattr(request.state, "user", None)
        user_id = getattr(request.state, "user_id", None)

        # 3. Skip for unauthenticated users (handled by auth dependencies)
        if not user_id:
            return await call_next(request)

        # 4. Admin bypass (system admins have unlimited access)
        if user and user.get("role") == "admin":
            logger.debug("rate_limit_admin_bypass", user_id=user_id, path=path)
            return await call_next(request)

        try:
            # 5. Get user's plan tier
            plan_name = await self._get_user_plan(user_id)
            plan = PlanName(plan_name)

            # 6. Get rate limit for this endpoint (tier-based or override)
            per_minute_limit = get_rate_limit_for_endpoint(path, plan)

            # 7. Check per-minute rate limit (burst protection)
            current_minute = utcnow().strftime("%Y-%m-%d-%H-%M")
            minute_key = f"rate_limit:user:{user_id}:minute:{current_minute}"

            try:
                minute_count = await increment_counter(minute_key, ttl=60)
            except RedisError as e:
                logger.error("rate_limit_redis_failed", error=str(e), user_id=user_id)
                # Fail-open: Allow request if Redis is down
                return await call_next(request)

            remaining = max(0, per_minute_limit - minute_count)
            reset_time = int(time.time()) + 60

            # 8. If per-minute limit exceeded, return 429
            if minute_count > per_minute_limit:
                logger.warning(
                    "rate_limit_exceeded",
                    user_id=user_id,
                    path=path,
                    count=minute_count,
                    limit=per_minute_limit,
                    plan=plan_name,
                )

                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "RATE-001",
                        "message": f"Rate limit exceeded: {minute_count}/{per_minute_limit} requests per minute",
                        "details": {
                            "retry_after": 60,
                            "limit": per_minute_limit,
                            "remaining": 0,
                            "reset": reset_time,
                        },
                    },
                    headers={
                        "X-RateLimit-Limit": str(per_minute_limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset_time),
                        "Retry-After": "60",
                    },
                )

            # 9. Check daily quota (MCP vs REST separation)
            try:
                await self._check_daily_quota(user_id, path, plan)
            except QuotaExceededError as e:
                logger.warning(
                    "daily_quota_exceeded",
                    user_id=user_id,
                    path=path,
                    plan=plan_name,
                    message=str(e),
                )

                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "QUOTA-001",
                        "message": str(e),
                        "details": {"retry_after": 86400},  # Reset in 24 hours
                    },
                    headers={
                        "Retry-After": "86400",
                    },
                )

            # 10. Process request
            response = await call_next(request)

            # 11. Add rate limit headers to successful response
            response.headers["X-RateLimit-Limit"] = str(per_minute_limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = str(reset_time)

            return response

        except Exception as e:
            logger.error(
                "rate_limit_middleware_unexpected_error",
                error=str(e),
                user_id=user_id,
                path=path,
            )
            # Fail-open: Allow request on unexpected errors
            return await call_next(request)

    async def _get_user_plan(self, user_id: str) -> str:
        """Get user's plan from workspace.

        Args:
            user_id: User ID

        Returns:
            Plan name ('free', 'basic', 'pro')

        Note:
            Creates new database session to avoid conflicts with request handler.
        """
        db_gen = get_db()
        db = await anext(db_gen)

        try:
            # Get user's current workspace ID
            result = await db.execute(
                select(User.current_workspace_id).where(User.user_id == user_id)
            )
            workspace_id = result.scalar_one_or_none()

            if not workspace_id:
                # No workspace → default to free plan
                return "free"

            # Get workspace's plan
            result = await db.execute(
                select(Workspace.plan_name).where(Workspace.id == workspace_id)
            )
            plan_name = result.scalar_one_or_none()

            return plan_name or "free"

        finally:
            await db.close()

    async def _check_daily_quota(self, user_id: str, path: str, plan: PlanName):
        """Check daily quota (MCP vs Public vs REST API separation).

        Issue #238: Separated quotas for MCP and REST APIs.
        Issue #242: Added Public API quota separation.

        Args:
            user_id: User ID
            path: Request path
            plan: User's plan tier

        Raises:
            QuotaExceededError: If daily quota exceeded

        Note:
            - MCP endpoints: /api/v1/memory/*, /mcp/*
            - Public endpoints: /api/v1/public/* (Issue #242)
            - REST endpoints: Everything else
            - Free plan: rest_calls_per_day=0, public_calls_per_day=0 (disabled)
        """
        today = utcnow().date().isoformat()
        plan_tier = get_plan_tier(plan)

        # Determine quota type (priority order: MCP > Public > REST)
        is_mcp = path.startswith("/api/v1/memory/") or path.startswith("/mcp/")
        is_public = path.startswith("/api/v1/public/") or path.startswith("/api/v1/resources/")

        if is_mcp:
            # MCP API quota
            daily_key = f"quota:user:{user_id}:mcp:{today}"
            daily_limit = plan_tier.mcp_calls_per_day
            quota_type = "MCP"

        elif is_public:
            # Public API quota (Issue #242)
            daily_key = f"quota:user:{user_id}:public:{today}"
            daily_limit = plan_tier.public_calls_per_day
            quota_type = "Public API"

            # Free plan: Public API disabled (public_calls_per_day=0)
            if daily_limit == 0:
                raise QuotaExceededError(
                    "Public API is not available on Free plan. Please upgrade to Basic or Pro plan."
                )

        else:
            # REST API quota
            daily_key = f"quota:user:{user_id}:rest:{today}"
            daily_limit = plan_tier.rest_calls_per_day
            quota_type = "REST"

            # Free plan: REST API disabled (rest_calls_per_day=0)
            if daily_limit == 0:
                raise QuotaExceededError(
                    "REST API is not available on Free plan. Please upgrade to Basic or Pro plan."
                )

        try:
            daily_count = await increment_counter(daily_key, ttl=86400)
        except RedisError as e:
            logger.error("daily_quota_redis_failed", error=str(e), user_id=user_id)
            # Fail-open: Allow request if Redis is down
            return

        if daily_count > daily_limit:
            raise QuotaExceededError(
                f"Daily {quota_type} quota exceeded: {daily_count}/{daily_limit}. "
                f"Resets at midnight UTC."
            )

        logger.debug(
            "daily_quota_checked",
            user_id=user_id,
            quota_type=quota_type,
            count=daily_count,
            limit=daily_limit,
        )
