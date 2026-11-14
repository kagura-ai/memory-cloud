"""Isolated OIDC scope checks for synchronization control, not worker identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.oidc_bearer import identity_access_token
from auth.oidc_server import credential_hash
from db.base import get_db
from models.auth import User
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from utils.datetime import utcnow
from utils.db_helpers import is_lock_not_available
from utils.exceptions import ConflictError


@dataclass(frozen=True)
class SyncPrincipal:
    """Proof of one live scoped login; reloaded inside every control transaction."""

    user_id: str
    client_id: str
    token_hash: str = field(repr=False)
    scope: str
    expires_at: datetime

    def assert_live(self) -> None:
        """Check wall-clock lifetime after the last awaited operation."""
        if utcnow() >= self.expires_at:
            raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})


async def load_sync_identity(
    db: AsyncSession, token_hash: str, scope: str, *, lock: bool = False
) -> SyncPrincipal:
    """Validate all authority scopes, optionally holding nonblocking shared locks."""
    stmt = (
        select(OIDCToken, OIDCGrant, OIDCClient)
        .select_from(OIDCToken)
        .join(OIDCGrant, OIDCGrant.id == OIDCToken.grant_id)
        .join(OIDCClient, OIDCClient.client_id == OIDCGrant.client_id)
        .join(User, User.user_id == OIDCGrant.user_id)
        .where(OIDCToken.access_token_hash == token_hash)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update(read=True, nowait=True, of=(OIDCClient, OIDCGrant, OIDCToken))
    try:
        row = (await db.execute(stmt)).first()
    except DBAPIError as exc:
        if is_lock_not_available(exc):
            raise ConflictError("Identity authority is changing; retry") from exc
        raise
    if row is None or any(item.revoked_at is not None for item in row):
        raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
    token, grant, client = row
    if any(scope not in item.scope.split() for item in row):
        raise HTTPException(
            403, "insufficient_scope", headers={"WWW-Authenticate": f'Bearer scope="{scope}"'}
        )
    expiry = datetime.fromtimestamp(token.issued_at + token.expires_in, UTC).replace(tzinfo=None)
    principal = SyncPrincipal(
        grant.user_id, client.client_id, token_hash, scope, min(expiry, grant.expires_at)
    )
    principal.assert_live()
    return principal


async def sync_manager(
    token: Annotated[str, Depends(identity_access_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncPrincipal:
    """Require the explicit management scope, with no session or API-key fallback."""
    return await load_sync_identity(db, credential_hash(token), "integrations:manage")


async def sync_reader(
    token: Annotated[str, Depends(identity_access_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncPrincipal:
    """Read metadata with the existing isolated workspace inspection scope."""
    return await load_sync_identity(db, credential_hash(token), "workspaces:read")


async def sync_records_reader(
    token: Annotated[str, Depends(identity_access_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncPrincipal:
    """Explicit data-read consent; workspace membership scope never implies it."""
    return await load_sync_identity(db, credential_hash(token), "sync.records:read")
