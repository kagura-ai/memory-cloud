"""The default context bound to a credential (#2129): reads and writes.

A user API key (``api_keys.default_context_id``) and an OAuth grant
(``oauth_grant_settings``) can name the context an MCP call without
``context_id`` runs in. The MCP side only reads the stored id
(``mcp_server.tools._default_context``); this module serves the places that
set it — key management, the consent page and the connected-apps routes.

A default is a preference, not a grant. Setting one checks that the user can
read the context now (the same read chokepoint the MCP read tools use, so a
deny is the uniform "not found"); every call made under it is authorized
again by the tool's own resolver, so a context the user loses later only
makes the defaulted calls fail ``context_not_found``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    Context,
    OAuth2Client,
    OAuth2Token,
    OAuthGrantSetting,
    Workspace,
    WorkspaceMember,
)
from services.capacity_lock import CapacityPolicy
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# The consent page lists at most this many contexts (name only).
CONSENT_CONTEXT_CAP = 200


async def readable_context(
    db: AsyncSession,
    *,
    user_id: str,
    context_id: UUID,
    key_workspace_id: UUID | None = None,
) -> Context | None:
    """The context if ``user_id`` can read it as viewer now, else ``None``.

    Read through ``resolve_context_for_workspace_read`` (capacity ALLOWED:
    naming a context reads no memory). Every deny — unknown, deleted, other
    workspace, outside ``key_workspace_id``, private non-creator, whitelist
    miss, no seat — is ``None``, with nothing that tells them apart.

    Args:
        db: The request's session.
        user_id: The credential's owner.
        context_id: The context to check.
        key_workspace_id: The workspace a workspace-scoped key is confined
            to; ``None`` for a global key, a session or an OAuth grant.

    Returns:
        The ``Context`` row, or ``None`` when the user cannot read it.
    """
    from services.permission_service import PermissionService
    from utils.exceptions import AuthorizationError, FeatureNotAvailableError, NotFoundException

    try:
        return await PermissionService(db).resolve_context_for_workspace_read(
            user_id=user_id,
            context_id=context_id,
            required_role="viewer",
            key_workspace_id=key_workspace_id,
            capacity=CapacityPolicy.ALLOWED,
        )
    except (NotFoundException, FeatureNotAvailableError, AuthorizationError):
        logger.debug("credential_default_context_unreadable")
        return None


async def readable_context_name(
    db: AsyncSession,
    *,
    user_id: str,
    context_id: UUID | None,
    key_workspace_id: UUID | None = None,
) -> str | None:
    """The context's name when it is set and readable, else ``None``."""
    if context_id is None:
        return None
    context = await readable_context(
        db, user_id=user_id, context_id=context_id, key_workspace_id=key_workspace_id
    )
    return context.name if context is not None else None


@dataclass(frozen=True)
class ContextOption:
    """One entry of the consent page's default-context picker."""

    context_id: UUID
    name: str
    workspace_name: str


async def readable_context_options(
    db: AsyncSession, *, user_id: str, limit: int = CONSENT_CONTEXT_CAP
) -> list[ContextOption]:
    """The contexts ``user_id`` can read, across the workspaces they belong to.

    One membership query, then the permission-scoped listing per workspace
    (``PermissionService.get_accessible_contexts`` — the rules
    ``list_contexts`` applies: privacy, whitelists, seats). A workspace the
    user cannot list (no seat) contributes nothing. Ordered by workspace
    name, then context name; capped at ``limit``.
    """
    from services.permission_service import PermissionService
    from utils.exceptions import AuthorizationError, FeatureNotAvailableError, NotFoundException

    rows = (
        await db.execute(
            select(Workspace.id, Workspace.name)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(WorkspaceMember.user_id == user_id)
            .order_by(Workspace.name, Workspace.id)
        )
    ).all()
    permissions = PermissionService(db)
    options: list[ContextOption] = []
    for workspace_id, workspace_name in rows:
        try:
            contexts = await permissions.get_accessible_contexts(user_id, workspace_id)
        except (AuthorizationError, FeatureNotAvailableError, NotFoundException):
            continue
        for context in sorted(contexts, key=lambda c: (c.name or "").lower()):
            options.append(ContextOption(context.id, context.name, workspace_name))
            if len(options) >= limit:
                return options
    return options


