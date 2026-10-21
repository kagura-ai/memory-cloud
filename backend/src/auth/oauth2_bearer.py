"""OAuth 2.0 Bearer access token verification (RFC 6749 §1.4, RFC 6750).

Shared verifier used by both REST ``/api/v1/*`` (via
``auth.dependencies.get_user_from_api_key_or_session``) and MCP ``/mcp``
(via ``mcp_server.auth.authenticate_mcp_request``). A single
implementation gates both transports so their validation contracts
cannot drift.

Distinct from ``auth.oauth2`` (``OAuth2Manager``), which is the Google
OAuth client manager for the Web login flow.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import API_KEY_PREFIX
from models.auth import OAuth2Token
from utils.logger import get_logger

logger = get_logger(__name__)


async def find_active_oauth_token(
    access_token: str,
    db: AsyncSession,
) -> OAuth2Token | None:
    """Look up an OAuth 2.0 Bearer access token that is neither revoked nor expired.

    Returns the ``oauth_tokens`` row, or ``None`` if the token is unknown,
    expired or revoked. A failed lookup (pool exhausted, database down)
    propagates instead of reading as an unknown token (#1742 for MCP,
    #1981 for REST): the app-wide handlers in ``api.main`` answer 503
    ``DB-002`` with ``details.reason`` and ``Retry-After``, and the MCP
    transport answers 503, so an outage never tells a client that its
    credential is bad (which would make it re-authorize). REST reads
    ``(user_id, scope)`` through ``verify_oauth_bearer_token``; MCP also
    reads the RFC 8707 ``resource`` (audience) from the row.

    The async query uses the caller's request-scoped session so this
    function does not contend with the smaller sync pool that Authlib's
    grant endpoints depend on, and avoids the per-request thread hop
    that an ``asyncio.to_thread`` wrapper would incur on every
    authenticated REST request.

    Args:
        access_token: The Bearer token.
        db: The caller's session.
    """
    stmt = select(OAuth2Token).where(OAuth2Token.access_token == access_token)
    token = (await db.execute(stmt)).scalar_one_or_none()

    if token is None:
        logger.debug("oauth_bearer_token_not_found")
        return None
    if token.is_revoked():
        logger.debug("oauth_bearer_token_revoked", token_id=token.id)
        return None
    if token.is_expired():
        logger.debug("oauth_bearer_token_expired", token_id=token.id)
        return None
    return token


async def verify_oauth_bearer_token(
    access_token: str,
    db: AsyncSession,
) -> tuple[str, str | None] | None:
    """Verify an OAuth 2.0 Bearer access token issued by the device flow.

    Returns ``(user_id, scope)`` for an active token (see
    ``find_active_oauth_token``) — ``scope`` is a space-separated RFC 6749
    §3.3 string and may be ``None`` for tokens issued without scope
    (legacy / non-CLI clients) — or ``None`` otherwise.
    """
    token = await find_active_oauth_token(access_token, db)
    if token is None:
        return None
    return (token.user_id, token.scope)


def is_oauth_bearer_token(token: str) -> bool:
    """Distinguish OAuth Bearer tokens from kagura API keys by prefix.

    Cheap prefix check that routes a Bearer token to one credential store
    on both REST (``get_user_from_api_key_or_session``) and MCP
    (``mcp_server.auth.authenticate_mcp_request``, #1994): an OAuth token
    never pays the API-key DB lookup, and vice versa. Kagura API keys
    start with ``"kagura_"``; OAuth tokens issued by ``oauth2_server`` are
    43-char ``secrets.token_urlsafe(32)`` strings, which collide with that
    prefix only with probability 64**-7 (about 2e-13) per token.
    """
    return bool(token) and not token.startswith(API_KEY_PREFIX)
