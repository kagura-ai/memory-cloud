"""Permission Service for RBAC (Role-Based Access Control).

Issue #115 Phase B-2: Workspace-level Multi-tenancy

Manages access control at workspace and context levels.
"""

from typing import NewType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import (
    ACCESS_READ,
    ACCESS_WRITE,
    CONTEXT_ROLE_WEIGHTS,
    Access,
    ContextRole,
    WorkspaceRole,
    access_for_context_role,
    access_for_workspace_role,
)
from auth.workspace_roles import (
    WORKSPACE_ROLE_WEIGHTS as ORG_ROLE_WEIGHTS,
)
from db.qdrant import chunk_points_supported
from models.auth import Context, ContextMember, Workspace, WorkspaceMember
from models.schemas import ContextSearchConfigUpdate
from services.capacity_lock import CapacityPolicy, ensure_not_capacity_locked
from services.identity_link_service import is_same_owner, owned_by
from services.plan_seats import ensure_context_writable, ensure_member_seated
from services.workspace_service import WorkspaceService
from utils.exceptions import AuthorizationError, NotFoundException, ValidationError
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

        # #1943: a member without a seat on the workspace's plan is refused —
        # after membership and role, so a non-member keeps the uniform refusal.
        await ensure_member_seated(self.db, workspace, member)

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
            - Returns **404** on "not found OR not accessible" — never 403,
              except a member without a seat on the plan (``FEAT-001``, #1943).
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
        capacity: CapacityPolicy,
        required_role: WorkspaceRole | str = WorkspaceRole.VIEWER,
        key_workspace_id: UUID | None = None,
        operation: str | None = None,
        binding_decision_out: list[str] | None = None,
    ) -> Context:
        """Resolve a ``context_id`` to a Context the caller can read, with uniform 404.

        #1948: ``capacity`` is required. With ``CapacityPolicy.BLOCKED`` the
        resolved context's workspace is checked against the capacity-over lock
        (#1941) *after* every authorization step, so the lock never answers for
        a context the caller could not reach anyway; ``ALLOWED`` skips it (the
        cleanup surface, and MCP handlers whose dispatcher already decided).

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
            FeatureNotAvailableError: the caller IS a member of the owning
                workspace but holds no seat on its plan (#1943, FEAT-001
                ``members``) — raised after membership is established, so it
                never answers for a context in someone else's workspace.
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
        if context.is_private and not await is_same_owner(self.db, user_id, context.created_by):
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
        access = access_for_workspace_role(required_role)
        decision = await self._apply_agent_binding_filter(
            context_id, access=access, user_id=user_id, operation=operation
        )
        # #1402: surface the evaluated binding decision (``allowed`` /
        # ``would_deny``) to callers that stamp it on their OWN success audit
        # row (agent bootstrap), without changing the return shape for the
        # other read callers. ``None`` when no agent scope applies.
        if binding_decision_out is not None and decision is not None:
            binding_decision_out.append(decision)

        # #1948: the capacity-over lock, after authorization (never an
        # existence oracle). Only the enum member ALLOWED skips the lock — a
        # stray value, even the string "allowed", fails closed. Membership was established above, so
        # the caller gets the full refusal (counts and cleanup URL).
        if capacity is not CapacityPolicy.ALLOWED:
            await ensure_not_capacity_locked(self.db, context.workspace_id)
            # #1943: a content write into a context without a writable slot.
            if access == ACCESS_WRITE:
                await ensure_context_writable(self.db, context)

        return context

    async def _apply_agent_binding_filter(
        self, context_id: UUID, *, access: Access, user_id: str, operation: str | None = None
    ) -> str | None:
        """Deny with the uniform 404 when the agent binding subtracts access.

        Reads the per-request agent scope (#1275, set at API-key verify for
        agent-bound keys only) and evaluates the binding intersection. Both
        read and write denies surface as ``context_not_found`` — confirming
        the context exists but is write-forbidden would leak the CWE-639
        signal the uniform-404 posture removes. Shadow-mode violations are
        logged and allowed (``would_deny``, the enforcement ramp).

        #1286 (P0-5): ``operation`` threads MAE audit identity into the
        evaluation so denies at THIS chokepoint persist — for the MCP read
        tools this is the pre-gate that raises before any service-layer gate
        runs, so without it enforce-mode read denies leave no audit row on
        the MCP face (the #1291/#1292 parity class). ``None`` (callers
        outside the MAE vocabulary — context CRUD/metadata reads) keeps the
        log-only behavior.

        Returns the evaluated binding decision (``allowed`` or ``would_deny``)
        when an agent scope applies and the request is permitted (or shadow-
        proceeds), or ``None`` when no agent scope applies. A hard deny raises
        ``NotFoundException`` instead of returning. Callers that stamp the
        decision on their own audit row (#1402, agent bootstrap) read it; the
        others ignore it (byte-compat).
        """
        from auth.agent_scope import get_agent_scope

        scope = get_agent_scope()
        if scope is None:
            return None

        from services.agent_binding_service import AgentBindingService

        allowed, decision = await AgentBindingService(self.db).evaluate_context_access(
            scope, context_id, access, operation=operation, user_id=user_id
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
        return decision

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

    async def is_read_only_member(self, user_id: str, workspace_id: UUID) -> bool:
        """True when the caller's role in ``workspace_id`` is ``viewer``.

        Memory writes need the member role or higher on the workspace that owns
        the target context (``docs/concepts.md``: a viewer has read-only
        access). The check is on the role alone: a caller without a membership
        row is ``False`` here, and the reach checks around it decide that case.
        """
        member = await self.workspace_service.get_member(
            workspace_id,
            user_id,
            raise_if_not_found=False,
        )
        return member is not None and member.role == WorkspaceRole.VIEWER

    async def ensure_memory_write_role(self, user_id: str, workspace_id: UUID) -> None:
        """Refuse a memory write by a workspace viewer.

        Call it after the caller's reach to the context is established, so a
        refusal never answers for a context the caller cannot see. It applies
        to a private context the viewer created as well: the workspace role
        makes the account read-only in the whole workspace, the same rule
        ``rollback_session`` applies.

        Raises:
            AuthorizationError: 403 (``AUTH-101``) for a workspace viewer.
        """
        if await self.is_read_only_member(user_id, workspace_id):
            logger.info(
                "memory_write_denied",
                reason="viewer_read_only",
                workspace_id=str(workspace_id),
                user_id=user_id,
            )
            raise AuthorizationError(reason="viewer_read_only")

    # ========================================================================
    # Context-level Permissions
    # ========================================================================

    async def check_context_access(
        self,
        user_id: str,
        context_id: UUID,
        required_role: ContextRole | str = ContextRole.VIEWER,
        *,
        capacity: CapacityPolicy,
        operation: str | None = None,
        key_workspace_id: UUID | None = None,
    ) -> tuple[Context, ContextRole]:
        """Check if user has required context access.

        #1948: ``capacity`` is required — ``BLOCKED`` applies the capacity-over
        lock (#1941) after the RBAC decision and the agent-binding filter;
        ``ALLOWED`` is the cleanup / inspection surface (see ``CapacityPolicy``).

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
            key_workspace_id: Issue #963 — the caller's pure API-key workspace
                scope, or ``None`` when the request did not use a
                workspace-scoped key. A context of another workspace is the
                uniform ``NotFoundException``, as in
                ``resolve_context_for_workspace_read``.

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
            user_id, context_id, required_role, key_workspace_id=key_workspace_id
        )

        # ContextRole is a str-comparable enum here only via its value set;
        # normalize the same way the RBAC core does.
        required = (
            required_role if isinstance(required_role, ContextRole) else ContextRole(required_role)
        )
        access = access_for_context_role(required)
        await self._apply_agent_binding_filter(
            context_id, access=access, user_id=user_id, operation=operation
        )

        # #1948: the capacity-over lock, after authorization (never an oracle);
        # only the enum member ALLOWED skips it (a stray value fails closed). ``user_id`` keeps the outsider
        # redaction for the private-context creator shortcut, which since #1957
        # reads membership too and reaches here without a row only in the
        # logged, unexpected case.
        if capacity is not CapacityPolicy.ALLOWED:
            await ensure_not_capacity_locked(self.db, context.workspace_id, user_id=user_id)
            # #1943: a content write into a context without a writable slot.
            if access == ACCESS_WRITE:
                await ensure_context_writable(self.db, context)

        return context, effective_role

    async def _seated_member(self, workspace_id: UUID, user_id: str) -> WorkspaceMember | None:
        """The caller's membership row once their plan seat is confirmed.

        One place for the "membership → ``ensure_member_seated``" pair the
        seat gates share (#1943, #1957): ``None`` when there is no membership
        row (the caller decides what that means), otherwise the row after the
        seat check, which raises ``FeatureNotAvailableError`` for a member
        without a seat.
        """
        member = await self.workspace_service.get_member(
            workspace_id, user_id, raise_if_not_found=False
        )
        if member is not None:
            await ensure_member_seated(self.db, workspace_id, member)
        return member

    async def _check_context_access_rbac(
        self,
        user_id: str,
        context_id: UUID,
        required_role: ContextRole | str = ContextRole.VIEWER,
        *,
        key_workspace_id: UUID | None = None,
    ) -> tuple[Context, ContextRole]:
        """The RBAC decision core of ``check_context_access``.

        ``check_context_access`` wraps it with the agent-binding intersection
        (#1275) and adds nothing else, so a caller without ``agent_id`` gets
        exactly this decision.

        #1784: a private context is also open to an account linked to its
        creator — as that account: its own workspace membership, whitelist
        and role decide what it may do there.
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

        # Issue #963: a workspace-scoped key reaches its own workspace only.
        # Before every membership read, with the not-found answer above.
        if key_workspace_id is not None and context.workspace_id != key_workspace_id:
            logger.warning(
                "context_access_denied",
                reason="key_workspace_mismatch",
                context_id=str(context_id),
                context_workspace_id=str(context.workspace_id),
                key_workspace_id=str(key_workspace_id),
                user_id=user_id,
            )
            raise NotFoundException("Context")

        # Issue #165: Private context check (creator-only access)
        # Normalise at the boundary: accept str for backward compat
        required = (
            required_role if isinstance(required_role, ContextRole) else ContextRole(required_role)
        )

        linked_owner = False
        if context.is_private:
            if context.created_by == user_id:
                # Creator has full access to their private context — once they
                # are a seated member (#1957). Membership and the seat are read
                # only after the caller is known to be the creator, so a
                # non-creator still learns nothing here (#394 ordering). A
                # creator with no membership row is not expected (removing a
                # member hands their contexts to the owner, #275); it is logged
                # and refused like any non-member, as ``ContextService.get_context``
                # and the MCP resolver already do. The shortcut still skips the
                # role and whitelist rules below — that is #165's contract for
                # the creator, not part of #1957.
                if await self._seated_member(context.workspace_id, user_id) is None:
                    # The workspace's owner_user_id holder always has a seat
                    # (plan_seats.member_seat_suspended makes the same
                    # exception), even through a missing OWNER row (#1108).
                    workspace = await self.db.get(Workspace, context.workspace_id)
                    if workspace is None or workspace.owner_user_id != user_id:
                        logger.warning(
                            "private_context_creator_without_membership",
                            context_id=str(context_id),
                            workspace_id=str(context.workspace_id),
                            user_id=user_id,
                        )
                        raise AuthorizationError("Insufficient permissions")
                return context, ContextRole.OWNER
            # #1784: an account linked to the creator is the same person. The
            # link widens ownership only: the caller still passes the
            # membership, whitelist and role rules below as itself.
            if not await is_same_owner(self.db, user_id, context.created_by):
                # Others cannot access private contexts
                raise AuthorizationError("Insufficient permissions")
            linked_owner = True

        # Shared context, or a linked account's private one: workspace membership,
        # then the seat (#1943) — right after the membership row, before the
        # owner/admin bypass (the owner always has a seat; no query for them).
        workspace_member = await self._seated_member(context.workspace_id, user_id)
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

        if linked_owner:
            # A member with no whitelist is suspended (Migration 042): the
            # link does not lift that. One whose whitelist names this context
            # passed the check above and owns it like the linked creator.
            if workspace_member.allowed_context_ids is None:
                raise AuthorizationError("Insufficient permissions")
            return context, ContextRole.OWNER

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

    async def check_context_write(
        self,
        user_id: str,
        context_id: UUID,
        *,
        capacity: CapacityPolicy,
        key_workspace_id: UUID | None = None,
    ) -> Context:
        """Check if user can write to context (editor or owner).

        Args:
            user_id: User ID
            context_id: Context ID
            capacity: #1948 — see ``CapacityPolicy``.
            key_workspace_id: #963 pure key scope — see ``check_context_access``.

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
            capacity=capacity,
            key_workspace_id=key_workspace_id,
        )
        return context

    async def check_context_owner(
        self,
        user_id: str,
        context_id: UUID,
        *,
        capacity: CapacityPolicy,
        key_workspace_id: UUID | None = None,
    ) -> Context:
        """Check if user is context owner.

        Args:
            user_id: User ID
            context_id: Context ID
            capacity: #1948 — see ``CapacityPolicy``.
            key_workspace_id: #963 pure key scope — see ``check_context_access``.

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
            capacity=capacity,
            key_workspace_id=key_workspace_id,
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
        # #1784: the caller's own includes those of an account linked to it.
        privacy_filter = (Context.is_private.is_(False)) | owned_by(Context.created_by, user_id)

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
            # A capability probe, not a content access: never gated (#1948).
            await self.check_context_owner(user_id, context_id, capacity=CapacityPolicy.ALLOWED)
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
            # A capability probe, not a content access: never gated (#1948).
            await self.check_context_write(user_id, context_id, capacity=CapacityPolicy.ALLOWED)
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
        access: Access = ACCESS_READ,
        operation: str | None = None,
        memory_id: UUID | None = None,
        memory_type: str | None = None,
        memory_source_type: str | None = None,
    ) -> bool:
        """Check if user can access a memory based on workspace membership and context privacy.

        Access rules:
        1. Private context (is_private=true): Only creator can access
        2. Shared context (is_private=false): Workspace members can access
        3. ``access="write"``: the caller's role on the memory's workspace must
           be member or higher — a workspace viewer is refused, on its own
           memories and in a private context it created too.

        Issue #1275 (RFC-0002 P0-2): this is the memory-id-addressed access
        chokepoint (reference / update / forget-by-id / explore authorize the
        memory's *own* stored ``context_id`` here). After the base RBAC
        decision allows, the purely subtractive agent-binding intersection is
        applied for agent-bound credentials, so a binding-denied context is
        unreachable even by memory_id. Callers on a WRITE path (update /
        forget) MUST pass ``access="write"`` so a ``can_read``-only binding
        cannot be used to mutate. No-op for non-agent credentials.

        Issue #1299: the **read-lane** callers (reference, forget-by-id,
        explore seed) additionally thread the fetched row's own ``memory_type``
        / ``memory_source_type`` so the binding's per-memory type/source
        filter applies. #1301 extended the same threading to the id-addressed
        **update** paths (PATCH / in-place update): a no-op update on a
        read-denied row would otherwise read back the L3 content the read
        lanes hide, so those ops are uniformly 404 on denied rows too. The
        one deliberate exception is the upsert's internal replacement
        forget (``_skip_binding_row_filter``), which passes ``None`` — see
        MemoryService.forget. Whether a mutation may run at all stays
        governed by ``write_policy``.

        #1286 item 2 (P0-5): ``operation`` / ``memory_id`` are the
        audit-identity passthrough. The binding-shaped deny is persisted
        inside the binding evaluation; the rbac-shaped branches below stamp
        ``policy_decision='rbac_denied'`` themselves — this is the one
        chokepoint where the two deny causes collapse into a single bool, so
        the emission is what keeps them distinguishable in audit. No-op for
        non-agent traffic and for un-threaded callers.

        Args:
            user_id: Requesting user ID
            memory_user_id: Memory creator's user ID
            workspace_id: Workspace ID
            context_id: Context ID
            access: ``"read"`` (default) or ``"write"`` — the binding gate.
            operation: MAE operation vocabulary value for deny capture, or
                ``None`` (no emission) for callers outside that vocabulary.
            memory_id: The requested memory id — rides ``event_metadata`` as
                an unverified claim on deny rows.
            memory_type: The fetched row's own ``type`` (#1299) — read-lane
                AND id-addressed update callers (#1301) thread it so the
                binding's per-memory type filter applies; only internal
                maintenance callers leave it ``None``.
            memory_source_type: The fetched row's own ``source_type`` (#1299),
                same threading rule.

        Returns:
            True if user can access, False otherwise
        """

        async def _binding_ok() -> bool:
            # The subtractive agent-binding gate applies to EVERY caller,
            # including the memory owner (an agent bound-denied to a context
            # must not reach its own memories there via memory_id).
            from services.agent_binding_service import agent_binding_permits

            return await agent_binding_permits(
                self.db,
                context_id,
                access,
                operation=operation,
                user_id=str(user_id),
                requested_memory_id=memory_id,
                memory_type=memory_type,
                memory_source_type=memory_source_type,
            )

        async def _emit_rbac_denied() -> None:
            # #1286 item 2 (P0-5): persist the rbac-shaped deny for agent
            # traffic. workspace_id = the CREDENTIAL scope (never the
            # memory's workspace param — that is derived from the requested,
            # unverified identifier); requested ids ride event_metadata.
            if operation is None:
                return
            from auth.agent_scope import get_agent_scope

            scope = get_agent_scope()
            if scope is None:
                # D34: only verified-agent traffic is audited.
                return
            from services.agent_binding_service import DECISION_RBAC_DENIED
            from services.memory_access_event_writer import emit_memory_access_event

            metadata: dict[str, str] = {
                "requested_context_id": str(context_id),
                "access": access,
            }
            if memory_id is not None:
                metadata["requested_memory_id"] = str(memory_id)
            await emit_memory_access_event(
                operation=operation,
                outcome="denied",
                workspace_id=scope.workspace_id,
                user_id=str(user_id),
                policy_decision=DECISION_RBAC_DENIED,
                extra_metadata=metadata,
            )

        def _read_only(member: WorkspaceMember | None) -> bool:
            # A write needs the member role or higher on the memory's
            # workspace: a viewer reads and cannot create, change or delete,
            # its own memories and private contexts included (see
            # ``ensure_memory_write_role``).
            return (
                access == ACCESS_WRITE
                and member is not None
                and member.role == WorkspaceRole.VIEWER
            )

        # Owner can always access their own memories (RBAC), but the binding
        # still subtracts.
        if user_id == memory_user_id:
            # #1943: a member without a seat on the plan is refused here too —
            # the REST memory-id routes (reference / patch / explore) authorize
            # through this method alone. An author with no membership row keeps
            # the pre-existing pass on their own memories; the context-level
            # paths (#1957) refuse that caller, so the two surfaces differ here
            # by design, not by accident.
            if _read_only(await self._seated_member(workspace_id, user_id)):
                await _emit_rbac_denied()
                return False
            return await _binding_ok()

        # Check if context is shared
        from services.context_service import ContextService

        context_service = ContextService(self.db)
        is_shared = await context_service.is_context_shared(context_id)

        if not is_shared:
            # Private context: only its creator, and — #1784 — a memory written
            # there by an account linked to the caller is the caller's own.
            # Both must hold: the context is the caller's (directly or by a
            # link, with the caller's own workspace membership) and so is the
            # memory's author.
            if await is_same_owner(self.db, user_id, memory_user_id):
                # A write needs what a write into the context needs: a
                # workspace viewer reads a linked account's memories and
                # cannot change or delete them.
                needed = ContextRole.EDITOR if access == ACCESS_WRITE else ContextRole.VIEWER
                try:
                    await self._check_context_access_rbac(user_id, context_id, needed)
                except (AuthorizationError, NotFoundException):
                    await _emit_rbac_denied()
                    return False
                # The creator of a private context passes the RBAC core as its
                # owner whatever its workspace role; the role still decides a write.
                if access == ACCESS_WRITE and await self.is_read_only_member(user_id, workspace_id):
                    await _emit_rbac_denied()
                    return False
                return await _binding_ok()
            await _emit_rbac_denied()
            return False

        # Shared context: workspace membership and the seat (#1943), the member
        # role for a write, then the binding.
        member = await self._seated_member(workspace_id, user_id)
        if member is None or _read_only(member):
            await _emit_rbac_denied()
            return False
        return await _binding_ok()


CONTENT_CHUNKS_OWNER_ONLY_MESSAGE = (
    "Only the context owner or a workspace owner/admin can change content_chunks_enabled"
)

CONTENT_CHUNKS_UNSUPPORTED_MESSAGE = (
    "content_chunks_enabled cannot be turned on with the LanceDB vector store "
    "(KAGURA_VECTOR_BACKEND=lance): it keeps one vector per memory and has no "
    "content chunk vectors. Content chunks need the Qdrant backend."
)


async def check_content_chunks_change(
    perm_service: PermissionService, user_id: str, context_id: UUID
) -> None:
    """Require the context owner to change ``content_chunks_enabled`` (#2015).

    Turning the flag on is the first time memory content, not only summaries,
    goes to the embedding and rerank providers, so a change of the stored value
    (either way) takes ``check_context_owner``: a context owner, the creator of
    a private context, or a workspace owner/admin. The caller has already
    passed the context write check, so the refusal names the setting.

    Raises:
        AuthorizationError: 403 (``AUTH-101``) for anyone below context owner.
    """
    try:
        # The write check before this one carries the capacity decision.
        await perm_service.check_context_owner(user_id, context_id, capacity=CapacityPolicy.ALLOWED)
    except AuthorizationError as exc:
        raise AuthorizationError(
            CONTENT_CHUNKS_OWNER_ONLY_MESSAGE, reason="content_chunks_owner_only"
        ) from exc


async def gate_content_chunks_change(
    perm_service: PermissionService,
    user_id: str,
    context_id: UUID,
    update: ContextSearchConfigUpdate,
    *,
    current_enabled: bool,
) -> ContextSearchConfigUpdate:
    """Apply the #2015 owner rule to a search-config update.

    The rule: a request whose ``content_chunks_enabled`` differs from the
    stored value needs the context owner (``check_content_chunks_change``).
    One that omits it, or sends the stored value, is not a change: any editor
    may send it, and the field is dropped from the update so the write never
    touches the flag (a concurrent owner change is not overwritten by it).

    It also holds a backend limit (#2072): the LanceDB store keeps no content
    chunk points, so turning the flag on there is refused instead of being
    reported on and doing nothing. And, where the deployment gates content
    chunks by plan (#2103, ``CONTENT_CHUNKS_REQUIRE_PLAN``), the plan's
    feature and its cap on contexts with chunks on. Turning it off stays
    allowed. Order when turning on: store (422), plan (403 ``FEAT-001``),
    owner (403 ``AUTH-101``), cap (429 ``QUOTA-001``). The cap check takes
    the workspace's advisory lock, held until the caller's transaction ends:
    the update must commit on the same session with nothing committing in
    between (``ContextSearchConfigRepository.update`` is that commit).

    Returns:
        The update to apply (``update`` itself, or a copy without the flag).

    Raises:
        AuthorizationError: 403 when the flag changes and the caller is not
            the context owner or a workspace owner/admin.
        ValidationError: 422 (``VAL-001``) when the change turns the flag on
            and the vector store has no content chunk points.
        FeatureNotAvailableError: 403 (``FEAT-001``) when the change turns the
            flag on and the plan lacks ``content_chunks`` (gated only).
        QuotaExceededError: 429 (``QUOTA-001``) when the change turns the flag
            on and the workspace is at ``max_content_chunk_contexts`` (gated
            only).
    """
    if "content_chunks_enabled" not in update.model_fields_set:
        return update
    if update.content_chunks_enabled == current_enabled:
        return ContextSearchConfigUpdate(
            **update.model_dump(exclude_unset=True, exclude={"content_chunks_enabled"})
        )
    from services.content_chunk_policy import (
        ensure_content_chunk_capacity,
        ensure_content_chunks_plan,
    )

    turning_on = update.content_chunks_enabled is True
    # Before the owner check: the answers do not depend on the caller, so an
    # editor is not sent to an owner whose request would be refused too.
    if turning_on:
        if not chunk_points_supported():
            raise ValidationError(
                CONTENT_CHUNKS_UNSUPPORTED_MESSAGE, field="content_chunks_enabled"
            )
        await ensure_content_chunks_plan(perm_service.db, context_id)
    await check_content_chunks_change(perm_service, user_id, context_id)
    # After it: an editor's 403 never takes the cap's lock.
    if turning_on:
        await ensure_content_chunk_capacity(perm_service.db, context_id)
    return update


LLM_SELECTOR_OWNER_ONLY_MESSAGE = (
    "Only the context owner or a workspace owner/admin can change llm_selector_enabled"
)


async def check_llm_selector_change(
    perm_service: PermissionService, user_id: str, context_id: UUID
) -> None:
    """Require the context owner to change ``llm_selector_enabled`` (#2144).

    Turning the flag on sends the context's memory text to an LLM provider at
    recall time (and, under BYOK, the workspace pays for it), so a change of
    the stored value (either way) takes ``check_context_owner``, the
    ``content_chunks_enabled`` rule (#2015).

    Raises:
        AuthorizationError: 403 (``AUTH-101``) for anyone below context owner.
    """
    try:
        # The write check before this one carries the capacity decision.
        await perm_service.check_context_owner(user_id, context_id, capacity=CapacityPolicy.ALLOWED)
    except AuthorizationError as exc:
        raise AuthorizationError(
            LLM_SELECTOR_OWNER_ONLY_MESSAGE, reason="llm_selector_owner_only"
        ) from exc


async def gate_llm_selector_change(
    perm_service: PermissionService,
    user_id: str,
    context_id: UUID,
    update: ContextSearchConfigUpdate,
    *,
    current_enabled: bool,
) -> ContextSearchConfigUpdate:
    """Apply the #2144 owner rule and plan gate to a search-config update.

    The twin of :func:`gate_content_chunks_change` without the store check
    (the selector stores nothing). A request whose ``llm_selector_enabled``
    differs from the stored value needs the context owner; one that omits it,
    or sends the stored value, is not a change and the field is dropped from
    the update. Where the deployment gates the selector by plan
    (``LLM_SELECTOR_REQUIRE_PLAN``), turning it on also needs the plan's
    feature and a free slot under its cap. Turning it off stays allowed.
    Order when turning on: plan (403 ``FEAT-001``), owner (403 ``AUTH-101``),
    cap (429 ``QUOTA-001``). The cap check takes the workspace's advisory
    lock, held until the caller's transaction ends: the update must commit on
    the same session with nothing committing in between.

    Returns:
        The update to apply (``update`` itself, or a copy without the flag).

    Raises:
        AuthorizationError: 403 when the flag changes and the caller is not
            the context owner or a workspace owner/admin.
        FeatureNotAvailableError: 403 (``FEAT-001``) when the change turns the
            flag on and the plan lacks ``llm_selector`` (gated only).
        QuotaExceededError: 429 (``QUOTA-001``) when the change turns the flag
            on and the workspace is at ``max_llm_selector_contexts`` (gated
            only).
    """
    if "llm_selector_enabled" not in update.model_fields_set:
        return update
    if update.llm_selector_enabled == current_enabled:
        return ContextSearchConfigUpdate(
            **update.model_dump(exclude_unset=True, exclude={"llm_selector_enabled"})
        )
    from services.llm_selector_policy import (
        ensure_llm_selector_capacity,
        ensure_llm_selector_plan,
    )

    turning_on = update.llm_selector_enabled is True
    # Before the owner check: the answer does not depend on the caller.
    if turning_on:
        await ensure_llm_selector_plan(perm_service.db, context_id)
    await check_llm_selector_change(perm_service, user_id, context_id)
    # After it: an editor's 403 never takes the cap's lock.
    if turning_on:
        await ensure_llm_selector_capacity(perm_service.db, context_id)
    return update
