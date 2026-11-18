"""Isolated OIDC scope checks for synchronization control, not worker identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import ColumnElement, select
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


async def _authority_row(
    db: AsyncSession, condition: ColumnElement[bool], scope: str, *, lock: bool
) -> tuple[OIDCToken, OIDCGrant, OIDCClient, int]:
    """Load one token row with its grant, client and account; refuse revocation or scope."""
    stmt = (
        select(OIDCToken, OIDCGrant, OIDCClient, User.id)
        .select_from(OIDCToken)
        .join(OIDCGrant, OIDCGrant.id == OIDCToken.grant_id)
        .join(OIDCClient, OIDCClient.client_id == OIDCGrant.client_id)
        .join(User, User.user_id == OIDCGrant.user_id)
        .where(condition)
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
    if row is None or any(item.revoked_at is not None for item in row[:3]):
        raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
    if any(scope not in item.scope.split() for item in row[:3]):
        raise HTTPException(
            403, "insufficient_scope", headers={"WWW-Authenticate": f'Bearer scope="{scope}"'}
        )
    token, grant, client, account_id = row
    return token, grant, client, account_id


async def load_sync_identity(
    db: AsyncSession, token_hash: str, scope: str, *, lock: bool = False, audit_context: bool = True
) -> SyncPrincipal:
    """Validate all authority scopes, optionally holding nonblocking shared locks."""
    token, grant, client, account_id = await _authority_row(
        db, OIDCToken.access_token_hash == token_hash, scope, lock=lock
    )
    expiry = datetime.fromtimestamp(token.issued_at + token.expires_in, UTC).replace(tzinfo=None)
    principal = SyncPrincipal(
        grant.user_id, client.client_id, token_hash, scope, min(expiry, grant.expires_at)
    )
    principal.assert_live()
    from services.sync_audit import bind_actor, human_actor

    if audit_context:
        bind_actor(human_actor(principal.user_id, principal.client_id, account_id=account_id), db)
    return principal


async def load_login_identity(
    db: AsyncSession, token_id: UUID, scope: str, *, lock: bool = False
) -> SyncPrincipal:
    """Recheck the login (OIDC grant) behind a stored token row, not that token's lifetime.

    A manual synchronization request is bound to the login that made it
    (#2911): the access token it was made with may have expired or been
    rotated since. The grant and client must still be unrevoked and carry
    ``scope``, the grant must be unexpired, and the stored token row itself
    must not be revoked. The principal expires with the grant. Same rows and
    the same nonblocking shared locks as ``load_sync_identity``; no audit actor
    is bound, because the caller is not this login's user.
    """
    token, grant, client, _account_id = await _authority_row(
        db, OIDCToken.id == token_id, scope, lock=lock
    )
    principal = SyncPrincipal(
        grant.user_id, client.client_id, token.access_token_hash, scope, grant.expires_at
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
