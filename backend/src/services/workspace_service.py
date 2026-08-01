"""Workspace Service for multi-tenancy management.

Issue #115 Phase B-2: Workspace-level Multi-tenancy

Manages workspaces, memberships, and workspace-level operations.
"""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, ExternalAPIKey, UsageStats, Workspace, WorkspaceMember
from models.memory import Memory
from services.workspace_locks import lock_workspace_for_update
from utils.datetime import utcnow
from utils.exceptions import NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)
ANONYMOUS_USER_ID = "anonymous"  # P2: Magic string constant for anonymous users


class WorkspaceService:
    """Service for managing workspaces and memberships.

    Handles workspace CRUD, member management, and access control.

    Attributes:
        db: AsyncSession for database access
    """

    def __init__(self, db: AsyncSession):
        """Initialize workspace service.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db

    # ========================================================================
    # Workspace CRUD Operations
    # ========================================================================

    async def create_workspace(
        self,
        name: str,
        owner_user_id: str,
        openai_api_key: str | None = None,
        description: str | None = None,
        plan_name: str = "free",
        create_default_context: bool = True,
        default_context_name: str | None = None,
        default_context_summary: str | None = None,
        default_context_usage_guide: str | None = None,
        default_context_embedding_model: str | None = None,
    ) -> Workspace:
        """Create a new workspace.

        Issue #146: Supports OpenAI API key for workspace-level configuration.
        Issue #165: API key now optional - can be added later for personal workspaces.
        Issue #169: Supports default context settings.
        Issue #276: Slug removed - not used for routing, simplified UX.

        Args:
            name: Workspace display name
            owner_user_id: User ID of the owner
            openai_api_key: OpenAI API key (optional, workspace-scoped)
            description: Optional description
            plan_name: Billing plan (free/pro/enterprise)
            create_default_context: Whether to create default context (default: True)
            default_context_name: Optional name for default context (defaults to "default")
            default_context_summary: Optional summary for default context
            default_context_usage_guide: Optional usage guide for default context
            default_context_embedding_model: Optional embedding model for default context

        Returns:
            Created workspace

        Raises:
            ValidationError: If workspace creation fails

        Note:
            - If openai_api_key is None, no default context will be created
            - Contexts require API key for embedding generation
            - User can add API key later in External Keys settings
        """
        # Create workspace
        workspace = Workspace(
            name=name,
            owner_user_id=owner_user_id,
            description=description,
            plan_name=plan_name,
        )

        self.db.add(workspace)
        await self.db.flush()

        # Add owner as member
        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=owner_user_id,
            role=WorkspaceRole.OWNER,
            joined_at=func.now(),
        )
        self.db.add(member)

        # Create workspace-level OpenAI API key (Issue #146)
        # Issue #165: Now optional - personal workspaces can be created without API key
        if openai_api_key:
            from models.auth import ExternalAPIKey
            from utils.encryption import get_encryptor

            # Create workspace-scoped OpenAI key for this workspace
            # Issue #146+: Each workspace has independent API keys
            # Migration 026 ensures UNIQUE constraint includes workspace_id
            encryptor = get_encryptor()
            encrypted_key = encryptor.encrypt(openai_api_key)

            external_key = ExternalAPIKey(
                user_id=owner_user_id,  # For audit trail
                workspace_id=workspace.id,  # Workspace-scoped
                key_name="OPENAI_API_KEY",
                provider="openai",  # Required field
                encrypted_value=encrypted_key,
                enabled=True,
            )
            self.db.add(external_key)
            logger.info(f"Created workspace-scoped OpenAI key for workspace={workspace.id}")
        else:
            logger.info(
                f"Workspace {workspace.id} created without OpenAI key (will be added later)"
            )

        # Create default context for the workspace (if API key provided)
        # Issue #165: Skip context creation if no API key or explicitly disabled
        # Issue #169: Use provided settings or defaults
        default_context = None
        if openai_api_key and create_default_context:
            from models.auth import User
            from services.context_service import ContextService

            context_service = ContextService(self.db)
            context_name = default_context_name or "default"
            default_context = await context_service.create_context(
                workspace_id=workspace.id,
                name=context_name,
                description=f"Context '{context_name}' (auto-created)",
                summary=default_context_summary,
                usage_guide=default_context_usage_guide,
                embedding_model=default_context_embedding_model or "text-embedding-3-small",
                created_by=owner_user_id,
                create_collection=True,
            )
            logger.info(
                f"Created default context for workspace={workspace.id}: {default_context.id}"
            )
        else:
            logger.info(
                f"Skipped default context creation for workspace={workspace.id} (no API key or disabled)"
            )

        # Set user's current workspace and context (only if not already set)
        # Issue #165: Don't overwrite current_workspace_id if user already has one
        from models.auth import User

        user_result = await self.db.execute(select(User).where(User.user_id == owner_user_id))
        user = user_result.scalar_one_or_none()
        if user:
            if not user.current_workspace_id:
                user.current_workspace_id = workspace.id
            # Issue #246: current_context_id initialization removed

        await self.db.commit()
        await self.db.refresh(workspace)

        logger.info(
            f"Created workspace: {workspace.id} (owner: {owner_user_id}), "
            f"default context: {default_context.id if default_context else 'none'}"
        )

        # Issue #1470: a referral grant can be earned before the beneficiary owns
        # any workspace — ``api/routes/auth.py`` skips personal-workspace bootstrap
        # entirely when the user has pending team invitations. Those grants are
        # recorded with a NULL workspace_id and applied here, once a workspace
        # exists. This is the single funnel: ``ensure_personal_workspace`` and
        # ``create_personal_workspace`` both route through this method.
        #
        # Non-blocking on purpose: workspace creation must never fail because a
        # bonus could not be applied. The grant row survives either way, so a
        # later workspace creation (or an admin) can still resolve it.
        try:
            from services.referral_service import ReferralService

            await ReferralService(self.db).apply_pending_grants(owner_user_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "referral_pending_grants_apply_failed",
                workspace_id=str(workspace.id),
                owner_user_id=owner_user_id,
                error=str(exc),
            )

        return workspace

    async def ensure_personal_workspace(
        self,
        user_id: str,
        email: str,
    ) -> Workspace | None:
        """Ensure user has a personal workspace (auto-create only when appropriate).

        Issue #212: Auto-create personal workspace on first login.
        Issue #660: Avoid silently recreating a workspace after a user intentionally
        deletes one. The original logic decided based solely on `current_workspace_id`,
        which `delete_workspace` clears as a side-effect — making "first-ever login"
        and "existing user deleted everything on purpose" indistinguishable.

        Branch table (after the SKIP fast-path):
          SKIP : current_workspace_id resolves to a live workspace → return None
          (1)  : user still has WorkspaceMember rows on non-deleted workspaces →
                 select with deterministic role-preferring order (owner > admin > member
                 > viewer, tie by joined_at desc), set current_workspace_id, return it
          (2)  : zero WorkspaceMember rows ever recorded (true first-ever login) →
                 create personal workspace
          (3)  : has historical WorkspaceMember rows but none on a live workspace
                 (returning user deleted everything) → return None; frontend renders
                 the empty-state UI

        First-login discriminator: `count(WorkspaceMember WHERE user_id = ?) == 0`.
        `last_login_at` is NOT usable here — `auth.roles.RoleManager.ensure_user`
        writes it whenever a User row is touched (creation OR sync), which runs
        BEFORE this method in every OAuth callback. WorkspaceMember rows persist
        through workspace soft-delete (`delete_workspace` does not hard-delete
        members), so a non-zero count = "user has been part of the system before".

        Args:
            user_id: User ID (OAuth sub claim)
            email: User email (for logging)

        Returns:
            Workspace on branches (1) and (2); None on SKIP and (3).

        Note:
            - Does NOT create default context (user creates manually)
            - Sets `user.current_workspace_id` on branches (1) and (2)
            - Non-blocking: returns None on error
        """
        from sqlalchemy.orm import aliased

        from models.auth import User

        # Lazy import: permission_service imports WorkspaceService at module level.
        from services.permission_service import ORG_ROLE_WEIGHTS

        try:
            # Single query: fetch user with optional active workspace via LEFT JOIN
            ws_alias = aliased(Workspace)
            result = await self.db.execute(
                select(User, ws_alias)
                .outerjoin(
                    ws_alias,
                    and_(
                        ws_alias.id == User.current_workspace_id,
                        ws_alias.deleted_at.is_(None),
                    ),
                )
                .where(User.user_id == user_id)
            )
            row = result.one_or_none()

            if not row:
                logger.warning("ensure_workspace_user_not_found", user_id=user_id)
                return None

            user, existing_workspace = row

            if user.current_workspace_id and existing_workspace:
                logger.debug(
                    f"User {email} already has active workspace: {user.current_workspace_id}"
                )
                return None

            # `delete_workspace` already clears `current_workspace_id` for every
            # user that had the deleted workspace as current (Issue #218); this
            # fires only in edge cases (e.g., concurrent state).
            if user.current_workspace_id and not existing_workspace:
                logger.info(
                    "ensure_workspace_cleared_dangling_current",
                    user_id=user_id,
                    email=email,
                    dangling_workspace_id=str(user.current_workspace_id),
                )
                user.current_workspace_id = None

            # Fetch every WorkspaceMember row for this user — including those
            # pointing at soft-deleted workspaces. One query answers both
            # "any live membership for branch (1)?" and "any historical
            # membership for the branch (2)/(3) discriminator?".
            all_memberships = (
                await self.db.execute(
                    select(WorkspaceMember, Workspace)
                    .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                    .where(WorkspaceMember.user_id == user_id)
                )
            ).all()
            live_memberships = [(m, w) for m, w in all_memberships if w.deleted_at is None]

            if live_memberships:
                # Highest privilege first (owner > admin > member > viewer),
                # tie by most recent joined_at, final tiebreaker on the
                # WorkspaceMember UUID so the pick is fully deterministic even
                # when `joined_at` is None (nullable on the model; legacy or
                # backfilled rows can leave it unset).
                live_memberships.sort(
                    key=lambda r: (
                        -ORG_ROLE_WEIGHTS.get(r[0].role, 0),
                        -(r[0].joined_at.timestamp() if r[0].joined_at else 0.0),
                        str(r[0].id),
                    )
                )
                selected_member, selected_workspace = live_memberships[0]
                user.current_workspace_id = selected_workspace.id
                await self.db.commit()
                logger.info(
                    "auto_selected_remaining_workspace",
                    user_id=user_id,
                    email=email,
                    workspace_id=str(selected_workspace.id),
                    role=selected_member.role,
                    memberships_count=len(live_memberships),
                )
                return selected_workspace

            if not all_memberships:
                # True first-ever login.
                from config.plan_tiers import PlanName

                plan = PlanName.PRO if user.is_initial_admin else PlanName.FREE
                workspace = await self.create_workspace(
                    name="Personal Workspace",
                    owner_user_id=user_id,
                    description="Personal workspace (auto-created)",
                    plan_name=plan,
                    create_default_context=False,  # User creates context manually
                )
                logger.info(
                    "auto_created_personal_workspace",
                    user_id=user_id,
                    email=email,
                    workspace_id=str(workspace.id),
                )
                return workspace

            # Returning user with only soft-deleted memberships — respect intent.
            # Commit any defensive-fallback clearing from above.
            await self.db.commit()
            logger.info(
                "returning_user_no_memberships_skipped",
                user_id=user_id,
                email=email,
                historical_memberships=len(all_memberships),
            )
            return None

        except Exception as e:
            logger.error(f"Failed to ensure personal workspace for {email}: {e}")
            await self.db.rollback()
            return None

    async def get_workspace(self, workspace_id: UUID) -> Workspace:
        """Get workspace by ID.

        Args:
            workspace_id: Workspace ID

        Returns:
            Workspace

        Raises:
            NotFoundException: If workspace not found
        """
        stmt = select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        workspace = result.scalar_one_or_none()

        if not workspace:
            raise NotFoundException(f"Workspace not found: {workspace_id}")

        return workspace

    async def list_user_workspaces(self, user_id: str) -> list[Workspace]:
        """List all workspaces user belongs to.

        Args:
            user_id: User ID

        Returns:
            List of workspaces
        """
        stmt = (
            select(Workspace)
            .join(
                WorkspaceMember,
                WorkspaceMember.workspace_id == Workspace.id,
            )
            .where(
                WorkspaceMember.user_id == user_id,
                Workspace.deleted_at.is_(None),
            )
            .order_by(Workspace.created_at.desc())
        )

        result = await self.db.execute(stmt)
        workspaces = result.scalars().all()

        return list(workspaces)

    async def update_workspace(
        self,
        workspace_id: UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Workspace:
        """Update workspace details.

        Issue #276: Slug removed (no longer editable).

        Args:
            workspace_id: Workspace ID
            name: New name (optional)
            description: New description (optional)

        Returns:
            Updated workspace

        Raises:
            ValidationError: If workspace update fails
        """
        workspace = await self.get_workspace(workspace_id)

        if name is not None:
            workspace.name = name

        if description is not None:
            workspace.description = description

        workspace.updated_at = func.now()

        await self.db.commit()
        await self.db.refresh(workspace)

        logger.info(f"Updated workspace: {workspace.id}")
        return workspace

    async def delete_workspace(self, workspace_id: UUID, deleted_by: str) -> None:
        """Soft delete workspace.

        Issue #218: Clear current_workspace_id for all users in deleted workspace.
        Issue #223: Delete external API keys to allow re-creation in new workspace.

        Args:
            workspace_id: Workspace ID
            deleted_by: User ID who deleted the workspace
        """
        from sqlalchemy import delete

        from models.auth import User

        workspace = await self.get_workspace(workspace_id)

        # Issue #223: Delete external API keys (hard delete to allow re-creation)
        # The unique constraint is (user_id, key_name), so we need to delete them
        # to allow the same user to create the same key in a new workspace.
        external_keys_result = await self.db.execute(
            select(ExternalAPIKey).where(ExternalAPIKey.workspace_id == workspace_id)
        )
        external_keys = external_keys_result.scalars().all()
        external_keys_count = len(external_keys)

        if external_keys_count > 0:
            await self.db.execute(
                delete(ExternalAPIKey).where(ExternalAPIKey.workspace_id == workspace_id)
            )
            logger.info(
                f"Deleted {external_keys_count} external API keys for workspace {workspace.id}"
            )

        # Delete Qdrant points for ALL contexts (including soft-deleted)
        # Workspace deletion is permanent — clean up everything
        from services.context_service import ContextService

        contexts_result = await self.db.execute(
            select(Context).where(Context.workspace_id == workspace_id)
        )
        contexts = contexts_result.scalars().all()

        context_service = ContextService(self.db)
        for context in contexts:
            try:
                await context_service._delete_context_collection(
                    str(workspace_id), context.name, context_id=str(context.id)
                )
                logger.info(
                    "org_delete_qdrant_collection",
                    workspace_id=str(workspace_id),
                    context_name=context.name,
                )
            except Exception as e:
                # Qdrant deletion failure is non-fatal
                logger.warning(
                    "org_delete_qdrant_collection_failed",
                    workspace_id=str(workspace_id),
                    context_name=context.name,
                    error=str(e),
                )

        # Soft delete
        workspace.deleted_at = func.now()
        workspace.updated_at = func.now()

        # Issue #218: Clear current_workspace_id for all users who had this workspace as current
        # This ensures they get a new workspace auto-created on next login
        result = await self.db.execute(
            select(User).where(User.current_workspace_id == workspace_id)
        )
        users = result.scalars().all()

        for user in users:
            user.current_workspace_id = None
            # Issue #246: current_context_id removed
            logger.info(
                "cleared_user_workspace_and_context",
                user_id=user.user_id,
                workspace_id=str(workspace.id),
            )

        await self.db.commit()

        logger.info(f"Deleted workspace: {workspace.id} (by: {deleted_by})")

    # ========================================================================
    # Member Management
    # ========================================================================

    async def add_member(
        self,
        workspace_id: UUID,
        user_id: str,
        role: WorkspaceRole | str = WorkspaceRole.MEMBER,
        invited_by: str | None = None,
    ) -> WorkspaceMember:
        """Add a member to workspace.

        Args:
            workspace_id: Workspace ID
            user_id: User ID to add
            role: Member role (owner/admin/member/viewer)
            invited_by: User ID who invited this member

        Returns:
            Created membership

        Raises:
            ValidationError: If member already exists
        """
        # Validate role
        self.validate_role(role)

        # Check if member already exists
        existing = await self.get_member(workspace_id, user_id, raise_if_not_found=False)
        if existing:
            raise ValidationError(f"User {user_id} is already a member")

        # Create membership
        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
            invited_at=func.now(),
            joined_at=func.now(),
        )

        self.db.add(member)
        await self.db.commit()
        await self.db.refresh(member)

        logger.info(f"Added member to workspace {workspace_id}: {user_id} (role: {role})")
        return member

    async def get_member(
        self,
        workspace_id: UUID,
        user_id: str,
        raise_if_not_found: bool = True,
        with_lock: bool = False,
    ) -> WorkspaceMember | None:
        """Get workspace member.

        Args:
            workspace_id: Workspace ID
            user_id: User ID
            raise_if_not_found: Raise exception if not found
            with_lock: Use SELECT FOR UPDATE to prevent race conditions

        Returns:
            Membership or None

        Raises:
            NotFoundException: If not found and raise_if_not_found=True
        """
        stmt = select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )

        # Issue #275 High: Row-level locking for concurrent member operations
        if with_lock:
            stmt = stmt.with_for_update()

        result = await self.db.execute(stmt)
        member = result.scalar_one_or_none()

        if not member and raise_if_not_found:
            raise NotFoundException(f"Member not found: {user_id} in workspace {workspace_id}")

        return member

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]:
        """List all members of workspace.

        Args:
            workspace_id: Workspace ID

        Returns:
            List of members
        """
        stmt = (
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .order_by(
                # Owner first, then admin, member, viewer
                WorkspaceMember.role.desc(),
                WorkspaceMember.joined_at,
            )
        )

        result = await self.db.execute(stmt)
        members = result.scalars().all()

        return list(members)

    async def update_member_role(
        self,
        workspace_id: UUID,
        user_id: str,
        new_role: str,
    ) -> WorkspaceMember:
        """Update member's role.

        Args:
            workspace_id: Workspace ID
            user_id: User ID
            new_role: New role

        Returns:
            Updated membership

        Raises:
            ValidationError: If trying to create multiple owners
        """
        self.validate_role(new_role)

        member = await self.get_member(workspace_id, user_id)
        if member is None:
            # get_member raises by default; narrow for pyright + any future
            # call site that passes raise_if_not_found=False.
            raise NotFoundException(f"Member not found: {user_id} in workspace {workspace_id}")

        # #1102: a role change that ADDS or REMOVES an OWNER must serialize on the
        # workspace row with the other owner-mutating paths (transfer_ownership,
        # account_erasure) so the single-owner invariant is structural rather than
        # emergent. Plain member<->viewer changes never touch ownership and skip
        # the lock to keep the common path contention-free.
        locked_workspace = None
        if WorkspaceRole.OWNER in (new_role, member.role):
            locked_workspace = await lock_workspace_for_update(self.db, workspace_id)

        # #1108: refuse to demote the workspace owner via this path. The source of
        # truth for ownership is ``workspace.owner_user_id`` (what transfer_ownership
        # and downstream consumers read); in a consistent workspace its holder is the
        # single OWNER-role member. Lowering that holder's role here would drop the
        # OWNER-row count to zero while ``owner_user_id`` still names them, desyncing
        # the two representations (the gap #1102's lock serialized but did not itself
        # close). Key the guard on the SoT holder — NOT merely on
        # ``member.role == OWNER`` — so it (a) fires precisely for the real owner and
        # (b) still permits demoting a *stray* OWNER-role row that is not the
        # ``owner_user_id`` holder, which is the repair path out of a multi-owner
        # corruption. Owner changes must go through transfer_ownership, which moves
        # both representations atomically under this same lock (mirrors the
        # promote-to-owner guard below). ``locked_workspace`` is always present when
        # ``member.role == OWNER`` (the lock fires on that branch), so the consistent
        # owner-demotion case is always covered and the check runs serialized.
        if (
            new_role != WorkspaceRole.OWNER
            and locked_workspace is not None
            and locked_workspace.owner_user_id == user_id
        ):
            raise ValidationError(
                "Cannot demote the workspace owner via a role change. "
                "Only one owner is allowed per workspace. "
                "Please transfer ownership first if you want to change the owner."
            )

        # Validate single owner constraint (Issue #165)
        if new_role == WorkspaceRole.OWNER and member.role != WorkspaceRole.OWNER:
            # Promoting to owner - check if owner already exists
            stmt = select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == WorkspaceRole.OWNER,
            )
            result = await self.db.execute(stmt)
            existing_owners = result.scalars().all()

            if existing_owners:
                raise ValidationError(
                    "Workspace already has an owner. "
                    "Only one owner is allowed per workspace. "
                    "Please transfer ownership first if you want to change the owner."
                )

        old_role = member.role
        # new_role is validated by the caller's request schema; column is
        # Mapped[WorkspaceRole] (StrEnum). See the note in api/routes/contexts.py
        # — cast only, no behaviour change (#1442).
        member.role = cast(WorkspaceRole, new_role)
        member.updated_at = func.now()

        # Issue #234: Clear allowed_context_ids on role change
        # Previous restrictions may not be appropriate for the new role
        if old_role != new_role and member.allowed_context_ids is not None:
            member.allowed_context_ids = None
            logger.info(
                "cleared_allowed_context_ids_on_role_change",
                workspace_id=str(workspace_id),
                user_id=user_id,
                old_role=old_role,
                new_role=new_role,
            )

        await self.db.commit()
        await self.db.refresh(member)

        logger.info(f"Updated member role in workspace {workspace_id}: {user_id} -> {new_role}")
        return member

    async def update_member_context_access(
        self,
        workspace_id: UUID,
        user_id: str,
        allowed_context_ids: list[UUID] | None,
    ) -> WorkspaceMember:
        """Update member's allowed context access.

        Issue #234: Context access restriction for member/viewer.

        Args:
            workspace_id: Workspace ID
            user_id: User ID
            allowed_context_ids: Whitelist of context IDs (None = no restriction)

        Returns:
            Updated membership

        Note:
            This setting only applies to member/viewer roles.
            owner/admin roles always have full access (this setting is ignored).
        """
        member = await self.get_member(workspace_id, user_id)
        if member is None:
            # get_member raises by default; narrow for pyright + any future
            # call site that passes raise_if_not_found=False.
            raise NotFoundException(f"Member not found: {user_id} in workspace {workspace_id}")

        # Warn if setting on owner/admin (no effect)
        if (
            member.role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN)
            and allowed_context_ids is not None
        ):
            logger.warning(
                f"allowed_context_ids set on privileged role (ignored): "
                f"workspace={workspace_id}, user={user_id}, role={member.role}"
            )

        member.allowed_context_ids = allowed_context_ids
        member.updated_at = func.now()

        await self.db.commit()
        await self.db.refresh(member)

        count = len(allowed_context_ids) if allowed_context_ids else "unrestricted"
        logger.info(
            f"Updated member context access in workspace {workspace_id}: "
            f"{user_id} -> {count} contexts"
        )
        return member

    async def remove_member(self, workspace_id: UUID, user_id: str) -> None:
        """Remove member from workspace.

        Issue #275 Critical: Comprehensive cleanup including ownership transfers.
        Issue #275 High: Race condition prevention with row-level locking.

        Transfers to workspace owner:
        - Memories (organizational knowledge)
        - Contexts (workspace resources)
        - Resource tokens (API access)

        Deletes:
        - Context memberships
        - API credentials (keys, OAuth clients/tokens, external keys)
        - Pending invitations sent by this member

        Args:
            workspace_id: Workspace ID
            user_id: User ID to remove

        Raises:
            ValidationError: If trying to remove owner or role changed during operation
        """
        # Issue #275 High: SELECT FOR UPDATE to prevent concurrent deletion
        member = await self.get_member(workspace_id, user_id, with_lock=True)

        # Cannot remove owner
        if member.role == WorkspaceRole.OWNER:
            raise ValidationError("Cannot remove workspace owner")

        # Issue #275: Comprehensive cleanup with transaction safety
        from services.member_credentials_service import MemberCredentialsService

        cred_service = MemberCredentialsService(self.db)

        try:
            # Issue #275 High: Get owner once for all transfer operations (N+1 optimization)
            owner_id = await self.db.scalar(
                select(WorkspaceMember.user_id).where(
                    and_(
                        WorkspaceMember.workspace_id == workspace_id,
                        WorkspaceMember.role == WorkspaceRole.OWNER,
                    )
                )
            )

            # Issue #275 P0: Data integrity check - workspace must have owner
            if not owner_id:
                await self.db.rollback()
                logger.error(
                    "workspace_has_no_owner",
                    workspace_id=str(workspace_id),
                    user_id=user_id,
                )
                raise ValidationError(
                    f"Cannot remove member: Workspace {workspace_id} has no owner (data corruption)"
                )

            # Cleanup/Transfer in logical order
            context_cleanup = await cred_service.cleanup_context_members(workspace_id, user_id)
            cred_cleanup = await cred_service.cleanup_member_credentials(workspace_id, user_id)
            memory_transfer = await cred_service.cleanup_member_memories(
                workspace_id, user_id, owner_id
            )
            context_transfer = await cred_service.cleanup_member_contexts(
                workspace_id, user_id, owner_id
            )
            resource_token_transfer = await cred_service.cleanup_member_resource_tokens(
                workspace_id, user_id, owner_id
            )
            invitation_cleanup = await cred_service.cleanup_member_invitations(
                workspace_id, user_id
            )

            # Issue #275 High: Re-check role before deletion (permission escalation prevention)
            # Member object might be stale if role was changed during cleanup
            await self.db.refresh(member)
            if member.role == WorkspaceRole.OWNER:
                await self.db.rollback()
                logger.warning(
                    "member_role_changed_to_owner_during_removal",
                    workspace_id=str(workspace_id),
                    user_id=user_id,
                )
                raise ValidationError(
                    "Cannot remove member: Role was changed to owner during operation"
                )

            # Delete member record
            await self.db.delete(member)
            await self.db.commit()

            # Log after successful commit
            logger.info(
                "removed_member_from_workspace",
                workspace_id=str(workspace_id),
                user_id=user_id,
                context_members_deleted=context_cleanup["context_members_deleted"],
                api_keys_deleted=cred_cleanup["api_keys_deleted"],
                oauth_clients_deleted=cred_cleanup["oauth_clients_deleted"],
                oauth_tokens_revoked=cred_cleanup["oauth_tokens_revoked"],
                external_keys_deleted=cred_cleanup["external_keys_deleted"],
                memories_transferred=memory_transfer["memories_transferred"],
                contexts_transferred=context_transfer["contexts_transferred"],
                resource_tokens_transferred=resource_token_transfer["resource_tokens_transferred"],
                invitations_deleted=invitation_cleanup["invitations_deleted"],
            )

        except ValidationError:
            # Re-raise validation errors (owner removal, role escalation)
            await self.db.rollback()
            raise
        except Exception as e:
            # Catch all other errors (DB errors, network issues, etc.)
            await self.db.rollback()
            logger.error(
                "failed_to_remove_member",
                workspace_id=str(workspace_id),
                user_id=user_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    # ========================================================================
    # Workspace Statistics
    # ========================================================================

    async def get_workspace_stats(self, workspace_id: UUID) -> dict[str, Any]:
        """Get workspace statistics.

        Args:
            workspace_id: Workspace ID

        Returns:
            Dict with stats:
                - total_memories: Total memories across all contexts
                - context_count: Number of contexts
                - member_count: Number of members
        """
        # Get context count
        context_count_stmt = select(func.count(Context.id)).where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
        )
        context_result = await self.db.execute(context_count_stmt)
        context_count = context_result.scalar() or 0

        # Get member count
        member_count_stmt = select(func.count(WorkspaceMember.id)).where(
            WorkspaceMember.workspace_id == workspace_id
        )
        member_result = await self.db.execute(member_count_stmt)
        member_count = member_result.scalar() or 0

        # Get all member user_ids
        members_stmt = select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace_id
        )
        members_result = await self.db.execute(members_stmt)
        member_ids = [row[0] for row in members_result.all()]

        # Aggregate memories across all workspace members
        if member_ids:
            memory_count_stmt = select(func.count(Memory.id)).where(
                Memory.user_id.in_(member_ids),
                Memory.deleted_at.is_(None),
            )

            memory_count_result = await self.db.execute(memory_count_stmt)
            total_memories = memory_count_result.scalar() or 0
        else:
            total_memories = 0

        return {
            "context_count": context_count,
            "member_count": member_count,
            "total_memories": total_memories,
        }

    async def get_context_stats(self, workspace_id: UUID) -> dict[str, Any]:
        """Get per-context statistics for workspace.

        Issue #249: Context usage overview for workspace page.

        Args:
            workspace_id: Workspace ID

        Returns:
            Dict with context-level statistics:
                - contexts: List of context stats items
                - total_contexts: Total number of contexts
                - workspace_totals: Aggregate totals
        """
        # Query contexts with aggregated memory stats
        # Single Collection Migration: Use workspace_id/context_id instead of collection_name
        # Use LEFT JOIN to include contexts with no memories
        stmt = (
            select(
                Context.id,
                Context.name,
                Context.display_name,
                func.count(Memory.id).label("memory_count"),
                func.max(Memory.created_at).label("last_activity"),
            )
            .outerjoin(
                Memory,
                and_(
                    Memory.workspace_id == Context.workspace_id,
                    Memory.context_id == Context.id,
                    Memory.deleted_at.is_(None),
                ),
            )
            .where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
            .group_by(Context.id, Context.name, Context.display_name)
            .order_by(func.count(Memory.id).desc())
        )

        result = await self.db.execute(stmt)
        context_rows = result.all()

        # Get member count for each context
        # Note: All workspace members have access to all contexts by default
        # unless Issue #234 context access restriction is applied
        member_count_stmt = select(func.count(WorkspaceMember.id)).where(
            WorkspaceMember.workspace_id == workspace_id
        )
        member_result = await self.db.execute(member_count_stmt)
        org_member_count = member_result.scalar() or 0

        # Get usage statistics for each context (Issue #249)
        # Aggregate API calls, active users, and response times
        from datetime import timedelta

        today = utcnow().date()
        week_ago = today - timedelta(days=7)

        usage_stats_stmt = (
            select(
                UsageStats.context_id,
                func.count(UsageStats.id).label("total_calls"),
                func.count(func.distinct(UsageStats.user_id)).label("active_users"),
                func.avg(UsageStats.response_time_ms).label("avg_response_time"),
            )
            .where(
                UsageStats.workspace_id == workspace_id,
                UsageStats.date >= week_ago,
                UsageStats.context_id.isnot(None),
            )
            .group_by(UsageStats.context_id)
        )
        usage_result = await self.db.execute(usage_stats_stmt)
        usage_by_context = {
            str(row.context_id): {
                "api_calls_week": row.total_calls,
                "active_users_week": row.active_users,
                "avg_response_time_ms": round(row.avg_response_time or 0, 1),
            }
            for row in usage_result.all()
        }

        # Build context stats list
        contexts = []
        total_memories = 0

        for row in context_rows:
            memory_count = row.memory_count or 0

            context_id_str = str(row.id)
            usage_stats = usage_by_context.get(
                context_id_str,
                {
                    "api_calls_week": 0,
                    "active_users_week": 0,
                    "avg_response_time_ms": 0,
                },
            )

            contexts.append(
                {
                    "context_id": context_id_str,
                    "context_name": row.display_name or row.name,
                    "memory_count": memory_count,
                    "last_activity": row.last_activity,
                    "member_count": org_member_count,  # All members have access
                    "api_calls_week": usage_stats["api_calls_week"],
                    "active_users_week": usage_stats["active_users_week"],
                    "avg_response_time_ms": usage_stats["avg_response_time_ms"],
                }
            )

            total_memories += memory_count

        return {
            "contexts": contexts,
            "total_contexts": len(contexts),
            "workspace_totals": {
                "memory_count": total_memories,
            },
        }

    async def get_context_usage_timeline(
        self, workspace_id: UUID, context_id: UUID, days: int = 7
    ) -> dict[str, Any]:
        """Get time-series usage statistics for a context.

        Issue #249: Context activity timeline with daily API call counts.

        Args:
            workspace_id: Workspace ID
            context_id: Context ID
            days: Number of days to include (default: 7)

        Returns:
            Dict with daily usage data
        """
        from datetime import timedelta

        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days - 1)

        # Get context info
        context_result = await self.db.execute(
            select(Context).where(Context.id == context_id, Context.workspace_id == workspace_id)
        )
        context = context_result.scalar_one_or_none()
        if not context:
            raise NotFoundException(f"Context {context_id} not found")

        # Aggregate daily usage
        daily_stats_stmt = (
            select(
                UsageStats.date,
                func.count(UsageStats.id).label("api_calls"),
                func.count(func.distinct(UsageStats.user_id)).label("unique_users"),
            )
            .where(
                UsageStats.context_id == context_id,
                UsageStats.workspace_id == workspace_id,
                UsageStats.date >= start_date,
                UsageStats.date <= end_date,
            )
            .group_by(UsageStats.date)
            .order_by(UsageStats.date)
        )

        result = await self.db.execute(daily_stats_stmt)
        daily_rows = result.all()

        # Build complete timeline (fill missing dates with zeros)
        daily_usage = []
        current_date = start_date
        stats_by_date = {row.date: row for row in daily_rows}

        while current_date <= end_date:
            row = stats_by_date.get(current_date)
            daily_usage.append(
                {
                    "date": current_date.isoformat(),
                    "api_calls": row.api_calls if row else 0,
                    "unique_users": row.unique_users if row else 0,
                }
            )
            current_date += timedelta(days=1)

        total_calls = sum(item["api_calls"] for item in daily_usage)

        return {
            "context_id": str(context_id),
            "context_name": context.display_name or context.name,
            "daily_usage": daily_usage,
            "total_calls": total_calls,
        }

    async def get_context_user_activity(
        self, workspace_id: UUID, context_id: UUID, days: int = 7
    ) -> dict[str, Any]:
        """Get per-user activity statistics for a context.

        Issue #249: User activity breakdown showing who is using the context.

        Args:
            workspace_id: Workspace ID
            context_id: Context ID
            days: Number of days to include (default: 7)

        Returns:
            Dict with per-user activity data
        """
        from datetime import timedelta

        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days - 1)

        # Get context info
        context_result = await self.db.execute(
            select(Context).where(Context.id == context_id, Context.workspace_id == workspace_id)
        )
        context = context_result.scalar_one_or_none()
        if not context:
            raise NotFoundException(f"Context {context_id} not found")

        # Aggregate per-user activity
        user_stats_stmt = (
            select(
                UsageStats.user_id,
                func.count(UsageStats.id).label("api_calls"),
                func.max(UsageStats.created_at).label("last_activity"),
            )
            .where(
                UsageStats.context_id == context_id,
                UsageStats.workspace_id == workspace_id,
                UsageStats.date >= start_date,
                UsageStats.date <= end_date,
            )
            .group_by(UsageStats.user_id)
            .order_by(func.count(UsageStats.id).desc())
        )

        result = await self.db.execute(user_stats_stmt)
        user_rows = result.all()

        # Fetch user details
        from models.auth import User

        # Critical Fix: Avoid N+1 query - fetch all users in single query
        user_ids = [row.user_id for row in user_rows]
        if user_ids:
            user_result = await self.db.execute(select(User).where(User.user_id.in_(user_ids)))
            users_dict = {u.user_id: u for u in user_result.scalars().all()}
        else:
            users_dict = {}

        users = []
        for row in user_rows:
            user = users_dict.get(row.user_id)
            users.append(
                {
                    "user_id": row.user_id,
                    "user_name": user.name if user else None,
                    "user_email": user.email if user else None,
                    "api_calls": row.api_calls,
                    "last_activity": row.last_activity,
                }
            )

        return {
            "context_id": str(context_id),
            "context_name": context.display_name or context.name,
            "users": users,
            "total_users": len(users),
        }

    async def get_workspace_memory_timeline(
        self, workspace_id: UUID, days: int = 30, context_id: UUID | None = None
    ) -> dict[str, Any]:
        """Get time-series memory creation statistics for workspace.

        Issue #275 Task 6: Memory count timeline for visualization.
        Issue #134: Optional context_id filter.

        Args:
            workspace_id: Workspace ID
            days: Number of days to include (default: 30, max: 90)
            context_id: Optional context ID to filter by

        Returns:
            Dict with daily memory counts and period info
        """
        from datetime import datetime, time, timedelta

        # Calculate date range
        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days - 1)

        # Convert to datetime for efficient index usage
        start_datetime = datetime.combine(start_date, time.min)
        end_datetime = datetime.combine(end_date, time.max)

        # Get workspace
        workspace_result = await self.db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()
        if not workspace:
            raise NotFoundException(f"Workspace {workspace_id} not found")

        # Workspace boundary via the workspace's own contexts (Issue #822).
        # Scoping by membership (``Memory.user_id.in_(member_ids)``) leaked
        # other workspaces' memories for users who belong to multiple
        # workspaces. Scope by ``Context.workspace_id`` instead — matching the
        # context-scoped stats path in ``api/routes/workspace.py`` (the
        # /workspace/stats endpoint filters ``Context.workspace_id``). We scope
        # on ``Context`` rather than ``Memory.workspace_id`` because the latter
        # is nullable and would under-count legacy rows. As a subquery (not a
        # materialized id list) this stays a single round-trip and lets the DB
        # use the ``Context`` index regardless of how many contexts exist.
        workspace_contexts = select(Context.id).where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
        )

        # An optional ``context_id`` filter must belong to this workspace —
        # otherwise a foreign context_id could surface another workspace's
        # memories. Reuse the canonical boundary check rather than re-deriving
        # it here (single source of truth, see analysis.query_service).
        if context_id is not None:
            from services.analysis.query_service import verify_context_in_workspace

            in_workspace = await verify_context_in_workspace(
                self.db, workspace_id=workspace_id, context_id=context_id
            )
            memory_scope = Memory.context_id == context_id if in_workspace else None
        else:
            memory_scope = Memory.context_id.in_(workspace_contexts)

        # Aggregate daily memory creation counts. ``memory_scope`` is None only
        # when a foreign context_id was supplied — return an empty (zero-filled)
        # timeline in that case.
        if memory_scope is not None:
            # Issue #275 Performance: Use datetime range filter (idx_created_at)
            # instead of func.date() which prevents index usage
            conditions = [
                memory_scope,
                Memory.deleted_at.is_(None),  # CRITICAL: Exclude soft-deleted
                Memory.created_at >= start_datetime,  # ✅ Uses idx_created_at
                Memory.created_at <= end_datetime,
            ]

            daily_counts_stmt = (
                select(
                    func.date(Memory.created_at).label("date"),
                    func.count(Memory.id).label("memory_count"),
                )
                .where(*conditions)
                .group_by(func.date(Memory.created_at))
                .order_by(func.date(Memory.created_at))
            )
            result = await self.db.execute(daily_counts_stmt)
            daily_rows = result.all()
        else:
            daily_rows = []

        # Zero-fill missing dates for continuous timeline
        daily_counts = []
        current_date = start_date
        counts_by_date = {row.date: row.memory_count for row in daily_rows}

        while current_date <= end_date:
            daily_counts.append(
                {
                    "date": current_date.isoformat(),
                    "count": counts_by_date.get(current_date, 0),
                }
            )
            current_date += timedelta(days=1)

        return {
            "workspace_id": str(workspace_id),
            "workspace_name": workspace.name,
            "daily_counts": daily_counts,
            "memories_created_in_period": sum(item["count"] for item in daily_counts),
            "period_start": start_date.isoformat(),
            "period_end": end_date.isoformat(),
        }

    async def get_context_public_api_stats(
        self, workspace_id: UUID, context_id: UUID, days: int = 7
    ) -> dict[str, Any]:
        """Get Public API usage statistics for a public context.

        Issue #265: Resource Ingest API and Public Search API stats.

        Args:
            workspace_id: Workspace ID
            context_id: Context ID
            days: Number of days to include (default: 7, max: 30)

        Returns:
            Dict with resource_ingest and public_search stats

        Raises:
            NotFoundException: If context not found
            ValidationError: If context is not public
        """
        from datetime import timedelta

        from sqlalchemy import and_, case, func, select

        from models.auth import Context
        from models.resource import ResourceToken
        from utils.exceptions import NotFoundException, ValidationError

        # Validate context exists and is public. Previously this called
        # self.get_context(...) which did not exist on WorkspaceService —
        # the route was raising AttributeError at runtime. Query directly
        # against the workspace-scoped Context row to keep the boundary check
        # in one statement without pulling in ContextService here.
        ctx_result = await self.db.execute(
            select(Context).where(
                Context.id == context_id,
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
        context = ctx_result.scalar_one_or_none()
        if context is None:
            # NotFoundException builds "<resource> not found[: <id>]" — workspace_id
            # is also in the URL path so we keep the resource label short.
            raise NotFoundException("Context", str(context_id))
        if not context.is_public:
            raise ValidationError("Context is not public")

        # Calculate date range
        days = max(1, min(days, 30))  # P2: Ensure days >= 1, max 30

        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days - 1)

        # ============================================================================
        # Resource Ingest API Statistics (Optimized with CTE)
        # ============================================================================

        # P2: SQL Optimization - Single query with subqueries instead of 4 separate SELECTs
        ingest_base_filter = and_(
            UsageStats.context_id == context_id,
            UsageStats.workspace_id == workspace_id,
            UsageStats.endpoint.like("/api/v1/resources/%/events%"),
        )

        # Get aggregate stats in one query
        ingest_stats_result = await self.db.execute(
            select(
                func.count(UsageStats.id).label("total_events"),
                func.sum(
                    case(
                        (
                            and_(
                                UsageStats.date >= start_date,
                                UsageStats.date <= end_date,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("last_n_days_events"),
            ).where(ingest_base_filter)
        )
        ingest_stats = ingest_stats_result.one()
        total_events = ingest_stats.total_events or 0
        last_n_days_events = ingest_stats.last_n_days_events or 0
        avg_per_day = last_n_days_events / days if days > 0 else 0.0

        # Active tokens count (separate query - different table)
        if context.resource_id:
            token_result = await self.db.execute(
                select(func.count(ResourceToken.id)).where(
                    ResourceToken.resource_id == context.resource_id,
                    ResourceToken.is_active == True,  # noqa: E712
                )
            )
            active_tokens = token_result.scalar() or 0
        else:
            active_tokens = 0

        # Timeline (daily aggregation)
        timeline_result = await self.db.execute(
            select(UsageStats.date, func.count(UsageStats.id).label("event_count"))
            .where(
                ingest_base_filter,
                UsageStats.date >= start_date,
                UsageStats.date <= end_date,
            )
            .group_by(UsageStats.date)
            .order_by(UsageStats.date)
        )

        # Fill missing dates with zeros (helper: fill_timeline)
        events_by_date = {row.date: row.event_count for row in timeline_result.all()}
        events_timeline = [
            {"date": day.isoformat(), "count": events_by_date.get(day, 0)}
            for day in (
                start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)
            )
        ]

        # ============================================================================
        # Public Search API Statistics (Optimized with CTE)
        # ============================================================================

        # P2: SQL Optimization - Combine total and breakdown queries
        search_base_filter = and_(
            UsageStats.context_id == context_id,
            UsageStats.workspace_id == workspace_id,
            UsageStats.endpoint.like("/api/v1/public/%/search"),
        )

        # Get aggregate stats in one query
        search_stats_result = await self.db.execute(
            select(
                func.count(UsageStats.id).label("total_all_time"),
                func.sum(
                    case(
                        (
                            and_(
                                UsageStats.date >= start_date,
                                UsageStats.date <= end_date,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("total_last_n"),
                func.sum(
                    case(
                        (
                            and_(
                                UsageStats.date >= start_date,
                                UsageStats.date <= end_date,
                                UsageStats.user_id == ANONYMOUS_USER_ID,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("anonymous"),
                func.sum(
                    case(
                        (
                            and_(
                                UsageStats.date >= start_date,
                                UsageStats.date <= end_date,
                                UsageStats.user_id != ANONYMOUS_USER_ID,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("authenticated"),
            ).where(search_base_filter)
        )
        search_stats = search_stats_result.one()
        total_searches = search_stats.total_all_time or 0
        last_n_days_searches = search_stats.total_last_n or 0
        anonymous_searches = search_stats.anonymous or 0
        authenticated_searches = search_stats.authenticated or 0

        # Timeline with anonymous/authenticated split
        search_timeline_result = await self.db.execute(
            select(
                UsageStats.date,
                func.count(UsageStats.id).label("total"),
                func.count(case((UsageStats.user_id == ANONYMOUS_USER_ID, 1), else_=None)).label(
                    "anonymous"
                ),
                func.count(case((UsageStats.user_id != ANONYMOUS_USER_ID, 1), else_=None)).label(
                    "authenticated"
                ),
            )
            .where(
                search_base_filter,
                UsageStats.date >= start_date,
                UsageStats.date <= end_date,
            )
            .group_by(UsageStats.date)
            .order_by(UsageStats.date)
        )

        # Fill missing dates with zeros (helper: fill_timeline)
        searches_by_date = {
            row.date: {
                "total": row.total,
                "anonymous": row.anonymous,
                "authenticated": row.authenticated,
            }
            for row in search_timeline_result.all()
        }
        default_stats = {"total": 0, "anonymous": 0, "authenticated": 0}
        searches_timeline = [
            {"date": day.isoformat(), **searches_by_date.get(day, default_stats)}
            for day in (
                start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)
            )
        ]

        return {
            "resource_ingest": {
                "total_events": total_events,
                "last_n_days": last_n_days_events,
                "avg_per_day": round(avg_per_day, 1),
                "active_tokens": active_tokens,
                "timeline": events_timeline,
            },
            "public_search": {
                "total_searches": total_searches,
                "last_n_days": last_n_days_searches,
                "anonymous": anonymous_searches,
                "authenticated": authenticated_searches,
                "timeline": searches_timeline,
            },
        }

    # ========================================================================
    # Utility Methods
    # ========================================================================

    @staticmethod
    def validate_role(role: str) -> None:
        """Validate workspace role.

        Args:
            role: Role to validate

        Raises:
            ValidationError: If role is invalid
        """
        valid_roles: frozenset[WorkspaceRole] = frozenset(WorkspaceRole)
        if role not in valid_roles:
            valid_values = ", ".join(r.value for r in WorkspaceRole)
            raise ValidationError(f"Invalid role: {role}. Must be one of: {valid_values}")

    async def create_personal_workspace(
        self,
        user_id: str,
        user_name: str,
        openai_api_key: str,
    ) -> Workspace:
        """Create personal workspace for user.

        Issue #146: OpenAI API key is required (stored in DB, no env var fallback)
        Issue #276: Slug removed.

        Args:
            user_id: User ID
            user_name: User display name
            openai_api_key: OpenAI API key (required)

        Returns:
            Created workspace

        Raises:
            ValidationError: If workspace creation fails
        """
        # Create workspace with OpenAI key
        workspace = await self.create_workspace(
            name=f"{user_name}'s Workspace",
            owner_user_id=user_id,
            description="Personal workspace (auto-created)",
            openai_api_key=openai_api_key,
        )

        logger.info(f"Created personal workspace for user: {user_id}")
        return workspace

    # ========================================================================
    # Statistics Methods (Issue #204)
    # ========================================================================

    async def get_collection_memory_stats(
        self,
        user_id: str,
        contexts: list[Context],
        is_workspace_owner: bool = False,
    ) -> dict[str, tuple[int, int]]:
        """Get memory statistics for multiple contexts with privacy-aware queries.

        Single Collection Migration: Returns stats by context_id instead of collection_name.

        This method executes different queries based on context privacy:
        1. Private contexts (owner): Count ALL members' memories
        2. Private contexts (member): Count only user's own memories
        3. Shared contexts: Count all members' memories

        Args:
            user_id: User ID (for private context filtering)
            contexts: List of Context objects to get stats for
            is_workspace_owner: If True, count all memories in all contexts (owner view)

        Returns:
            Dict mapping context_id (str) to (memory_count, storage_bytes)

        Example:
            >>> # Owner view: all memories
            >>> stats = await service.get_collection_memory_stats(user_id, contexts, is_workspace_owner=True)
            >>> # Member view: only accessible memories
            >>> stats = await service.get_collection_memory_stats(user_id, contexts, is_workspace_owner=False)
        """
        # Separate private and shared contexts
        if is_workspace_owner:
            # Owner: All private contexts (count all members' memories)
            private_context_ids = [c.id for c in contexts if c.is_private]
        else:
            # Member: Only own private contexts
            private_context_ids = [
                c.id for c in contexts if c.is_private and c.created_by == user_id
            ]

        shared_context_ids = [
            c.id
            for c in contexts
            if not c.is_private  # Shared contexts (all members)
        ]

        stats_by_context: dict[str, tuple[int, int]] = {}

        # Issue #65: Single query for all contexts (was 2 sequential queries)
        all_context_ids = private_context_ids + shared_context_ids
        if all_context_ids:
            conditions = [
                Memory.context_id.in_(all_context_ids),
                Memory.deleted_at.is_(None),
            ]
            # Non-owner members can only count their own memories in private contexts
            if not is_workspace_owner and private_context_ids:
                conditions.append(
                    or_(
                        Memory.context_id.in_(shared_context_ids),
                        and_(
                            Memory.context_id.in_(private_context_ids),
                            Memory.user_id == user_id,
                        ),
                    )
                )

            result = await self.db.execute(
                select(
                    Memory.context_id,
                    func.count(Memory.id).label("memory_count"),
                )
                .where(*conditions)
                .group_by(Memory.context_id)
            )
            for row in result.all():
                stats_by_context[str(row.context_id)] = (row.memory_count, 0)

        logger.debug(
            "context_memory_stats_retrieved",
            user_id=user_id,
            private_count=len(private_context_ids),
            shared_count=len(shared_context_ids),
            total_stats=len(stats_by_context),
        )

        return stats_by_context
