"""Resource Token Manager for Resource Ingest API authentication.

Issue #238: Resource-scoped API tokens for external systems.

Based on auth/api_keys.py pattern with resource-specific adaptations.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.resource import Resource, ResourceToken, WorkspaceConnector
from services.resource_lookup import resolve_resource_pk
from utils.logger import get_logger

logger = get_logger(__name__)

# Resource token prefix for easy identification
RESOURCE_TOKEN_PREFIX = "kagura_resource_"


def _require_scope_for_slug(
    resource_id: str | None, workspace_id: UUID | None, resource_pk: UUID | None
) -> None:
    """Refuse a slug filter that is not pinned to a workspace or a resource (#1877).

    A slug is only unique among one workspace's resources: another workspace
    can hold the same one, and can take it over once a context is deleted. A
    query by the bare slug therefore reads (and, for the callers that revoke
    what they read, writes) across tenants.
    """
    if resource_id and workspace_id is None and resource_pk is None:
        raise ValueError(
            "resource_id is not unique across workspaces: pass workspace_id or resource_pk with it"
        )


def resource_token_scope(
    workspace_id: UUID, resource_id: str, resource_pk: UUID | None
) -> ColumnElement[bool]:
    """Predicate for "the tokens of this workspace's resource" (#1877).

    ``resource_pk`` identifies exactly one ``resources`` row, hence one
    workspace, so a token carrying it matches on that alone — including a row
    whose shadow ``workspace_id`` column was never backfilled. A legacy token
    without ``resource_pk`` matches on slug **and** ``workspace_id``; one with
    neither column cannot be attributed to a workspace and is left alone (it
    cannot authenticate either: ``verify_token`` joins through ``resource_pk``).
    """
    legacy = and_(
        ResourceToken.resource_pk.is_(None),
        ResourceToken.resource_id == resource_id,
        ResourceToken.workspace_id == workspace_id,
    )
    if resource_pk is None:
        return legacy
    return or_(ResourceToken.resource_pk == resource_pk, legacy)


def workspace_token_scope(workspace_id: UUID) -> ColumnElement[bool]:
    """Predicate for "the tokens of this workspace" (#268, #1863, #1919).

    The workspace-wide form of :func:`resource_token_scope`: a token belongs
    to the workspace whose ``resources`` row its ``resource_pk`` points at —
    not to a live ``contexts`` row (a token keeps authenticating ingest after
    its last context is soft-deleted; ``verify_token`` never looks at
    contexts) and not to its shadow ``workspace_id`` column, which was never
    backfilled on some rows. A legacy token with ``resource_pk IS NULL`` (it
    cannot authenticate, but is listed so the owner can revoke it) falls back
    to its own ``workspace_id``.

    The statement must have ``Resource`` outer-joined on
    ``ResourceToken.resource_pk`` — :func:`workspace_tokens` does that.
    """
    return or_(
        Resource.workspace_id == workspace_id,
        and_(
            ResourceToken.resource_pk.is_(None),
            ResourceToken.workspace_id == workspace_id,
        ),
    )


def workspace_tokens(workspace_id: UUID, *columns: Any) -> Select[*tuple[Any, ...]]:
    """``SELECT columns`` over the resource tokens of ``workspace_id`` (#1919).

    Every list, lookup, count and sum of a workspace's tokens goes through
    this builder (REST list / PATCH / DELETE, the create-time cap, the quota
    ceiling, MCP ``setup_resource``, the downgrade-eligibility read), so a
    token that one of them sees is a token all of them see: a token that
    counts toward the cap or the ceiling can always be listed, updated and
    revoked, and a token of another workspace is a uniform miss everywhere.
    """
    return (
        select(*columns)
        .outerjoin(Resource, Resource.id == ResourceToken.resource_pk)
        .where(workspace_token_scope(workspace_id))
    )


def workspace_regular_active_tokens(workspace_id: UUID, *columns: Any) -> Select[*tuple[Any, ...]]:
    """The population of the token cap and the quota ceiling (#858, #1919).

    :func:`workspace_tokens` minus revoked tokens and minus connector-owned
    ones: the connector setup flow mints a resource token that bypasses the
    ``max_resource_tokens`` gate on purpose (connectors are gated by
    ``max_connectors`` seats), so counting it here would let it eat a regular
    slot post-mint and prematurely refuse a legitimate creation. The anti-join
    against ``workspace_connectors`` (UNIQUE ``resource_pk``, so no row
    inflation) drops exactly the connector-owned tokens; a regular token with
    a NULL ``resource_pk`` never matches the join and is still counted.
    """
    return (
        workspace_tokens(workspace_id, *columns)
        .outerjoin(
            WorkspaceConnector,
            WorkspaceConnector.resource_pk == ResourceToken.resource_pk,
        )
        .where(
            ResourceToken.is_active == True,  # noqa: E712
            WorkspaceConnector.id.is_(None),
        )
    )


# Namespaced like the other per-workspace advisory locks
# (``connector_seat:``, ``memory_analysis_quota:``) so the same workspace's
# keys hash apart; 64-bit ``hashtextextended`` rather than 32-bit ``hashtext``,
# which would collide across ~65k workspaces (PR #686).
_TOKEN_CAP_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


async def count_regular_active_tokens_for_mint(db: AsyncSession, workspace_id: UUID) -> int:
    """Count the cap population of ``workspace_id`` under its mint lock (#1927).

    Every path that mints a regular token — REST ``create_resource_token``
    and MCP ``setup_resource`` — calls this immediately before the cap check,
    then inserts the token and commits on the same session. The
    ``pg_advisory_xact_lock`` taken first is held until that transaction ends,
    so a second mint for the same workspace waits here until the first one
    has committed (or rolled back) and its COUNT then includes the new token:
    two owners minting at ``cap - 1`` can no longer both pass and leave the
    workspace one over ``max_resource_tokens``.

    Load-bearing: nothing between this call and the token INSERT may commit
    the session — a commit releases an xact lock. The connector setup flow
    is not a caller; its tokens are outside this population and its seats
    have their own lock (``connector_seat:``).
    """
    await db.execute(_TOKEN_CAP_LOCK_SQL.bindparams(key=f"resource_token_cap:{workspace_id}"))
    result = await db.execute(
        workspace_regular_active_tokens(workspace_id, func.count(ResourceToken.id))
    )
    return int(result.scalar() or 0)


class ResourceTokenManager:
    """Resource token manager for Resource Ingest API.

    Issue #238: Manages resource-scoped API tokens using async/await.

    Pattern: Based on APIKeyManager (auth/api_keys.py)
    """

    def __init__(self, db: AsyncSession):
        """Initialize resource token manager.

        Args:
            db: Async database session
        """
        self.db = db

    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash token using SHA256.

        Args:
            token: Plaintext token

        Returns:
            Hexadecimal hash string
        """
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _generate_token() -> str:
        """Generate a new resource token.

        Returns:
            Token string (format: kagura_resource_<random>)
        """
        random_part = secrets.token_urlsafe(32)
        return f"{RESOURCE_TOKEN_PREFIX}{random_part}"

    async def create_token(
        self,
        resource_id: str,
        *,
        resource_pk: UUID,
        workspace_id: UUID,
        description: str | None = None,
        quota_events_per_hour: int = 1000,
        created_by: str | None = None,
    ) -> tuple[str, ResourceToken]:
        """Create a new resource token.

        Args:
            resource_id: Resource identifier this token is scoped to
            resource_pk: Authoritative ``resources.id`` UUID (Issue #390
                Phase 2). Keyword-only so every caller is forced to resolve
                it — the ``before_insert`` event listener on ResourceToken
                rejects inserts with resource_id but no resource_pk, so a
                forgotten value surfaces as a hard error at test time.
            workspace_id: Owning workspace UUID (Issue #390 Phase 2).
                Keyword-only for the same reason; populates the Phase 1
                shadow column that Phase C (#325) will tighten to NOT NULL.
            description: Human-readable description
            quota_events_per_hour: Event ingestion quota (default: 1000/hour)
            created_by: User ID who created this token

        Returns:
            Plaintext token (only shown once)

        Raises:
            ValueError: If resource_id is invalid
        """
        # Validate resource_id format
        if not resource_id or len(resource_id) > 255:
            raise ValueError(f"Invalid resource_id: {resource_id}")

        # Generate new token
        token = self._generate_token()
        token_hash = self._hash_token(token)

        # Create database record. ``resource_pk`` + ``workspace_id`` are
        # populated from the caller's resolved values so the event listener
        # invariant (models/resource.py) passes.
        new_token = ResourceToken(
            resource_pk=resource_pk,
            resource_id=resource_id,
            workspace_id=workspace_id,
            token_hash=token_hash,
            description=description,
            quota_events_per_hour=quota_events_per_hour,
            created_by=created_by,
        )

        self.db.add(new_token)
        await self.db.flush()

        logger.info(
            "resource_token_created",
            token_id=new_token.id,
            public_id=new_token.public_id,
            resource_id=resource_id,
            resource_pk=str(resource_pk),
            workspace_id=str(workspace_id),
            quota=quota_events_per_hour,
            created_by=created_by,
        )

        return token, new_token

    async def verify_token(self, token: str, resource_id: str) -> ResourceToken | None:
        """Verify resource token and return token record.

        Issue #390 Phase 2: JOIN on ``Resource`` via ``resource_pk`` so
        the auth query is workspace-scoped by construction. Without this
        join, a still-valid token from a soft-deleted workspace whose
        slug has been reused in a different live workspace could
        authenticate for the new workspace's resource — the same
        CWE-639 leak that the read-path hardening closes, but on the
        auth boundary. Legacy tokens with ``resource_pk IS NULL`` are
        rejected here (backfilled by migration b01 before this code
        ships; no legacy NULL tokens are expected in production).

        Args:
            token: Plaintext token to verify
            resource_id: Expected resource_id (must match token's scope)

        Returns:
            ResourceToken record if valid, None otherwise
        """
        token_hash = self._hash_token(token)

        # Query token with resource_id validation via Resource JOIN — the
        # JOIN pins workspace even when the slug is reused across
        # workspaces, because each token's resource_pk FK identifies
        # exactly one Resource (and therefore one workspace).
        result = await self.db.execute(
            select(ResourceToken)
            .join(Resource, Resource.id == ResourceToken.resource_pk)
            .where(
                and_(
                    ResourceToken.token_hash == token_hash,
                    Resource.resource_id == resource_id,
                    ResourceToken.is_active == True,  # noqa: E712
                )
            )
        )
        token_record = result.scalar_one_or_none()

        if not token_record:
            logger.debug(  # Changed from warning to debug (security - don't log token prefix)
                "invalid_resource_token_attempt",
                resource_id=resource_id,
            )
            return None

        # Update last_used_at
        from utils.datetime import utcnow

        token_record.last_used_at = utcnow()
        await self.db.flush()

        logger.debug(
            "resource_token_verified",
            resource_id=resource_id,
            token_id=token_record.id,
            public_id=token_record.public_id,
        )

        return token_record

    async def revoke_token(self, token_id: int) -> None:
        """Revoke a resource token.

        Args:
            token_id: Token ID to revoke

        Raises:
            ValueError: If token not found
        """
        token = await self.db.get(ResourceToken, token_id)

        if not token:
            raise ValueError(f"Resource token {token_id} not found")

        token.is_active = False
        await self.db.flush()

        logger.info(
            "resource_token_revoked",
            token_id=token_id,
            public_id=token.public_id,
            resource_id=token.resource_id,
        )

    async def revoke_tokens_for_resource(
        self,
        workspace_id: UUID,
        resource_id: str,
        *,
        created_by: str | None = None,
    ) -> int:
        """Revoke the active tokens of one workspace's resource (#1877).

        The chokepoint for the "context deleted" / "context re-slugged"
        auto-revoke: scoped by :func:`resource_token_scope`, never by the bare
        slug, so a same-slug resource of another workspace keeps its tokens.

        Args:
            workspace_id: Workspace that owns the resource
            resource_id: Resource slug within that workspace
            created_by: Only revoke tokens minted by this user (None = all)

        Returns:
            Number of tokens revoked
        """
        resource_pk = await resolve_resource_pk(self.db, workspace_id, resource_id)

        query = select(ResourceToken).where(
            resource_token_scope(workspace_id, resource_id, resource_pk),
            ResourceToken.is_active == True,  # noqa: E712
        )
        if created_by is not None:
            query = query.where(ResourceToken.created_by == created_by)

        tokens = list((await self.db.execute(query)).scalars().all())
        for token in tokens:
            await self.revoke_token(token.id)
        return len(tokens)

    async def list_tokens(
        self,
        resource_id: str | None = None,
        created_by: str | None = None,
        include_revoked: bool = True,
        limit: int | None = None,
        offset: int = 0,
        *,
        workspace_id: UUID | None = None,
        resource_pk: UUID | None = None,
    ) -> list[ResourceToken]:
        """List resource tokens with optional filters and pagination.

        Issue #264: Added pagination support and created_by filter.
        #1863: ``workspace_id`` / ``resource_pk`` scope the list to one
        workspace's tokens (and one resource by its ``resources.id``) — a bare
        slug is shared across workspaces, so the slug filter alone would mix
        same-slug tokens of another workspace in. #1877: that call is refused.
        #1919: ``workspace_id`` is judged by :func:`workspace_token_scope`
        (the ``resources`` row, shadow column only for a legacy row), the
        same population the cap and the ceiling count — not by the shadow
        column alone, which hid a token the cap counted.

        Args:
            resource_id: Optional resource_id (slug) filter
            created_by: Optional created_by filter (for user-specific tokens)
            include_revoked: Include revoked tokens (default: True)
            limit: Maximum number of tokens to return (None = all)
            offset: Starting offset for pagination (default: 0)
            workspace_id: Optional workspace filter (:func:`workspace_token_scope`)
            resource_pk: Optional ``resource_tokens.resource_pk`` filter

        Returns:
            List of ResourceToken entities

        Raises:
            ValueError: ``resource_id`` without ``workspace_id`` or ``resource_pk``
        """
        _require_scope_for_slug(resource_id, workspace_id, resource_pk)

        query = (
            workspace_tokens(workspace_id, ResourceToken)
            if workspace_id is not None
            else select(ResourceToken)
        ).order_by(ResourceToken.created_at.desc())

        if resource_id:
            query = query.where(ResourceToken.resource_id == resource_id)

        if created_by:
            query = query.where(ResourceToken.created_by == created_by)

        if resource_pk is not None:
            query = query.where(ResourceToken.resource_pk == resource_pk)

        if not include_revoked:
            query = query.where(ResourceToken.is_active == True)  # noqa: E712

        if limit is not None:
            query = query.offset(offset).limit(limit)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_tokens(
        self,
        resource_id: str | None = None,
        created_by: str | None = None,
        include_revoked: bool = True,
        *,
        workspace_id: UUID | None = None,
        resource_pk: UUID | None = None,
    ) -> int:
        """Count resource tokens matching filters.

        Issue #264: For pagination total count. Same filters as ``list_tokens``.

        Args:
            resource_id: Optional resource_id (slug) filter
            created_by: Optional created_by filter
            include_revoked: Include revoked tokens (default: True)
            workspace_id: Optional workspace filter (:func:`workspace_token_scope`)
            resource_pk: Optional ``resource_tokens.resource_pk`` filter

        Returns:
            Total count of matching tokens

        Raises:
            ValueError: ``resource_id`` without ``workspace_id`` or ``resource_pk``
        """
        _require_scope_for_slug(resource_id, workspace_id, resource_pk)

        query = (
            workspace_tokens(workspace_id, func.count(ResourceToken.id))
            if workspace_id is not None
            else select(func.count(ResourceToken.id))
        )

        conditions = []
        if resource_id:
            conditions.append(ResourceToken.resource_id == resource_id)
        if created_by:
            conditions.append(ResourceToken.created_by == created_by)
        if resource_pk is not None:
            conditions.append(ResourceToken.resource_pk == resource_pk)
        if not include_revoked:
            conditions.append(ResourceToken.is_active == True)  # noqa: E712

        if conditions:
            query = query.where(and_(*conditions))

        result = await self.db.execute(query)
        return result.scalar() or 0
