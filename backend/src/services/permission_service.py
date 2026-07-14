"""Permission Service for RBAC (Role-Based Access Control).

Issue #115 Phase B-2: Workspace-level Multi-tenancy

Manages access control at workspace and context levels.
"""

from typing import NewType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import (
    CONTEXT_ROLE_WEIGHTS,
    ContextRole,
    WorkspaceRole,
)
from auth.workspace_roles import (
    WORKSPACE_ROLE_WEIGHTS as ORG_ROLE_WEIGHTS,
)
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

# Re-exported from auth.workspace_roles for backward compatibility.
# New code should import directly from auth.workspace_roles.
# ORG_ROLE_WEIGHTS keeps the old name (used by external callers).
__all__ = [
    "ContextRole",
    "WorkspaceRole",
    "ORG_ROLE_WEIGHTS",
    "CONTEXT_ROLE_WEIGHTS",
    "CallerId",
    "MemoryAuthorId",
    "PermissionService",
]


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
        required_role: WorkspaceRole | str = WorkspaceRole.MEMBER,
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
        # Normalise at the boundary: accept str for backward compat
        required = (
            required_role
            if isinstance(required_role, WorkspaceRole)
            else WorkspaceRole(required_role)
        )

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
        required_weight = ORG_ROLE_WEIGHTS.get(required, 0)

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
        required_role: WorkspaceRole | str = WorkspaceRole.MEMBER,
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
        required_role: WorkspaceRole | str = WorkspaceRole.VIEWER,
        key_workspace_id: UUID | None = None,
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
            key_workspace_id: Issue #963. The caller's *pure* API-key workspace
                scope — the workspace the API key is itself scoped to, or
                ``None`` when the request is not authenticated with a
                workspace-scoped key. This is NOT the conflated/effective
                workspace: MCP must pass ``api_key_workspace_id`` (the transport
                exposes it via a per-request contextvar), NOT
                ``session.workspace_id`` (which becomes the user's *current*
                workspace for OAuth2/session/global-key auth and would
                over-confine those callers); REST passes
                ``user["api_key_workspace_id"]``. When supplied, confine the
                read to it: a key minted for workspace A must not reach a context
                owned by B even when the user is also a member of B (membership
                is necessary but not sufficient). Pass ``None`` for non-key-scoped
                callers — they carry no key scope (#889) and are governed by
                membership alone. A mismatch raises the same uniform 404 as every
                other deny.

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

        # Issue #963: confine a workspace-scoped caller (API key carrying
        # key_workspace_id) to its own workspace. Checked here — right after the
        # Context is loaded and BEFORE the membership / whitelist lookups —
        # because the outcome is a uniform 404 regardless, so short-circuiting a
        # cross-workspace key avoids an unnecessary workspace-membership query
        # (and the DB-load amplification it invites under cross-workspace
        # probing). Skip when key_workspace_id is None (non-key-scoped auth
        # carries no key scope, #889; governed by membership alone).
        if key_workspace_id is not None and context.workspace_id != key_workspace_id:
            logger.warning(
                "context_read_denied",
                reason="key_workspace_mismatch",
                context_id=str(context_id),
                context_workspace_id=str(context.workspace_id),
                key_workspace_id=str(key_workspace_id),
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
        if (
            workspace_member.role == WorkspaceRole.MEMBER
            and workspace_member.allowed_context_ids is None
        ):
            logger.warning(
                "context_read_denied",
                reason="member_suspended",
                context_id=str(context_id),
                context_workspace_id=str(context.workspace_id),
                user_id=user_id,
            )
            raise NotFoundException("Context", str(context_id))

        if (
            workspace_member.role in (WorkspaceRole.MEMBER, WorkspaceRole.VIEWER)
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

        # Issue #1275 (RFC-0002 P0-2): purely subtractive agent-binding
        # intersection — applied strictly AFTER every existing gate passed, so
        # it can only remove access. No-op for non-agent credentials (scope is
        # None). ``required_role`` semantics: viewer = read helper default;
        # writers pass admin/owner. WorkspaceRole is a StrEnum, so the
        # comparison holds for both str and enum callers.
        access = "read" if required_role == WorkspaceRole.VIEWER else "write"
        await self._apply_agent_binding_filter(context_id, access=access, user_id=user_id)

        return context

    async def _apply_agent_binding_filter(
        self, context_id: UUID, *, access: str, user_id: str
    ) -> None:
        """Deny with the uniform 404 when the agent binding subtracts access.

        Reads the per-request agent scope (#1275, set at API-key verify for
        agent-bound keys only) and evaluates the binding intersection. Both
        read and write denies surface as ``context_not_found`` — confirming
        the context exists but is write-forbidden would leak the CWE-639
        signal the uniform-404 posture removes. Shadow-mode violations are
        logged and allowed (``would_deny``, the enforcement ramp).
        """
        from auth.agent_scope import get_agent_scope

        scope = get_agent_scope()
        if scope is None:
            return

        from services.agent_binding_service import AgentBindingService

        allowed, decision = await AgentBindingService(self.db).evaluate_context_access(
            scope, context_id, access
        )
        if not allowed:
            logger.warning(
                "context_access_denied",
                reason="agent_binding",
                decision=decision,
                access=access,
                context_id=str(context_id),
                agent_id=str(scope.agent_id),
                user_id=user_id,
            )
            raise NotFoundException("Context", str(context_id))

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
        return await self.check_workspace_access(
            user_id, workspace_id, required_role=WorkspaceRole.OWNER
        )

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
        return await self.check_workspace_access(
            user_id, workspace_id, required_role=WorkspaceRole.ADMIN
        )

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
        required_role: ContextRole | str = ContextRole.VIEWER,
    ) -> tuple[Context, ContextRole]:
        """Check if user has required context access.

        Access hierarchy:
        1. Workspace owner/admin → Full access (bypass context membership)
        2. Workspace viewer → Read-only access (bypass context membership)
        3. Workspace member → Requires explicit context membership

        Issue #1275 (RFC-0002 P0-2): after the RBAC decision allows, the
        purely subtractive agent-binding intersection is applied for
        agent-bound credentials (no-op otherwise). A binding deny — read OR
        write — surfaces as the uniform ``context_not_found`` 404 per the
        design contract, not a 403 that would confirm existence.

        Args:
            user_id: User ID
            context_id: Context ID
            required_role: Minimum required role (owner/editor/viewer)

        Returns:
            Tuple of (Context, effective_role). The effective_role is always a
            ``ContextRole``: workspace owner/admin map to ``ContextRole.OWNER``,
            workspace viewer maps to ``ContextRole.VIEWER``, and an explicit
            ContextMember row keeps its own role.

        Raises:
            NotFoundException: 404 if the context does not exist, or the
                agent binding subtracts access (#1275).
            AuthorizationError: 403 if user doesn't have required access.
                Message is the uniform ``"Insufficient permissions"`` —
                per-deny-path messages were intentionally stripped to remove
                a CWE-639 enumeration vector. Use structured logs / sentry
                breadcrumbs for the deny reason.
        """
        context, effective_role = await self._check_context_access_rbac(
            user_id, context_id, required_role
        )

        # ContextRole is a str-comparable enum here only via its value set;
        # normalize the same way the RBAC core does.
        required = (
            required_role if isinstance(required_role, ContextRole) else ContextRole(required_role)
        )
        access = "read" if required == ContextRole.VIEWER else "write"
        await self._apply_agent_binding_filter(context_id, access=access, user_id=user_id)

        return context, effective_role

    async def _check_context_access_rbac(
        self,
        user_id: str,
        context_id: UUID,
        required_role: ContextRole | str = ContextRole.VIEWER,
    ) -> tuple[Context, ContextRole]:
        """The pre-#1275 RBAC decision core of ``check_context_access``.

        Kept verbatim so the binding intersection is a strict wrapper — the
        backward-compat matrix's "byte-for-byte unchanged without agent_id"
        guarantee reduces to "this method did not change".
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
        # Normalise at the boundary: accept str for backward compat
        required = (
            required_role if isinstance(required_role, ContextRole) else ContextRole(required_role)
        )

        if context.is_private:
            if context.created_by == user_id:
                # Creator has full access to their private context
                return context, ContextRole.OWNER
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
        if workspace_member.role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
            # Treat as context owner
            return context, ContextRole.OWNER

        # Issue #234: Check allowed_context_ids whitelist for member/viewer
        if workspace_member.allowed_context_ids is not None:
            if context_id not in workspace_member.allowed_context_ids:
                raise AuthorizationError("Insufficient permissions")

        # Workspace viewer → read-only access (bypass context checks)
        if workspace_member.role == WorkspaceRole.VIEWER:
            # Check if viewer role meets requirement
            if required != ContextRole.VIEWER:
                raise AuthorizationError("Insufficient permissions")
            return context, ContextRole.VIEWER

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
        required_weight = CONTEXT_ROLE_WEIGHTS.get(required, 0)

        if user_weight < required_weight:
            raise AuthorizationError("Insufficient permissions")

        return context, ContextRole(context_member.role)

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
        context, _ = await self.check_context_access(
            user_id,
            context_id,
            required_role=ContextRole.EDITOR,
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
        context, _ = await self.check_context_access(
            user_id,
            context_id,
            required_role=ContextRole.OWNER,
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

        Workspace-level owner/admin bypass (handled inside
        ``check_context_access``) is not counted here — this reflects the
        real governance anchor stored in the ContextMember table.

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
                ContextMember.role == ContextRole.OWNER,
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

        Issue #1275 (RFC-0002 P0-2): for ``enforce``-mode agent credentials
        the enumeration surface is intersected with the agent's binding read
        set — a membership-driven listing must not disclose contexts the
        binding subtracts. ``shadow``-mode agents keep the full membership
        view (the enforcement ramp changes nothing observable).

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            List of accessible contexts
        """
        contexts = await self._get_accessible_contexts_rbac(user_id, workspace_id)

        from auth.agent_scope import get_agent_scope
        from models.agent import AGENT_ENFORCEMENT_SHADOW

        scope = get_agent_scope()
        if scope is None or not contexts:
            return contexts
        # shadow is the only mode that keeps the full membership view (the
        # enforcement ramp — would_deny is logged, not applied). enforce AND
        # any unexpected mode fall through to the intersection so an
        # unrecognized enforcement_mode fails CLOSED, matching
        # evaluate_context_access which denies on a non-shadow mode
        # (code-review: the two paths must not disagree).
        if scope.enforcement_mode == AGENT_ENFORCEMENT_SHADOW:
            return contexts

        from services.agent_binding_service import AgentBindingService

        readable = await AgentBindingService(self.db).readable_context_ids(scope.agent_id)
        return [c for c in contexts if c.id in readable]

    async def _get_accessible_contexts_rbac(
        self,
        user_id: str,
        workspace_id: UUID,
    ) -> list[Context]:
        """The pre-#1275 membership-driven listing (see get_accessible_contexts)."""
        from sqlalchemy import select

        # Check workspace membership (viewer is the floor — the per-role
        # branches below decide what each role can actually see).
        workspace_member = await self.check_workspace_access(
            user_id, workspace_id, required_role=WorkspaceRole.VIEWER
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
        if workspace_member.role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
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
        if workspace_member.role == WorkspaceRole.VIEWER:
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
        access: str = "read",
    ) -> bool:
        """Check if user can access a memory based on workspace membership and context privacy.

        Access rules:
        1. Private context (is_private=true): Only creator can access
        2. Shared context (is_private=false): Workspace members can access

        Issue #1275 (RFC-0002 P0-2): this is the memory-id-addressed access
        chokepoint (reference / update / forget-by-id / explore authorize the
        memory's *own* stored ``context_id`` here). After the base RBAC
        decision allows, the purely subtractive agent-binding intersection is
        applied for agent-bound credentials, so a binding-denied context is
        unreachable even by memory_id. Callers on a WRITE path (update /
        forget) MUST pass ``access="write"`` so a ``can_read``-only binding
        cannot be used to mutate. No-op for non-agent credentials.

        Args:
            user_id: Requesting user ID
            memory_user_id: Memory creator's user ID
            workspace_id: Workspace ID
            context_id: Context ID
            access: ``"read"`` (default) or ``"write"`` — the binding gate.

        Returns:
            True if user can access, False otherwise
        """

        async def _binding_ok() -> bool:
            # The subtractive agent-binding gate applies to EVERY caller,
            # including the memory owner (an agent bound-denied to a context
            # must not reach its own memories there via memory_id).
            from services.agent_binding_service import agent_binding_permits

            return await agent_binding_permits(self.db, context_id, access)

        # Owner can always access their own memories (RBAC), but the binding
        # still subtracts.
        if user_id == memory_user_id:
            return await _binding_ok()

        # Check if context is shared
        from services.context_service import ContextService

        context_service = ContextService(self.db)
        is_shared = await context_service.is_context_shared(context_id)

        if not is_shared:
            # Private context: only creator can access
            return False

        # Shared context: check workspace membership, then the binding.
        if not await self.is_workspace_member(user_id, workspace_id):
            return False
        return await _binding_ok()
