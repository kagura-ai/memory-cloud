"""Workspace Invitation Service.

Issue #165: Team Collaboration Features - Workspace Invitation System

This service handles the core business logic for workspace invitations:
- Token generation (cryptographically secure)
- Invitation creation and validation
- Acceptance with permission checks
- Duplicate prevention
- Expiration management
"""

import secrets
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace, WorkspaceInvitation, WorkspaceMember
from utils.datetime import utcnow
from utils.exceptions import NotFoundException, QuotaExceededError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Invitation expiry presets (days)
EXPIRY_PRESETS = {
    7: timedelta(days=7),
    30: timedelta(days=30),
    90: timedelta(days=90),
    365: timedelta(days=365),
}


class InvitationService:
    """Service for managing workspace invitations.

    Provides methods for creating, retrieving, and accepting workspace
    invitations with full validation and security checks.

    Security features:
    - Cryptographically secure token generation (32 bytes)
    - Email case-insensitive matching
    - Expiration enforcement
    - Duplicate invitation prevention
    - Single-use tokens (cannot be reused after acceptance)
    """

    def __init__(self, db: AsyncSession):
        """Initialize invitation service.

        Args:
            db: Async database session
        """
        self.db = db

    async def create_invitation(
        self,
        workspace_id: UUID,
        invited_by: str,
        role: str = "member",
        email: str = None,
        expires_in_days: int | None = None,
        allowed_context_ids: list[UUID] | None = None,
    ) -> WorkspaceInvitation:
        """Create workspace invitation.

        Generates a unique invitation token and creates an invitation record.
        Email is required and must match the user's Google OAuth account.

        Args:
            workspace_id: Workspace ID
            invited_by: User ID creating invitation
            role: Role to assign (owner/admin/member/viewer)
            email: Email address (REQUIRED, must match Google OAuth account)
            expires_in_days: Days until expiration (7/30/90/365 or None=never)
            allowed_context_ids: Context IDs to allow (REQUIRED for member/viewer, minimum 1)

        Returns:
            Created invitation

        Raises:
            NotFoundException: If workspace not found
            ValidationError: If validation fails (invalid expiry, duplicate invitation, no contexts)
        """
        # Validate email is provided
        if not email or not email.strip():
            raise ValidationError(
                "Email is required for invitations. "
                "Must match the Google account used for OAuth login."
            )

        # Validate workspace exists
        stmt = select(Workspace).where(Workspace.id == workspace_id)
        result = await self.db.execute(stmt)
        workspace = result.scalar_one_or_none()
        if not workspace:
            raise NotFoundException(f"Workspace {workspace_id} not found")

        # Migration 042: Validate context selection for member/viewer
        if role in ("member", "viewer"):
            # Check that at least one shared context exists
            from models.auth import Context

            shared_contexts_stmt = select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.is_private.is_(False),
                Context.deleted_at.is_(None),
            )
            shared_count_result = await self.db.execute(shared_contexts_stmt)
            shared_count = shared_count_result.scalar() or 0

            if shared_count == 0:
                raise ValidationError(
                    "Cannot invite members: No shared contexts available. "
                    "Please create at least one shared context first."
                )

            # Validate allowed_context_ids is provided and not empty
            if not allowed_context_ids or len(allowed_context_ids) == 0:
                raise ValidationError(
                    "At least one context must be selected for member/viewer invitations."
                )

            # Validate all context IDs exist and are shared
            contexts_stmt = select(Context.id).where(
                Context.workspace_id == workspace_id,
                Context.id.in_(allowed_context_ids),
                Context.is_private.is_(False),
                Context.deleted_at.is_(None),
            )
            valid_result = await self.db.execute(contexts_stmt)
            valid_ids = {row[0] for row in valid_result.all()}

            invalid_ids = set(allowed_context_ids) - valid_ids
            if invalid_ids:
                raise ValidationError(f"Invalid or private context IDs: {list(invalid_ids)}")

        # Validate single owner constraint (Issue #165 - only one owner per workspace)
        if role == "owner":
            stmt = select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == "owner",
            )
            result = await self.db.execute(stmt)
            existing_owners = result.scalars().all()

            if existing_owners:
                raise ValidationError(
                    "Workspace already has an owner. "
                    "Only one owner is allowed per workspace. "
                    "Please transfer ownership first if you want to change the owner."
                )

        # Generate unique token (32 bytes = 43 chars base64)
        token = secrets.token_urlsafe(32)

        # Calculate expiration
        expires_at = None
        if expires_in_days:
            if expires_in_days not in EXPIRY_PRESETS:
                raise ValidationError(
                    f"Invalid expires_in_days: {expires_in_days}. "
                    f"Must be one of: {list(EXPIRY_PRESETS.keys())} or null"
                )
            expires_at = utcnow() + EXPIRY_PRESETS[expires_in_days]

        # Check for duplicate active invitation (same workspace + email)
        if email:
            # Normalize email to lowercase for duplicate check
            email_lower = email.lower()

            # Issue #217: Check if user with this email is already a member
            stmt = (
                select(WorkspaceMember)
                .join(User, WorkspaceMember.user_id == User.user_id)
                .where(
                    WorkspaceMember.workspace_id == workspace_id,
                    func.lower(User.email) == email_lower,
                )
            )
            result = await self.db.execute(stmt)
            existing_member = result.scalar_one_or_none()
            if existing_member:
                raise ValidationError(
                    f"User with email {email} is already a member of this workspace."
                )

            # Check for duplicate pending invitation
            stmt = select(WorkspaceInvitation).where(
                WorkspaceInvitation.workspace_id == workspace_id,
                func.lower(WorkspaceInvitation.email) == email_lower,
                WorkspaceInvitation.accepted_at.is_(None),
            )
            result = await self.db.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing and not existing.is_expired():
                raise ValidationError(
                    f"Active invitation already exists for {email}. "
                    f"Please revoke the existing invitation first."
                )

        # Create invitation
        invitation = WorkspaceInvitation(
            workspace_id=workspace_id,
            token=token,
            email=email,
            role=role,
            invited_by=invited_by,
            expires_at=expires_at,
            allowed_context_ids=allowed_context_ids,  # Migration 042
        )

        self.db.add(invitation)
        await self.db.flush()

        logger.info(
            f"Created invitation for workspace={workspace_id} role={role} "
            f"email={email or 'any'} expires={expires_at or 'never'}"
        )

        return invitation

    async def get_invitation(self, token: str) -> WorkspaceInvitation:
        """Get invitation by token.

        Args:
            token: Invitation token

        Returns:
            Invitation

        Raises:
            NotFoundException: If invitation not found
        """
        stmt = select(WorkspaceInvitation).where(WorkspaceInvitation.token == token)
        result = await self.db.execute(stmt)
        invitation = result.scalar_one_or_none()

        if not invitation:
            raise NotFoundException("Invitation not found or invalid")

        return invitation

    async def list_invitations(
        self,
        workspace_id: UUID,
        include_accepted: bool = False,
    ) -> list[WorkspaceInvitation]:
        """List all invitations for workspace.

        Args:
            workspace_id: Workspace ID
            include_accepted: Include accepted invitations (default: False)

        Returns:
            List of invitations, ordered by creation date (newest first)
        """
        stmt = (
            select(WorkspaceInvitation)
            .where(WorkspaceInvitation.workspace_id == workspace_id)
            .order_by(WorkspaceInvitation.created_at.desc())
        )

        if not include_accepted:
            stmt = stmt.where(WorkspaceInvitation.accepted_at.is_(None))

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def accept_invitation(
        self,
        token: str,
        user_id: str,
        user_email: str,
    ) -> tuple[Workspace, WorkspaceMember]:
        """Accept workspace invitation.

        Validates invitation (existence, expiration, email match) and creates
        workspace membership. Marks invitation as accepted.

        Args:
            token: Invitation token
            user_id: Accepting user's ID
            user_email: Accepting user's email

        Returns:
            Tuple of (Workspace, WorkspaceMember)

        Raises:
            ValidationError: If validation fails (expired, email mismatch, already member, etc.)
            NotFoundException: If invitation or workspace not found
        """
        # Get invitation
        invitation = await self.get_invitation(token)

        # Validate invitation
        if invitation.is_accepted():
            raise ValidationError("This invitation has already been accepted and cannot be reused")

        if invitation.is_expired():
            raise ValidationError("This invitation has expired. Please request a new invitation.")

        # Check email restriction (case-insensitive)
        if invitation.email:
            if invitation.email.lower() != user_email.lower():
                raise ValidationError(
                    f"This invitation is restricted to {invitation.email}. "
                    f"You are logged in as {user_email}."
                )

        # Check if user is already a member
        stmt = select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == invitation.workspace_id,
            WorkspaceMember.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        existing_member = result.scalar_one_or_none()

        if existing_member:
            raise ValidationError("You are already a member of this workspace")

        # Get workspace
        stmt = select(Workspace).where(Workspace.id == invitation.workspace_id)
        result = await self.db.execute(stmt)
        workspace = result.scalar_one_or_none()

        if not workspace:
            raise NotFoundException("Workspace not found")

        # Check member quota (Issue #229 - race condition protection)
        from services.quota_service import QuotaService

        quota_service = QuotaService(self.db)

        try:
            await quota_service.check_member_quota(invitation.workspace_id, raise_on_exceeded=True)
        except QuotaExceededError as e:
            raise ValidationError(f"Cannot accept invitation: {str(e)}") from e

        # Create membership
        joined_at_time = utcnow()  # Store value to avoid detached instance issues
        member = WorkspaceMember(
            workspace_id=invitation.workspace_id,
            user_id=user_id,
            role=invitation.role,
            invited_by=invitation.invited_by,
            invited_at=invitation.created_at,
            joined_at=joined_at_time,  # Use explicit datetime instead of func.now()
            allowed_context_ids=invitation.allowed_context_ids,  # Migration 042: Copy from invitation
        )

        self.db.add(member)

        # Mark invitation as accepted
        invitation.accepted_at = utcnow()
        invitation.accepted_by = user_id

        # Set user's current workspace to invited workspace (Issue #165)
        # CRITICAL: Always set to invited workspace, even if user already has one
        # This ensures invited users land in the correct workspace
        from models.auth import User

        stmt = select(User).where(User.user_id == user_id)
        result = await self.db.execute(stmt)
        user_record = result.scalar_one_or_none()

        if user_record:
            user_record.current_workspace_id = invitation.workspace_id
            logger.info(
                f"Set user {user_id} current_workspace_id to {invitation.workspace_id} "
                f"(invited workspace)"
            )

        # Issue #276: Do NOT auto-create personal workspace for invited users.
        # Invited users land directly in the invited workspace.
        # They can create their own workspace later via Settings/Sidebar if needed.
        logger.info(
            f"User {user_id} joined workspace {invitation.workspace_id} via invitation. "
            f"No personal workspace created (Issue #276)."
        )

        await self.db.flush()

        logger.info(
            f"User {user_id} accepted invitation to workspace={workspace.id} "
            f"as {member.role} (token={token[:8]}...)"
        )

        return workspace, member

    async def delete_invitation(
        self,
        invitation_id: int,
        workspace_id: UUID,
    ) -> None:
        """Delete (revoke) invitation.

        Args:
            invitation_id: Invitation ID
            workspace_id: Workspace ID (for validation)

        Raises:
            NotFoundException: If invitation not found
        """
        stmt = select(WorkspaceInvitation).where(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.workspace_id == workspace_id,
        )
        result = await self.db.execute(stmt)
        invitation = result.scalar_one_or_none()

        if not invitation:
            raise NotFoundException("Invitation not found")

        await self.db.delete(invitation)
        await self.db.flush()

        logger.info(
            f"Deleted invitation id={invitation_id} workspace={workspace_id} "
            f"token={invitation.token[:8]}..."
        )

    async def cleanup_expired_invitations(
        self,
        workspace_id: UUID | None = None,
    ) -> int:
        """Cleanup expired invitations.

        Removes all expired, unaccepted invitations. This is a maintenance
        operation that should be run periodically.

        Args:
            workspace_id: Optional workspace ID to limit cleanup scope

        Returns:
            Number of invitations deleted
        """
        stmt = select(WorkspaceInvitation).where(
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.expires_at < utcnow(),
        )

        if workspace_id:
            stmt = stmt.where(WorkspaceInvitation.workspace_id == workspace_id)

        result = await self.db.execute(stmt)
        expired_invitations = list(result.scalars().all())

        count = len(expired_invitations)
        for invitation in expired_invitations:
            await self.db.delete(invitation)

        if count > 0:
            await self.db.flush()
            logger.info(f"Cleaned up {count} expired invitations")

        return count

    async def get_pending_invitations_for_email(self, email: str) -> list[WorkspaceInvitation]:
        """Get pending invitations for email address.

        Issue #179: In-app invitation notifications.

        Returns all pending, non-expired invitations that match the user's email
        (case-insensitive) or have no email restriction.

        Args:
            email: User email address (case-insensitive)

        Returns:
            List of pending, non-expired invitations sorted by creation date (newest first)
        """
        from sqlalchemy import and_, or_

        stmt = (
            select(WorkspaceInvitation)
            .where(
                and_(
                    # Email match (case-insensitive) OR no email restriction
                    or_(
                        func.lower(WorkspaceInvitation.email) == email.lower(),
                        WorkspaceInvitation.email.is_(None),
                    ),
                    # Not accepted
                    WorkspaceInvitation.accepted_at.is_(None),
                    # Not expired (or no expiration)
                    or_(
                        WorkspaceInvitation.expires_at.is_(None),
                        WorkspaceInvitation.expires_at > utcnow(),
                    ),
                )
            )
            .order_by(WorkspaceInvitation.created_at.desc())
        )

        result = await self.db.execute(stmt)
        invitations = result.scalars().all()

        logger.info(f"Found {len(invitations)} pending invitations for email {email}")

        return list(invitations)
