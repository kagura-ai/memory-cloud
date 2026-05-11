"""Permission Service for RBAC (Role-Based Access Control).

Issue #115 Phase B-2: Workspace-level Multi-tenancy

Manages access control at workspace and context levels.
"""

from typing import NewType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, ContextMember, WorkspaceMember
from services.workspace_service import WorkspaceService
from utils.exceptions import AuthorizationError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

# Brand types for authz identities (Issue #383).
# NewType is zero-cost at runtime but prevents str/str argument swaps at pyright.
# Mint at authz function boundaries; do not leak these into repository layers.
CallerId = NewType("CallerId", str)
MemoryAuthorId = NewType("MemoryAuthorId", str)

# Role hierarchy weights (higher = more privilege)
ORG_ROLE_WEIGHTS = {
    "owner": 4,
    "admin": 3,
    "member": 2,
    "viewer": 1,
}

CONTEXT_ROLE_WEIGHTS = {
    "owner": 3,
    "editor": 2,
    "viewer": 1,
}


class PermissionService:
    """Service for permission checking and access control.

    Implements 2-layer RBAC:
    - Workspace-level: owner/admin/member/viewer
    - Context-level: owner/editor/viewer

    Access hierarchy:
    - Workspace owner/admin: Bypass context checks (full access to all contexts)
    - Workspace viewer: Read-only access to all contexts
    - Workspace member: Requires explicit context membership

    Attributes:
        db: AsyncSession for database access
        workspace_service: WorkspaceService instance
    """

    def __init__(self, db: AsyncSession):
        """Initialize permission service.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        self.workspace_service = WorkspaceService(db)

    # ========================================================================
    # Workspace-level Permissions
    # ========================================================================

    async def check_workspace_access(
        self,
        user_id: str,
        workspace_id: UUID,
        required_role: str = "member",
    ) -> WorkspaceMember:
        """Check if user has required workspace access.

        Issue #276: Also verifies workspace is not deleted (soft-delete check).

        Args:
            user_id: User ID
            workspace_id: Workspace ID
            required_role: Minimum required role (owner/admin/member/viewer)

        Returns:
            Workspace membership

        Raises:
            AuthorizationError: 403 if user doesn't have required access or
                workspace is deleted. The fixed ``"Insufficient permissions"``
                message uniformly hides which sub-reason fired (CWE-639
                workspace enumeration defense). ``exc.reason`` (private
                attribute, never serialized to clients) carries one of
                ``"workspace_deleted"`` / ``"not_a_member"`` / ``"role_too_low"``
                for structured-log classification.
        """
        # Issue #276: Verify workspace exists and is not deleted
        from sqlalchemy import select

        from models.auth import Workspace

        workspace_result = await self.db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            raise AuthorizationError(
                "Insufficient permissions",
                reason="workspace_deleted",
            )

        # Get membership
        member = await self.workspace_service.get_member(
            workspace_id,
            user_id,
            raise_if_not_found=False,
        )

        if not member:
            raise AuthorizationError(
                "Insufficient permissions",
                reason="not_a_member",
            )

        # Check role hierarchy
        user_weight = ORG_ROLE_WEIGHTS.get(member.role, 0)
        required_weight = ORG_ROLE_WEIGHTS.get(required_role, 0)

        if user_weight < required_weight:
            raise AuthorizationError(
                "Insufficient permissions",
                reason="role_too_low",
            )

        return member

    async def resolve_resource_by_slug(
        self,
        user_id: str,
        resource_id: str,
        *,
        required_role: str = "member",
    ) -> Context:
        """Resolve a ``resource_id`` slug to the Context the caller can access.

        Issue #326: shared helper for per-resource API endpoints that receive
        a slug in the URL (e.g. ``GET /resources/{resource_id}/indexer-status``)
        and need to check cross-tenant access before reading any resource state.

        Rationale for preserving slugs (Issue #327):
            External consumers (CI pipelines, MCP clients, Resource Tokens)
            address resources by human-readable slugs. Forcing UUID migration
            on every external caller would break existing integrations with no
            user-facing benefit. This shim resolves the incoming slug to an
            authorized ``Context``, keeping external API contracts stable
            while callers that need internal identifiers can obtain them from
            the returned context or related joins as appropriate.

        Contract:
            - Never trusts ``User.current_workspace_id`` — a mutable UI
              preference, unsuitable for authorization (same rationale as
              ``resource_ingest._enforce_workspace_membership``).
            - Enumerates every active Context with this slug. Under the #322
              global partial UNIQUE index on ``contexts.resource_id``, the
              candidate count is at most one for live rows, so the loop is
              O(1) in practice.
            - Delegates final authorization to ``check_workspace_access`` so
              the workspace-deleted / non-member / role-too-low paths surface
              uniformly.
            - Returns **404** on "not found OR not accessible" — never 403.
              A 403 would leak existence across workspace boundaries
              (CWE-639 / OWASP A01). The detail string is intentionally
              indistinguishable from a completely unknown slug.

        Args:
            user_id: Authenticated principal's user ID.
            resource_id: Resource slug (URL path segment).
            required_role: Minimum workspace role to accept. Defaults to
                ``member`` — callers needing stricter access (e.g. writes)
                can pass ``admin``/``owner``.

        Returns:
            The ``Context`` row owning this resource slug that the caller
            has access to.

        Raises:
            NotFoundException: slug does not exist, or exists only in
                workspaces the caller cannot access. The message is the
                uniform ``"Resource not found"`` to keep the surface
                indistinguishable from a completely unknown slug.
        """
        candidates = (
            (
                await self.db.execute(
                    select(Context).where(
                        Context.resource_id == resource_id,
                        Context.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        for ctx in candidates:
            try:
                await self.check_workspace_access(
                    user_id=user_id,
                    workspace_id=ctx.workspace_id,
                    required_role=required_role,
                )
                return ctx
            except (AuthorizationError, NotFoundException):
                # Not this one — keep looking. Under a96's global UNIQUE there
                # will be at most one live candidate, but we loop defensively
                # in case the index is ever dropped for an operational reason.
                continue

        raise NotFoundException("Resource")

    async def resolve_context_for_workspace_read(
        self,
        user_id: str,
        context_id: UUID,
        *,
        required_role: str = "viewer",
    ) -> Context:
        """Resolve a ``context_id`` to a Context the caller can read, with uniform 404.

        Issue #383: centralizes the "look up a Context + verify workspace
        membership" chokepoint for UUID-addressed context reads. Returns a
        unified 404 on either "context does not exist" or "caller is not a
        workspace member" so cross-workspace existence does not leak
        (CWE-639 / OWASP A01 uniform disclosure — same principle as
        ``resolve_resource_by_slug`` for slug-addressed paths).

        Issue #398: default lowered from ``member`` to ``viewer`` — this is
        a *read* helper and viewers must be able to read shared-context
        graphs/stats. Previously a viewer hitting ``/graph/stats`` for a
        shared context they could otherwise see surfaced as
        ``404 Context not found`` because the membership gate rejected
        viewer before any per-role logic ran.

        Args:
            user_id: Authenticated principal.
            context_id: UUID of the context to resolve.
            required_role: Minimum workspace role to accept. Defaults to
                ``viewer`` — suitable for read endpoints like ``/graph/*``
                and ``/memory/stats``. Writers should pass ``admin`` or
                ``owner``.

        Returns:
            The ``Context`` row for ``context_id`` if the caller has the
            required workspace role.

        Raises:
            NotFoundException: context does not exist, or the caller is not
                a member of its owning workspace with the required role.
                The uniform 404 hides whether the context exists in another
                workspace (CWE-639 / OWASP A01).
        """
        context_result = await self.db.execute(
            select(Context).where(
                Context.id == context_id,
                Context.deleted_at.is_(None),
            )
        )
        context = context_result.scalar_one_or_none()
        if context is None:
            logger.info(
                "context_read_denied",
                reason="not_found",
                context_id=str(context_id),
                user_id=user_id,
            )
            raise NotFoundException("Context", str(context_id))

        # Private context: creator-only. Enforce here so graph / other
        # UUID-addressed read endpoints match ``can_access_memory`` semantics
        # (private → only the creator sees anything) instead of returning an
        # empty 200 that leaks "a private context with this ID exists in
        # this workspace, and you're not its owner".
        if context.is_private and context.created_by != user_id:
            logger.warning(
                "context_read_denied",
                reason="private_non_creator",
                context_id=str(context_id),
                context_workspace_id=str(context.workspace_id),
                user_id=user_id,
            )
            raise NotFoundException("Context", str(context_id))

        try:
            workspace_member = await self.check_workspace_access(
                user_id=user_id,
                workspace_id=context.workspace_id,
                required_role=required_role,
            )
        except AuthorizationError as exc:
            reason = exc.reason or "workspace_access_denied"
            logger.warning(
                "context_read_denied",
                reason=reason,
                context_id=str(context_id),
                context_workspace_id=str(context.workspace_id),
                user_id=user_id,
            )
            raise NotFoundException("Context", str(context_id)) from None

        # Apply the same allowed_context_ids semantics that get_accessible_contexts
        # and check_context_access enforce for the lower-privilege roles. Without
        # this, a restricted member/viewer could read /graph/stats and other
        # UUID-addressed endpoints for contexts they cannot list. Workspace
        # owner/admin bypass these restrictions by design (line 427-429).
        #
        # Per Migration 042 the NULL/[] semantics differ by role:
        #   - member, allowed_context_ids IS NULL → suspended (no access)
        #   - viewer, allowed_context_ids IS NULL → no restriction (all)
        #   - either, allowed_context_ids = [<ids>] → whitelist enforced
        #   - either, allowed_context_ids = []     → explicit no access
        if workspace_member.role == "member" and workspace_member.allowed_context_ids is None:
            logger.warning(
                "context_read_denied",
                reason="member_suspended",
                context_id=str(context_id),
                context_workspace_id=str(context.workspace_id),
                user_id=user_id,
            )
            raise NotFoundException("Context", str(context_id))

        if (
            workspace_member.role in ("member", "viewer")
            and workspace_member.allowed_context_ids is not None
            and context_id not in workspace_member.allowed_context_ids
        ):
            logger.warning(
                "context_read_denied",
                reason="not_in_whitelist",
                context_id=str(context_id),
                context_workspace_id=str(context.workspace_id),
                user_id=user_id,
            )
            raise NotFoundException("Context", str(context_id))

        return context

    async def check_workspace_owner(self, user_id: str, workspace_id: UUID) -> WorkspaceMember:
        """Check if user is workspace owner.

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            Workspace membership

        Raises:
            AuthorizationError: 403 if not owner
        """
        return await self.check_workspace_access(user_id, workspace_id, required_role="owner")

    async def check_workspace_admin(self, user_id: str, workspace_id: UUID) -> WorkspaceMember:
        """Check if user is workspace admin or owner.

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            Workspace membership

        Raises:
            AuthorizationError: 403 if not admin/owner
        """
        return await self.check_workspace_access(user_id, workspace_id, required_role="admin")

    async def is_workspace_member(self, user_id: str, workspace_id: UUID) -> bool:
        """Check if user is a member of workspace (any role).

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            True if member, False otherwise
        """
        member = await self.workspace_service.get_member(
            workspace_id,
            user_id,
            raise_if_not_found=False,
        )
        return member is not None

    # ========================================================================
    # Context-level Permissions
    # ========================================================================

    async def check_context_access(
        self,
        user_id: str,
        context_id: UUID,
        required_role: str = "viewer",
    ) -> tuple[Context, str]:
        """Check if user has required context access.

        Access hierarchy:
        1. Workspace owner/admin → Full access (bypass context membership)
        2. Workspace viewer → Read-only access (bypass context membership)
        3. Workspace member → Requires explicit context membership

        Args:
            user_id: User ID
            context_id: Context ID
            required_role: Minimum required role (owner/editor/viewer)

        Returns:
            Tuple of (Context, effective_role)

        Raises:
            NotFoundException: 404 if the context does not exist.
            AuthorizationError: 403 if user doesn't have required access.
                Message is the uniform ``"Insufficient permissions"`` —
                per-deny-path messages were intentionally stripped to remove
                a CWE-639 enumeration vector. Use structured logs / sentry
                breadcrumbs for the deny reason.
        """
        from sqlalchemy import select

        # Get context
        stmt = select(Context).where(
            Context.id == context_id,
            Context.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        context = result.scalar_one_or_none()

        if not context:
            raise NotFoundException("Context")

        # Issue #165: Private context check (creator-only access)
        if context.is_private:
            if context.created_by == user_id:
                # Creator has full access to their private context
                return context, "owner"
            else:
                # Others cannot access private contexts
                raise AuthorizationError("Insufficient permissions")

        # Shared context: Check workspace membership
        workspace_member = await self.workspace_service.get_member(
            context.workspace_id,
            user_id,
            raise_if_not_found=False,
        )

        if not workspace_member:
            raise AuthorizationError("Insufficient permissions")

        # Workspace owner/admin → bypass context checks (full access)
        # Note: allowed_context_ids is ignored for owner/admin
        if workspace_member.role in ("owner", "admin"):
            effective_role = "owner"  # Treat as context owner
            return context, effective_role

        # Issue #234: Check allowed_context_ids whitelist for member/viewer
        if workspace_member.allowed_context_ids is not None:
            if context_id not in workspace_member.allowed_context_ids:
                raise AuthorizationError("Insufficient permissions")

        # Workspace viewer → read-only access (bypass context checks)
        if workspace_member.role == "viewer":
            # Check if viewer role meets requirement
            if required_role != "viewer":
                raise AuthorizationError("Insufficient permissions")
            return context, "viewer"

        # Workspace member → requires explicit context membership
        stmt = select(ContextMember).where(
            ContextMember.context_id == context_id,
            ContextMember.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        context_member = result.scalar_one_or_none()

        if not context_member:
            raise AuthorizationError("Insufficient permissions")

        # Check context role hierarchy
        user_weight = CONTEXT_ROLE_WEIGHTS.get(context_member.role, 0)
        required_weight = CONTEXT_ROLE_WEIGHTS.get(required_role, 0)

        if user_weight < required_weight:
            raise AuthorizationError("Insufficient permissions")

        return context, context_member.role

    async def check_context_write(self, user_id: str, context_id: UUID) -> Context:
        """Check if user can write to context (editor or owner).

        Args:
            user_id: User ID
            context_id: Context ID

        Returns:
            Context

        Raises:
            NotFoundException: 404 if the context does not exist.
            AuthorizationError: 403 if no write access.
        """
        context, role = await self.check_context_access(
            user_id,
            context_id,
            required_role="editor",
        )
        return context

    async def check_context_owner(self, user_id: str, context_id: UUID) -> Context:
        """Check if user is context owner.

        Args:
            user_id: User ID
            context_id: Context ID

        Returns:
            Context

        Raises:
            NotFoundException: 404 if the context does not exist.
            AuthorizationError: 403 if not context owner.
        """
        context, role = await self.check_context_access(
            user_id,
            context_id,
            required_role="owner",
        )
        return context

    # ========================================================================
    # Utility Methods
    # ========================================================================

    async def count_context_owners(self, context_id: UUID) -> int:
        """Count explicit ContextMember rows with role='owner' for a context.

        Used by update_context_member_role to prevent demoting the last
        remaining explicit owner. remove_context_member rejects owner removal
        unconditionally via a separate role-check, so this helper is
        demotion-only today — re-wire into the remove flow if we ever relax
        that to "remove allowed when >1 owners exist".

        Workspace-level owner/admin bypass (check_context_access:327-329) is
        not counted here — this reflects the real governance anchor stored in
        the ContextMember table.

        Args:
            context_id: Context ID

        Returns:
            Number of ContextMember rows with role='owner'
        """
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(ContextMember)
            .where(
                ContextMember.context_id == context_id,
                ContextMember.role == "owner",
            )
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one() or 0)

    async def get_accessible_contexts(
        self,
        user_id: str,
        workspace_id: UUID,
    ) -> list[Context]:
        """Get all contexts user can access in workspace.

        Issue #234: Respects allowed_context_ids whitelist for member/viewer.
        Issue #398: viewer must reach the viewer branch below — gating at
        ``required_role="member"`` made that branch unreachable and silently
        hid every shared context from viewers (UX-visible: contexts list
        was empty for viewer even when shared contexts existed).

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            List of accessible contexts
        """
        from sqlalchemy import select

        # Check workspace membership (viewer is the floor — the per-role
        # branches below decide what each role can actually see).
        workspace_member = await self.check_workspace_access(
            user_id, workspace_id, required_role="viewer"
        )

        # Private contexts are creator-only across every workspace role
        # (matches check_context_access:401-410 — even an owner/admin gets 403
        # when trying to open another user's private context). Filter the
        # listing to non-private contexts plus the caller's own private ones
        # so the list shape matches the per-context access check; otherwise
        # the listing leaks the existence of private contexts that 403 on
        # click-through.
        privacy_filter = (Context.is_private.is_(False)) | (Context.created_by == user_id)

        # Workspace owner/admin → all accessible contexts (ignore
        # allowed_context_ids; privacy still applies).
        if workspace_member.role in ("owner", "admin"):
            stmt = (
                select(Context)
                .where(
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                    privacy_filter,
                )
                .order_by(Context.created_at.desc())
            )
            result = await self.db.execute(stmt)
            return list(result.scalars().all())

        # Issue #234: Workspace viewer with allowed_context_ids restriction
        if workspace_member.role == "viewer":
            if workspace_member.allowed_context_ids is not None:
                # Only whitelisted contexts
                if not workspace_member.allowed_context_ids:
                    return []  # Empty whitelist = no access
                stmt = (
                    select(Context)
                    .where(
                        Context.workspace_id == workspace_id,
                        Context.deleted_at.is_(None),
                        Context.id.in_(workspace_member.allowed_context_ids),
                        privacy_filter,
                    )
                    .order_by(Context.created_at.desc())
                )
            else:
                # No restriction → all shared contexts (+ caller's own private)
                stmt = (
                    select(Context)
                    .where(
                        Context.workspace_id == workspace_id,
                        Context.deleted_at.is_(None),
                        privacy_filter,
                    )
                    .order_by(Context.created_at.desc())
                )
            result = await self.db.execute(stmt)
            return list(result.scalars().all())

        # Workspace member → contexts based on allowed_context_ids
        # Migration 042: NULL = suspended (not configured)
        if workspace_member.allowed_context_ids is None:
            # NULL = suspended state (context not configured)
            # User must configure via Context Access dialog
            return []

        # Empty array = no access
        if not workspace_member.allowed_context_ids:
            return []

        # Show only whitelisted contexts (filtered to non-private + own private)
        stmt = (
            select(Context)
            .where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
                Context.id.in_(workspace_member.allowed_context_ids),
                privacy_filter,
            )
            .order_by(Context.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def can_manage_context(self, user_id: str, context_id: UUID) -> bool:
        """Check if user can manage context (owner-level access).

        Args:
            user_id: User ID
            context_id: Context ID

        Returns:
            True if can manage, False otherwise
        """
        try:
            await self.check_context_owner(user_id, context_id)
            return True
        except (AuthorizationError, NotFoundException):
            return False

    async def can_write_context(self, user_id: str, context_id: UUID) -> bool:
        """Check if user can write to context.

        Args:
            user_id: User ID
            context_id: Context ID

        Returns:
            True if can write, False otherwise
        """
        try:
            await self.check_context_write(user_id, context_id)
            return True
        except (AuthorizationError, NotFoundException):
            return False

    # ========================================================================
    # Memory Access Control (Issue #XXX: Team Collaboration)
    # ========================================================================

    async def can_access_memory(
        self,
        *,
        user_id: CallerId,
        memory_user_id: MemoryAuthorId,
        workspace_id: UUID,
        context_id: UUID,
    ) -> bool:
        """Check if user can access a memory based on workspace membership and context privacy.

        Access rules:
        1. Private context (is_private=true): Only creator can access
        2. Shared context (is_private=false): Workspace members can access

        Args:
            user_id: Requesting user ID
            memory_user_id: Memory creator's user ID
            workspace_id: Workspace ID
            context_id: Context ID

        Returns:
            True if user can access, False otherwise
        """
        # Owner can always access their own memories
        if user_id == memory_user_id:
            return True

        # Check if context is shared
        from services.context_service import ContextService

        context_service = ContextService(self.db)
        is_shared = await context_service.is_context_shared(context_id)

        if not is_shared:
            # Private context: only creator can access
            return False

        # Shared context: check workspace membership
        return await self.is_workspace_member(user_id, workspace_id)
