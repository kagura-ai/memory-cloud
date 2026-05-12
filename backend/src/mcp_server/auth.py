"""Authentication helpers for MCP Transport.

Provides unified authentication for MCP over HTTP/SSE:
- OAuth2 Bearer tokens (from ChatGPT, etc.)
- API Key Bearer tokens (from Claude Code, etc.)
- Session cookies (from Web UI)

Authentication is always required. No anonymous access is allowed.

Adapted from v4.4.0 mcp_auth.py for memory-cloud architecture.
"""

import logging
from uuid import UUID

from utils.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


async def authenticate_mcp_request(
    authorization_header: str | bytes | None,
    cookie_header: bytes | None = None,
) -> tuple[str, "UUID | None", "UUID | None"]:
    """Authenticate MCP request using Bearer token or session cookie.

    Issue #116: Now returns both user_id and context_id for proper context scoping.
    Issue #155: Added session cookie authentication for Claude Web UI support.
    Issue #169: Now returns workspace_id for workspace-scoped API keys.

    Authentication is always required. No anonymous access is allowed.

    Supports:
    1. API Key Bearer tokens
    2. OAuth2 Bearer tokens
    3. Session cookies (OAuth2 Web login)

    Authentication Priority:
        1. API Key Bearer token (from api_keys table) - returns context_id/workspace_id if scoped
        2. OAuth2 Bearer token (from oauth_tokens table) - returns None for context_id/workspace_id
        3. Session cookie (from Redis) - returns user's current context_id

    Args:
        authorization_header: Authorization header value (e.g., "Bearer xyz...")
        cookie_header: Cookie header value (e.g., "kagura_session=...")

    Returns:
        (user_id, context_id, workspace_id) tuple
        - For workspace-scoped keys: context_id=None, workspace_id=<UUID>
        - For session/OAuth2: context_id=<UUID>, workspace_id=None

    Raises:
        AuthenticationError: If authentication fails

    Example:
        >>> user_id, context_id, workspace_id = await authenticate_mcp_request("Bearer api_key_...")
        >>> # Returns: ("user_12345", None, UUID("...")) for workspace-scoped API key
    """
    # Try session cookie first (Issue #155: Claude Web UI support)
    # Issue #245: context_id is no longer returned from auth (now required in tool args)
    if not authorization_header and cookie_header:
        user_id = await _verify_session_cookie(cookie_header)
        if user_id:
            logger.info(f"MCP auth success: method=session_cookie, user={user_id}")
            return (user_id, None, None)  # Session auth doesn't have context/workspace scope

    # No auth header and no session → authentication required
    if not authorization_header:
        raise AuthenticationError("Authorization header or session cookie required")

    # Parse Authorization header
    if isinstance(authorization_header, bytes):
        auth_str = authorization_header.decode("utf-8")
    else:
        auth_str = authorization_header

    if not auth_str.startswith("Bearer "):
        raise AuthenticationError("Invalid authorization header format. Expected: Bearer {token}")

    token = auth_str[7:]  # Remove "Bearer " prefix
    logger.debug(f"MCP auth attempt: token={token[:20]}...")

    # Try API Key authentication
    result = await _verify_api_key(token)
    if result:
        user_id, context_id, workspace_id = result
        logger.info(
            f"MCP auth success: method=api_key, user={user_id}, context={context_id}, workspace={workspace_id}"
        )
        return result

    # Try OAuth2 token verification (Issue #33)
    user_id_oauth = await _verify_oauth2_token(token)
    if user_id_oauth:
        logger.info(f"MCP auth success: method=oauth2, user={user_id_oauth}")
        # Issue #245: context_id is now required in tool args, not from auth
        return (user_id_oauth, None, None)

    # Authentication failed
    logger.warning(f"MCP auth failed: token={token[:20]}...")
    raise AuthenticationError("Invalid or expired Bearer token")