async def grant_default_context_id(
    db: AsyncSession, *, user_id: str, client_id: str
) -> UUID | None:
    """The stored default of the user's grant to ``client_id`` (no read check)."""
    return (
        await db.execute(
            select(OAuthGrantSetting.default_context_id).where(
                OAuthGrantSetting.user_id == user_id,
                OAuthGrantSetting.client_id == client_id,
            )
        )
    ).scalar_one_or_none()


async def set_grant_default_context(
    db: AsyncSession, *, user_id: str, client_id: str, context_id: UUID | None
) -> None:
    """Store the grant's default (``None`` clears it). Does not commit.

    Clearing without a row writes nothing; setting creates the row when
    there is none. The caller has checked the context is readable.
    """
    row = await db.get(OAuthGrantSetting, (user_id, client_id))
    if row is None:
        if context_id is None:
            return
        db.add(
            OAuthGrantSetting(
                user_id=user_id,
                client_id=client_id,
                default_context_id=context_id,
                updated_at=utcnow(),
            )
        )
        await db.flush()
        return
    row.default_context_id = context_id
    row.updated_at = utcnow()
    await db.flush()


@dataclass(frozen=True)
class GrantSummary:
    """One OAuth client the user has authorized, with its settings."""

    client_id: str
    client_name: str
    scopes: list[str]
    default_context_id: UUID | None
    last_issued_at: datetime | None


async def list_grants(db: AsyncSession, *, user_id: str) -> list[GrantSummary]:
    """The user's OAuth grants: clients with a live token, plus settings rows.

    A live token is one not revoked. ``scopes`` is the union of the live
    tokens' scopes; ``last_issued_at`` the newest live issuance. A client with
    a settings row but no live token is listed too (no scopes, no issuance),
    so its default can still be cleared. Newest first.
    """
    live = (
        await db.execute(
            select(
                OAuth2Token.client_id,
                func.max(OAuth2Token.issued_at),
                func.array_agg(func.distinct(OAuth2Token.scope)),
            )
            .where(OAuth2Token.user_id == user_id, OAuth2Token.revoked.is_(False))
            .group_by(OAuth2Token.client_id)
        )
    ).all()
    settings = dict(
        (
            await db.execute(
                select(OAuthGrantSetting.client_id, OAuthGrantSetting.default_context_id).where(
                    OAuthGrantSetting.user_id == user_id
                )
            )
        ).all()
    )
    issued: dict[str, tuple[datetime | None, list[str]]] = {}
    for client_id, last_issued_at, scope_values in live:
        scopes = sorted({s for value in scope_values or [] if value for s in value.split()})
        issued[client_id] = (last_issued_at, scopes)
    client_ids = set(issued) | set(settings)
    if not client_ids:
        return []
    names = dict(
        (
            await db.execute(
                select(OAuth2Client.client_id, OAuth2Client.client_name).where(
                    OAuth2Client.client_id.in_(client_ids)
                )
            )
        ).all()
    )
    grants = [
        GrantSummary(
            client_id=client_id,
            client_name=names.get(client_id, client_id),
            scopes=issued.get(client_id, (None, []))[1],
            default_context_id=settings.get(client_id),
            last_issued_at=issued.get(client_id, (None, []))[0],
        )
        for client_id in client_ids
        if client_id in names
    ]
    # Newest issuance first; grants without a live token last, by client id.
    grants.sort(key=lambda g: g.client_id)
    grants.sort(key=lambda g: (g.last_issued_at is not None, g.last_issued_at or 0), reverse=True)
    return grants
