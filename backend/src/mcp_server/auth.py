"""Authentication helpers for MCP Transport.

Provides unified authentication for MCP over HTTP/SSE:
- OAuth2 Bearer tokens (from ChatGPT, etc.)
- API Key Bearer tokens (from Claude Code, etc.)
- Session cookies (from Web UI)

Authentication is always required. No anonymous access is allowed.

OAuth2 access tokens (#1686): a token bound to an RFC 8707 resource must be
bound to this server's MCP resource (``auth.mcp_resource.is_same_mcp_resource``),
and its granted scopes are recorded per request for the ``tools/call`` scope
check (``mcp_server.tools._scopes``).

Adapted from v4.4.0 mcp_auth.py for memory-cloud architecture.
"""

import contextlib
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from auth.mcp_resource import is_same_mcp_resource
from utils.exceptions import AuthenticationError, InvalidTokenError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class MissingCredentialsError(AuthenticationError):
    """The request carries no credentials at all (no Bearer token, no valid session).

    RFC 6750 §3.1: the challenge for such a request carries no error code.
    """


@dataclass(frozen=True)
class OAuthGrant:
    """What an active OAuth2 access token grants on ``/mcp``."""

    user_id: str
    scope: str | None
    resource: str | None
    # The issuing client's registered scope; read only when ``scope`` names no
    # ``memory:*`` scope (see ``granted_scopes``).
    client_scope: str | None = None
    # True when that read failed: the token then grants no ``memory:*`` scope.
    client_scope_unavailable: bool = False


# The OAuth scopes of the current request's access token; ``None`` when the
# request is not authenticated with an OAuth2 access token (API key, agent-bound
# key, session cookie), which the ``tools/call`` scope check does not gate.
# Set once per request by ``authenticate_mcp_request``.
_mcp_oauth_scopes: ContextVar[frozenset[str] | None] = ContextVar("mcp_oauth_scopes", default=None)


def get_mcp_oauth_scopes() -> frozenset[str] | None:
    """The current request's OAuth scopes, or ``None`` for a non-OAuth credential."""
    return _mcp_oauth_scopes.get()


_SCOPE_SEPARATORS = re.compile(r"[\s,]+")


def _scope_set(scope: str | None) -> frozenset[str]:
    """Scope names in a stored scope string (space- or comma-separated; empty = none)."""
    return frozenset(name for name in _SCOPE_SEPARATORS.split(scope or "") if name)


def _memory_scopes(scopes: frozenset[str]) -> frozenset[str]:
    return frozenset(name for name in scopes if name.startswith("memory:"))


def names_memory_scope(scope: str | None) -> bool:
    """Whether a stored scope string names any ``memory:*`` scope."""
    return bool(_memory_scopes(_scope_set(scope)))


