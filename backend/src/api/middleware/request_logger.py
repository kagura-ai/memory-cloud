"""Request Logging Middleware for API usage tracking.

Logs all API requests to usage_stats table for quota management and billing.
Issue #48 - Usage Statistics - Plan Limits & Usage Tracking
"""

import time
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from db.base import get_db
from utils.logger import get_logger
from utils.usage_logger import log_usage

logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all API requests for usage tracking."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Log API request and response.

        Args:
            request: FastAPI request object
            call_next: Next middleware/handler in chain

        Returns:
            Response from the next handler
        """
        # Skip logging for health check and static files
        if request.url.path in ["/health", "/", "/docs", "/openapi.json"]:
            return await call_next(request)

        # Skip logging for static files
        if request.url.path.startswith("/static"):
            return await call_next(request)

        # Record start time
        start_time = time.time()

        # Process request
        response = await call_next(request)

        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)

        # Extract user_id from request state (set by auth middleware)
        user_id = getattr(request.state, "user_id", None)

        # Only log if user is authenticated
        if user_id:
            try:
                # Get database session
                db_gen = get_db()
                db = await anext(db_gen)

                try:
                    # Log usage via common function
                    # Issue #50: Pass workspace_id (set by RateLimitMiddleware)
                    workspace_id = getattr(request.state, "workspace_id", None)
                    await log_usage(
                        db=db,
                        user_id=user_id,
                        endpoint=request.url.path,
                        method=request.method,
                        status_code=response.status_code,
                        response_time_ms=response_time_ms,
                        workspace_id=workspace_id,
                    )

                finally:
                    await db.close()

            except Exception as e:
                logger.error(
                    "request_logging_middleware_failed",
                    error=str(e),
                    user_id=user_id,
                )

        return response