async def _verify_api_key(api_key: str) -> tuple[str, "UUID | None", "UUID | None"] | None:
    """Verify API key.

    Migration 034: verify_api_key() returned a 2-tuple of (user_id, workspace_id).
    Issue #626: verify_api_key() now returns ``VerifiedKey`` — we read attributes
    by name. The MCP auth surface remains a 3-tuple ``(user_id, context_id,
    workspace_id)`` for backward compatibility with MCP tool args (Issue #245).
    ``bound_context_id`` is **not** propagated into the MCP context_id slot:
    MCP read tools enforce binding separately in ``list_my_bindings`` /
    ``describe_binding`` rather than re-using the principal's MCP context_id.

    Args:
        api_key: API key value

    Returns:
        (user_id, context_id, workspace_id) tuple if key is valid, None otherwise.
        - context_id is always None (now required in tool arguments)
        - workspace_id is from the API key scope (workspace-scoped keys)
    """
    try:
        from auth.dependencies import verify_api_key

        result = await verify_api_key(api_key)

        if result:
            user_id = result.user_id
            workspace_id = result.workspace_id
            context_id = None  # Issue #245: context_id is now in tool args
            logger.debug(f"API key valid: user={user_id}, workspace={workspace_id}")
            return (user_id, context_id, workspace_id)
        else:
            logger.debug("API key invalid")
            return None

    except Exception as e:
        logger.error(f"API key verification error: {e}")
        return None


async def _verify_oauth2_token(access_token: str) -> str | None:
    """Verify OAuth2 access token.

    Args:
        access_token: OAuth2 access token value

    Returns:
        user_id if token is valid

    Raises:
        TokenExpiredError: Token has expired
        TokenRevokedError: Token has been revoked
        InvalidTokenError: Token is invalid or not found
    """
    import asyncio

    from db.base import get_sync_session
    from models.auth import OAuth2Token
    from utils.exceptions import InvalidTokenError, TokenExpiredError, TokenRevokedError

    # Issue #102: Wrap sync DB call in thread to avoid blocking event loop
    def _verify_sync():
        db_session = get_sync_session()
        try:
            token = db_session.query(OAuth2Token).filter_by(access_token=access_token).first()

            if not token:
                logger.debug("OAuth2 token not found")
                raise InvalidTokenError("Token not found in database")

            # Check if token is revoked
            if token.is_revoked():
                logger.debug(f"OAuth2 token revoked: token_id={token.id}")
                raise TokenRevokedError()

            # Check if token is expired
            if token.is_expired():
                logger.debug(f"OAuth2 token expired: token_id={token.id}")
                raise TokenExpiredError()

            logger.debug(f"OAuth2 token valid: user={token.user_id}")
            return token.user_id

        except (TokenExpiredError, TokenRevokedError, InvalidTokenError):
            # Re-raise specific token errors
            raise
        except Exception as e:
            logger.error(f"OAuth2 token verification error: {e}")
            raise InvalidTokenError(f"Token verification failed: {e}") from e
        finally:
            db_session.close()

    # Run in thread pool to avoid blocking event loop
    return await asyncio.to_thread(_verify_sync)


async def _verify_session_cookie(cookie_header: bytes) -> str | None:
    """Verify session cookie and extract user_id.

    Issue #155: Support OAuth2 Web login via session cookie for Claude Web UI.
    Issue #245: Simplified - no longer returns context_id (now required in tool args).

    Args:
        cookie_header: Cookie header value (e.g., b"kagura_session=...; other=...")

    Returns:
        user_id if session is valid, None otherwise

    Raises:
        None - returns None on any error for graceful fallback
    """
    from http.cookies import CookieError, SimpleCookie

    from auth.session import SessionManager
    from config.database import get_redis_url

    try:
        # Parse cookie header safely using http.cookies.SimpleCookie
        cookie_str = (
            cookie_header.decode("utf-8") if isinstance(cookie_header, bytes) else cookie_header
        )
        cookies = SimpleCookie()
        cookies.load(cookie_str)

        # Extract kagura_session cookie
        session_cookie = cookies.get("kagura_session")
        if not session_cookie:
            logger.debug("No kagura_session cookie found")
            return None

        session_id = session_cookie.value

        # Get session data from Redis (SessionManager uses singleton, no cleanup needed)
        session_manager = SessionManager(redis_url=get_redis_url())
        session_data = session_manager.get_session(session_id)

        # Validate session data structure
        if not session_data or not isinstance(session_data, dict):
            logger.debug(f"Invalid session data: {session_id[:8]}...")
            return None

        # Extract user_id from session (use "sub" or "user_id")
        user_id = session_data.get("user_id") or session_data.get("sub")
        if not user_id or not isinstance(user_id, str):
            logger.warning(f"Session missing valid user_id: {session_id[:8]}...")
            return None

        logger.debug(f"Session cookie auth: user={user_id}")
        return user_id

    except (UnicodeDecodeError, CookieError) as e:
        logger.warning(f"Cookie parsing error: {e}")
        return None
    except Exception as e:
        logger.error(f"Session cookie verification error: {e}")
        return None
