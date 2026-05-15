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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import API_KEY_PREFIX
from models.auth import OAuth2Token
from utils.logger import get_logger

logger = get_logger(__name__)


async def verify_oauth_bearer_token(
    access_token: str,
    db: AsyncSession,
) -> tuple[str, str | None] | None:
    """Verify an OAuth 2.0 Bearer access token issued by the device flow.

    Looks up the token row in ``oauth_tokens``, checks expiry and
    revocation, and returns ``(user_id, scope)`` — ``scope`` is a
    space-separated RFC 6749 §3.3 string and may be ``None`` for tokens
    issued without scope (legacy / non-CLI clients). Returns ``None`` if
    the token is unknown, expired, revoked, or if the lookup itself
    fails (matches ``verify_api_key``'s silent-failure contract so a
    transient DB error surfaces as 401 to the client, not 500).

    The async query uses the caller's request-scoped session so this
    function does not contend with the smaller sync pool that Authlib's
    grant endpoints depend on, and avoids the per-request thread hop
    that an ``asyncio.to_thread`` wrapper would incur on every
    authenticated REST request.
    """
    try:
        stmt = select(OAuth2Token).where(OAuth2Token.access_token == access_token)
        result = await db.execute(stmt)
        token = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.error("oauth_bearer_token_lookup_failed", error=str(exc))
        return None

    if token is None:
        logger.debug("oauth_bearer_token_not_found")
        return None
    if token.is_revoked():
        logger.debug("oauth_bearer_token_revoked", token_id=token.id)
        return None
    if token.is_expired():
        logger.debug("oauth_bearer_token_expired", token_id=token.id)
        return None
    return (token.user_id, token.scope)


def is_oauth_bearer_token(token: str) -> bool:
    """Distinguish OAuth Bearer tokens from kagura API keys by prefix.

    Cheap prefix check used to skip the API-key DB lookup for OAuth
    tokens. Kagura API keys start with ``"kagura_"``; OAuth tokens
    issued by ``oauth2_server`` are 43-char ``secrets.token_urlsafe(32)``
    strings, which never collide with that prefix.
    """
    return bool(token) and not token.startswith(API_KEY_PREFIX)
