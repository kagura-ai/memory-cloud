"""Async OIDC Bearer authentication, isolated from general memory credentials."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.oidc_limits import check_oidc_rate_limit
from auth.oidc_server import credential_hash
from config.settings import get_settings
from db.base import get_db
from models.auth import User
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from utils.datetime import utcnow

_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="OIDCIdentity",
    description="Isolated OIDC access token with explicit consented scopes; no session or general memory credentials.",
)
_TOKEN = re.compile(r"kmc_oidc_at_[A-Za-z0-9_-]{43}")
_SCOPE = "workspaces:read"


@dataclass(frozen=True)
class OIDCPrincipal:
    """Stable subject proved by a live identity grant on this request."""

    user_id: str


async def identity_access_token(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
) -> str:
    """Reject other credential namespaces and excessive work before opening a DB."""
    if not get_settings().oidc_enabled:
        raise HTTPException(404, "not_found")
    headers = request.headers.getlist("authorization")
    parts = headers[0].split() if len(headers) == 1 else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not _TOKEN.fullmatch(parts[1]):
        raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
    limited = await check_oidc_rate_limit(request)
    if limited is not None:
        raise HTTPException(
            limited.status_code,
            "temporarily_unavailable",
            headers={"Retry-After": limited.headers.get("Retry-After", "1")},
        )
    return parts[1]


async def identity_principal(
    token: Annotated[str, Depends(identity_access_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OIDCPrincipal:
    """Recheck token, grant, client, account and issuance scope without a cache."""
    row = (
        await db.execute(
            select(User.user_id, OIDCToken.scope, OIDCGrant.scope, OIDCClient.scope)
            .select_from(OIDCToken)
            .join(OIDCGrant, OIDCGrant.id == OIDCToken.grant_id)
            .join(OIDCClient, OIDCClient.client_id == OIDCGrant.client_id)
            .join(User, User.user_id == OIDCGrant.user_id)
            .where(
                OIDCToken.access_token_hash == credential_hash(token),
                OIDCToken.revoked_at.is_(None),
                OIDCGrant.revoked_at.is_(None),
                OIDCClient.revoked_at.is_(None),
                OIDCToken.issued_at + OIDCToken.expires_in > int(time.time()),
                OIDCGrant.expires_at > utcnow(),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(
            401, "invalid_token", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
        )
    subject, token_scope, grant_scope, client_scope = row
    if any(_SCOPE not in scope.split() for scope in (token_scope, grant_scope, client_scope)):
        raise HTTPException(
            403,
            "insufficient_scope",
            headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{_SCOPE}"'},
        )
    return OIDCPrincipal(user_id=subject)