def granted_scopes(
    scope: str | None,
    client_scope: str | None = None,
    *,
    client_scope_unavailable: bool = False,
) -> frozenset[str]:
    """The scopes an OAuth access token grants on ``/mcp``.

    The ``memory:*`` scopes the token's stored scope names. A token whose
    scope names none (an empty scope, or one such as ``openid
    offline_access`` or a client-specific value) gets the ``memory:*`` scopes
    its client registered, or the DCR default scope when the client is
    unknown or registered none. When the client's registration could not be
    read, such a token grants no ``memory:*`` scope (fail closed: every
    ``tools/call`` is refused with ``insufficient_scope``). The token's other
    scopes are kept, so they reappear in an ``insufficient_scope`` challenge.
    """
    stored = _scope_set(scope)
    if _memory_scopes(stored):
        return stored
    if client_scope_unavailable:
        logger.warning("MCP OAuth token names no memory scope and its client could not be read")
        return stored
    registered = _memory_scopes(_scope_set(client_scope))
    if registered:
        source, memory = "the client's registered scope", registered
    else:
        from auth.mcp_scopes import DCR_DEFAULT_SCOPE

        source, memory = "the DCR default scope", _memory_scopes(_scope_set(DCR_DEFAULT_SCOPE))
    logger.debug(f"MCP OAuth token names no memory scope: using {source}")
    return stored | memory


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
        MissingCredentialsError: If the request carries no credentials
        InvalidTokenError: If the Bearer token is invalid, expired, revoked, or
            an OAuth2 access token bound to another resource
        AuthenticationError: If the Authorization header is malformed

    Example:
        >>> user_id, context_id, workspace_id = await authenticate_mcp_request("Bearer api_key_...")
        >>> # Returns: ("user_12345", None, UUID("...")) for workspace-scoped API key
    """
    # Issue #1275: reset the per-request agent scope BEFORE any auth branch —
    # session/OAuth paths never carry an agent binding, and the API-key branch
    # (auth.dependencies.verify_api_key) re-sets it when the key is
    # agent-bound. Defense in depth against a stale scope in reused contexts.
    # #1686: the OAuth scopes likewise; only the OAuth2 branch sets them.
    from auth.agent_scope import set_agent_scope

    set_agent_scope(None)
    _mcp_oauth_scopes.set(None)

    # Try session cookie first (Issue #155: Claude Web UI support)
    # Issue #245: context_id is no longer returned from auth (now required in tool args)
    if not authorization_header and cookie_header:
        user_id = await _verify_session_cookie(cookie_header)
        if user_id:
            logger.info(f"MCP auth success: method=session_cookie, user={user_id}")
            return (user_id, None, None)  # Session auth doesn't have context/workspace scope

    # No auth header and no session → authentication required
    if not authorization_header:
        raise MissingCredentialsError("Authorization header or session cookie required")

    # Parse Authorization header
    if isinstance(authorization_header, bytes):
        auth_str = authorization_header.decode("utf-8")
    else:
        auth_str = authorization_header

    # RFC 7235 §2.1: the scheme is case-insensitive. Whitespace around the
    # token is tolerated; an empty token or one containing whitespace is a
    # malformed request (invalid_request), not an invalid token.
    parts = auth_str.strip().split(None, 1)
    if not parts or parts[0].lower() != "bearer":
        raise AuthenticationError("Invalid authorization header format. Expected: Bearer {token}")
    token = parts[1].strip() if len(parts) == 2 else ""
    if not token:
        raise AuthenticationError("Malformed Authorization header: the Bearer token is empty")
    if any(ch.isspace() for ch in token):
        raise AuthenticationError(
            "Malformed Authorization header: the Bearer token contains whitespace"
        )
    # Issue #965: log at most an 8-char prefix (repo-wide redaction convention,
    # cf. session_id[:8] / refresh_token[:8] / oauth token_prefix=token[:8]).
    # Enough to correlate log lines without exposing usable key material. For
    # kagura_ API keys this is the 7-char prefix + 1 body char; for opaque
    # OAuth2 bearer tokens (no prefix) it is the first 8 token chars. Previously
    # logged token[:20], leaking ~13 chars of entropy past the kagura_ prefix.
    logger.debug(f"MCP auth attempt: token={token[:8]}...")

    # Try API Key authentication
    result = await _verify_api_key(token)
    if result:
        user_id, context_id, workspace_id = result
        logger.info(
            f"MCP auth success: method=api_key, user={user_id}, context={context_id}, workspace={workspace_id}"
        )
        return result

    # Try OAuth2 token verification (Issue #33)
    grant = await _verify_oauth2_token(token)
    if grant:
        # #1686: RFC 8707 audience — any form of this server's MCP resource
        # (``/mcp``, ``/mcp/w/<id>``, default port, host case; query ignored).
        # A token issued without ``resource`` carries no audience and is accepted.
        if grant.resource and not is_same_mcp_resource(grant.resource):
            logger.warning(
                f"MCP auth failed: method=oauth2, audience {grant.resource[:200]!r} "
                f"is not this server's MCP resource, token={token[:8]}..."
            )
            raise InvalidTokenError(
                "The access token was issued for a different resource. "
                "Re-authorize this server to get a token for it."
            )
        _mcp_oauth_scopes.set(
            granted_scopes(
                grant.scope,
                grant.client_scope,
                client_scope_unavailable=grant.client_scope_unavailable,
            )
        )
        logger.info(f"MCP auth success: method=oauth2, user={grant.user_id}")
        # Issue #245: context_id is now required in tool args, not from auth
        return (grant.user_id, None, None)

    # Authentication failed
    logger.warning(f"MCP auth failed: token={token[:8]}...")
    raise InvalidTokenError("Invalid or expired Bearer token")


async def _verify_api_key(api_key: str) -> tuple[str, "UUID | None", "UUID | None"] | None:
    """Verify API key.

    Migration 034: verify_api_key() returned a 2-tuple of (user_id, workspace_id).
    Issue #626: verify_api_key() now returns ``VerifiedKey``; we read attributes
    by name. The MCP auth surface remains a 3-tuple ``(user_id, context_id,
    workspace_id)`` for backward compatibility with MCP tool args (Issue #245).

    **Public-bound key rejection (#626)**: ``auth.dependencies.verify_api_key``
    (the standalone wrapper called below) returns ``None`` for any key with
    ``bound_context_id != None``. That treats a public-bound key as
    "invalid" on the MCP surface — preventing the privilege escalation
    where a key intended for one public context would otherwise inherit
    the owner's workspace_id and grant full account access on MCP tools.
    Bound keys are only honored on the REST public endpoint
    (``/api/v1/public/{ctx}/*`` via
    ``api.routes.public_search._resolve_public_attribution``, which
    reaches ``APIKeyManager.verify_key`` directly and bypasses this
    wrapper). MCP introspection of bindings is a separate follow-up.

    Args:
        api_key: API key value

    Returns:
        (user_id, context_id, workspace_id) tuple if key is valid AND not
        public-bound; None otherwise (invalid / revoked / expired / bound).
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


async def _verify_oauth2_token(access_token: str) -> OAuthGrant | None:
    """Verify an OAuth2 access token and return what it grants.

    Uses the verifier REST shares (``auth.oauth2_bearer``), reading the
    token's scope and RFC 8707 ``resource`` as well: the caller checks the
    audience and records the scopes for the ``tools/call`` scope check. MCP's
    transport entry point has no FastAPI-injected session, so this shim opens
    its own short-lived async session — symmetric with ``_verify_api_key``
    above, which also calls a session-managing wrapper.
    """
    from auth.oauth2_bearer import find_active_oauth_token
    from db.base import get_db

    # ``aclosing`` closes the ``get_db`` generator — and with it the session,
    # including one whose client-scope query failed mid-transaction — when
    # this returns, not whenever the generator is garbage-collected.
    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            token = await find_active_oauth_token(access_token, db)
            if token is None:
                return None
            client_scope = None
            client_scope_unavailable = False
            if not names_memory_scope(token.scope):
                try:
                    client_scope = await _registered_client_scope(token.client_id, db)
                except Exception as e:  # noqa: BLE001 - fail closed, see granted_scopes
                    logger.warning(f"MCP OAuth client scope lookup failed: {type(e).__name__}")
                    client_scope_unavailable = True
            return OAuthGrant(
                user_id=token.user_id,
                scope=token.scope,
                resource=token.resource,
                client_scope=client_scope,
                client_scope_unavailable=client_scope_unavailable,
            )
    return None


async def _registered_client_scope(client_id: str, db: "AsyncSession") -> str | None:
    """The scope the OAuth client registered, or ``None`` for an unknown client.

    Raises:
        Exception: Whatever the lookup raises; the caller fails closed.
    """
    from sqlalchemy import select

    from models.auth import OAuth2Client

    result = await db.execute(select(OAuth2Client.scope).where(OAuth2Client.client_id == client_id))
    return result.scalar_one_or_none()


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
