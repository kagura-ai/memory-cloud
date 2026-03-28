"""Session middleware for extracting authenticated user from cookies.

Issue #650 - Google OAuth2 Web Login & API Key Management
Issue #115 - Cookie name changed from 'session_id' to 'kagura_session'

Extracts kagura_session from HttpOnly cookie, validates session in Redis,
and injects user info into request.state for downstream handlers.

Example:
    from fastapi import FastAPI, Request, Depends
    from kagura.api.middleware.session import SessionMiddleware

    app = FastAPI()
    app.add_middleware(SessionMiddleware, session_manager=session_manager)

    @app.get("/protected")
    async def protected(request: Request):
        if not hasattr(request.state, "user"):
            raise HTTPException(401, "Not authenticated")
        return {"user": request.state.user}
"""

import logging
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from auth.session import SessionManager

logger = logging.getLogger(__name__)


class SessionMiddleware(BaseHTTPMiddleware):
    """Extract and validate session from cookies.

    Middleware that:
    1. Extracts kagura_session from cookie (Issue #115)
    2. Validates session in Redis (via SessionManager)
    3. Injects user info into request.state.user
    4. Updates last_accessed timestamp

    Args:
        app: FastAPI application
        session_manager: SessionManager instance

    Example:
        >>> app.add_middleware(
        ...     SessionMiddleware,
        ...     session_manager=session_manager
        ... )
    """

    # Paths that don't require authentication
    PUBLIC_PATHS = {
        "/",
        "/health",
        "/api/v1/health",
        "/api/v1/system/info",
        "/api/v1/auth/google/login",
        "/api/v1/auth/google/callback",
        "/docs",
        "/openapi.json",
        "/redoc",
    }

    def __init__(self, app, session_manager: SessionManager):
        """Initialize middleware.

        Args:
            app: FastAPI application
            session_manager: SessionManager instance
        """
        super().__init__(app)
        self.session_manager = session_manager

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and inject user session.

        Args:
            request: FastAPI request
            call_next: Next middleware in chain

        Returns:
            Response from downstream handler

        Note:
            If session is valid, request.state.user will contain:
            {
                "sub": "user_id",
                "email": "user@example.com",
                "name": "User Name",
                "role": "admin"
            }
        """
        # Skip auth for public paths
        if request.url.path in self.PUBLIC_PATHS or request.url.path.startswith("/static/"):
            return await call_next(request)

        # Extract session_id from cookie (Issue #115: renamed from session_id to kagura_session)
        session_id = request.cookies.get("kagura_session")

        if session_id:
            # Validate session
            session_data = self.session_manager.get_session(session_id)

            if session_data:
                # Inject user into request state
                request.state.user = session_data

                # Set user_id for RequestLoggingMiddleware (Issue #93-1)
                request.state.user_id = (
                    session_data.get("user_id")
                    or session_data.get("sub")
                    or session_data.get("email")
                )

                logger.debug(
                    f"Authenticated request: {session_data.get('email')} "
                    f"(role={session_data.get('role')})"
                )
            else:
                logger.warning(f"Invalid session: {session_id[:8]}...")
        else:
            logger.debug(f"No session cookie: {request.url.path}")

        # Continue to next middleware/handler
        response = await call_next(request)

        return response


def get_session_user(request: Request) -> dict | None:
    """FastAPI dependency to get current authenticated user.

    Args:
        request: FastAPI request

    Returns:
        User session data or None if not authenticated

    Example:
        from fastapi import Depends

        @router.get("/protected")
        async def protected(user: dict = Depends(get_session_user)):
            if not user:
                raise HTTPException(401)
            return {"email": user["email"]}
    """
    return getattr(request.state, "user", None)
