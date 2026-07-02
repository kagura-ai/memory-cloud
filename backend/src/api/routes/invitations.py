"""Workspace Invitation API Routes.

Issue #165: Team Collaboration Features - Workspace Invitation System

Provides REST API endpoints for managing workspace invitations:
- Create invitation (owner/admin only)
- List invitations (owner/admin only)
- Delete invitation (owner/admin only)
- Accept invitation (authenticated users)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser, get_current_user
from auth.programmatic_workspace_auth import authorize_workspace_management
from auth.workspace_roles import WorkspaceRole
from db.base import get_db
from models.auth import Workspace, WorkspaceInvitation, WorkspaceMember
from models.schemas import (
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    PendingInvitationItem,
    PendingInvitationsResponse,
    WorkspaceInvitationCreate,
    WorkspaceInvitationResponse,
)
from services.invitation_service import InvitationService, build_invitation_url
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["invitations"])

# ``build_invitation_url`` now lives in ``services.invitation_service`` so the
# API response and the invitation email (Issue #654) share one URL builder and
# cannot drift. Re-exported here for backward compatibility with importers of
# ``api.routes.invitations.build_invitation_url``.
__all__ = ["router", "build_invitation_url"]


@router.post(
    "/workspaces/{workspace_id}/invitations",
    response_model=WorkspaceInvitationResponse,
    summary="Create workspace invitation",
    description="Create an invitation to join the workspace. Requires owner or admin role.",
)
async def create_invitation(
    workspace_id: UUID,
    request: WorkspaceInvitationCreate,
    current_user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceInvitationResponse:
    """Create workspace invitation.

    Permission: Requires workspace owner or admin role.

    Args:
        workspace_id: Workspace ID
        request: Invitation creation request
        current_user: Authenticated user
        db: Database session

    Returns:
        Created invitation with URL

    Raises:
        HTTPException 403: Insufficient permissions
        HTTPException 400: Validation error (duplicate invitation, etc.)
        HTTPException 404: Workspace not found
    """
    user_id = current_user.get("user_id")

    try:
        # Issue #1164: session admin+ (unchanged) OR workspace-owner API key;
        # OAuth 403. role=owner is already rejected at the schema layer (#1166),
        # so no programmatic owner-invite branch is needed here.
        await authorize_workspace_management(
            current_user, workspace_id, db, session_required_role=WorkspaceRole.ADMIN
        )

        # Check plan tier (Issue #165 - invitations require Pro plan)
        from sqlalchemy import select

        from models.auth import Workspace

        stmt = select(Workspace).where(Workspace.id == workspace_id)
        result = await db.execute(stmt)
        workspace = result.scalar_one()

        if workspace.plan_name in ["free", "basic"]:
            raise HTTPException(
                status_code=403,
                detail="Team invitations require Pro plan. Upgrade your plan to invite team members.",
            )

        # Check member quota (Issue #229)
        from services.quota_service import QuotaService

        quota_service = QuotaService(db)
        can_invite, quota_error = await quota_service.check_member_quota(
            workspace_id, raise_on_exceeded=False
        )

        if not can_invite:
            raise HTTPException(
                status_code=429,  # Quota Exceeded
                detail=quota_error,
            )

        # Create invitation
        invitation_service = InvitationService(db)
        # Migration 042: Convert string UUIDs to UUID objects
        allowed_context_ids = None
        if request.allowed_context_ids:
            allowed_context_ids = [UUID(ctx_id) for ctx_id in request.allowed_context_ids]

        invitation = await invitation_service.create_invitation(
            workspace_id=workspace_id,
            invited_by=user_id,
            role=request.role,
            email=request.email,
            expires_in_days=request.expires_in_days,
            allowed_context_ids=allowed_context_ids,  # Migration 042
        )

        await db.commit()

        # Build response
        response = WorkspaceInvitationResponse(
            id=invitation.id,
            workspace_id=invitation.workspace_id,
            token=invitation.token,
            email=invitation.email,
            role=invitation.role,
            invited_by=invitation.invited_by,
            expires_at=invitation.expires_at,
            accepted_at=invitation.accepted_at,
            accepted_by=invitation.accepted_by,
            created_at=invitation.created_at,
            invitation_url=build_invitation_url(invitation.token),
            is_expired=invitation.is_expired(),
            is_accepted=invitation.is_accepted(),
            allowed_context_ids=(
                [str(ctx_id) for ctx_id in invitation.allowed_context_ids]
                if invitation.allowed_context_ids
                else None
            ),
        )

        logger.info(
            f"User {user_id} created invitation for workspace={workspace_id} "
            f"role={request.role} email={request.email or 'any'}"
        )

        return response

    except ValidationError as e:
        logger.warning(f"Validation error creating invitation: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NotFoundException as e:
        logger.warning(f"Workspace not found: {e}")
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get(
    "/workspaces/{workspace_id}/invitations",
    response_model=list[WorkspaceInvitationResponse],
    summary="List workspace invitations",
    description="List all invitations for the workspace. Requires owner or admin role.",
)
async def list_invitations(
    workspace_id: UUID,
    current_user: APIKeyOrSessionUser,
    include_accepted: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceInvitationResponse]:
    """List workspace invitations.

    Issue #1164: session admin+ (unchanged) OR workspace-owner API key.
    For PROGRAMMATIC principals the live ``token`` / ``invitation_url``
    (bearer join-credentials) are omitted — the token is only ever returned
    in the POST create response — so CI logs never become a workspace-join
    credential dump.

    Raises:
        HTTPException 403: Insufficient permissions.
    """
    principal = await authorize_workspace_management(
        current_user, workspace_id, db, session_required_role=WorkspaceRole.ADMIN
    )
    expose_token = principal.kind == "session"

    invitation_service = InvitationService(db)
    invitations = await invitation_service.list_invitations(
        workspace_id, include_accepted=include_accepted
    )

    return [
        WorkspaceInvitationResponse(
            id=inv.id,
            workspace_id=inv.workspace_id,
            token=inv.token if expose_token else None,
            email=inv.email,
            role=inv.role,
            invited_by=inv.invited_by,
            expires_at=inv.expires_at,
            accepted_at=inv.accepted_at,
            accepted_by=inv.accepted_by,
            created_at=inv.created_at,
            invitation_url=build_invitation_url(inv.token) if expose_token else None,
            is_expired=inv.is_expired(),
            is_accepted=inv.is_accepted(),
            allowed_context_ids=(
                [str(ctx_id) for ctx_id in inv.allowed_context_ids]
                if inv.allowed_context_ids
                else None
            ),
        )
        for inv in invitations
    ]


@router.delete(
    "/workspaces/{workspace_id}/invitations/{invitation_id}",
    summary="Delete workspace invitation",
    description="Delete (revoke) an invitation. Requires owner or admin role.",
)
async def delete_invitation(
    workspace_id: UUID,
    invitation_id: int,
    current_user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete (revoke) invitation.

    Issue #1164: session admin+ (unchanged) OR workspace-owner API key.

    Raises:
        HTTPException 403: Insufficient permissions
        HTTPException 404: Invitation not found
    """
    user_id = current_user.get("user_id")

    try:
        await authorize_workspace_management(
            current_user, workspace_id, db, session_required_role=WorkspaceRole.ADMIN
        )

        invitation_service = InvitationService(db)
        await invitation_service.delete_invitation(invitation_id, workspace_id)

        await db.commit()

        logger.info(
            f"User {user_id} deleted invitation id={invitation_id} workspace={workspace_id}"
        )

        return {"success": True}

    except NotFoundException as e:
        logger.warning(f"Invitation not found: {e}")
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post(
    "/invitations/accept",
    response_model=AcceptInvitationResponse,
    summary="Accept workspace invitation",
    description="Accept an invitation to join an workspace. Public endpoint (authenticated users only).",
)
async def accept_invitation(
    request: AcceptInvitationRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AcceptInvitationResponse:
    """Accept workspace invitation.

    Public endpoint (authenticated users only). Validates invitation token,
    checks expiration and email restrictions, and creates workspace membership.

    Args:
        request: Accept invitation request
        current_user: Authenticated user
        db: Database session

    Returns:
        Success response with workspace and member info

    Raises:
        HTTPException 400: Validation error (expired, email mismatch, already member, etc.)
        HTTPException 404: Invitation not found
    """
    user_id = current_user.get("user_id")
    user_email = current_user.get("email")

    try:
        invitation_service = InvitationService(db)

        # Accept invitation
        workspace, member = await invitation_service.accept_invitation(
            token=request.token,
            user_id=user_id,
            user_email=user_email,
        )

        # Note: Service already commits (via create_workspace if personal workspace created)
        # Member object may be detached, so use current time for joined_at

        await db.commit()

        logger.info(
            f"User {user_id} accepted invitation to workspace={workspace.id} as {member.role}"
        )

        # Build response (use current time since we just accepted)
        return AcceptInvitationResponse(
            success=True,
            workspace={
                "id": str(workspace.id),
                "name": workspace.name,
                "plan_name": workspace.plan_name,
            },
            member={
                "user_id": member.user_id,
                "role": member.role,
                "joined_at": utcnow().isoformat(),  # Use current time
            },
        )

    except ValidationError as e:
        logger.warning(f"Validation error accepting invitation: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NotFoundException as e:
        logger.warning(f"Invitation not found: {e}")
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get(
    "/invitations/pending",
    response_model=PendingInvitationsResponse,
    summary="Get pending invitations for current user",
    description="Get all pending (non-expired) invitations for the authenticated user's email.",
)
async def get_pending_invitations(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PendingInvitationsResponse:
    """Get pending invitations for current user.

    Issue #179: In-app invitation notifications.

    Returns invitations where:
    - User's email matches (case-insensitive) OR no email restriction
    - Not yet accepted (accepted_at IS NULL)
    - Not expired

    Args:
        current_user: Authenticated user
        db: Database session

    Returns:
        List of pending invitations with workspace info
    """
    user_email = current_user.get("email")

    invitation_service = InvitationService(db)
    invitations = await invitation_service.get_pending_invitations_for_email(user_email)

    # Build response with workspace info
    from sqlalchemy import select

    # Critical Fix: Avoid N+1 query - batch fetch all workspaces
    workspace_ids = {inv.workspace_id for inv in invitations}
    if workspace_ids:
        workspaces_result = await db.execute(
            select(Workspace).where(Workspace.id.in_(workspace_ids))
        )
        workspaces_by_id = {w.id: w for w in workspaces_result.scalars()}
    else:
        workspaces_by_id = {}

    results = []
    for inv in invitations:
        # Get workspace name from pre-fetched dict
        workspace = workspaces_by_id.get(inv.workspace_id)
        if not workspace:
            continue  # Skip if workspace was deleted

        results.append(
            PendingInvitationItem(
                id=inv.id,
                workspace_id=str(inv.workspace_id),
                workspace_name=workspace.name,
                role=inv.role,
                invited_by=inv.invited_by,
                expires_at=inv.expires_at,
                created_at=inv.created_at,
                token=inv.token,
                invitation_url=build_invitation_url(inv.token),
            )
        )

    logger.info(f"User {current_user.get('user_id')} retrieved {len(results)} pending invitations")

    return PendingInvitationsResponse(
        pending_invitations=results,
        count=len(results),
    )


@router.get(
    "/invitations/{token}",
    response_model=dict,
    summary="Get invitation info",
    description="Get invitation information by token. Does not require authentication (for preview before login).",
)
async def get_invitation_info(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get invitation information by token.

    Public endpoint (no authentication required). Allows users to see invitation
    details before logging in.

    Args:
        token: Invitation token
        db: Database session

    Returns:
        Invitation info (workspace name, role, expiration)

    Raises:
        HTTPException 404: Invitation not found
        HTTPException 410: Invitation expired or already accepted
    """
    try:
        invitation_service = InvitationService(db)
        invitation = await invitation_service.get_invitation(token)

        # Check if invitation is usable
        if invitation.is_accepted():
            raise HTTPException(status_code=410, detail="This invitation has already been accepted")

        if invitation.is_expired():
            raise HTTPException(status_code=410, detail="This invitation has expired")

        # Get workspace info (don't expose sensitive data)
        from sqlalchemy import select

        from models.auth import Workspace

        stmt = select(Workspace).where(Workspace.id == invitation.workspace_id)
        result = await db.execute(stmt)
        workspace = result.scalar_one()

        return {
            "workspace_name": workspace.name,
            "role": invitation.role,
            "expires_at": to_utc_iso(invitation.expires_at),
            "email_restricted": invitation.email is not None,
        }

    except NotFoundException as e:
        logger.warning(f"Invitation not found: {e}")
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get(
    "/workspaces/{workspace_id}/member-quota",
    summary="Get member quota status",
    description="Get current member count and limit for workspace. Requires member access.",
)
async def get_member_quota(
    workspace_id: UUID,
    current_user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get member quota status for frontend display.

    Returns current members, pending invitations, and available seats.

    Issue #229: Display seat usage in UI.
    Issue #1164: session member+ (unchanged) OR workspace-owner API key.

    Raises:
        HTTPException: 403 if user lacks access, 404 if workspace not found
    """
    # Issue #1164: session member+ gate; API key requires owner; OAuth 403.
    await authorize_workspace_management(
        current_user, workspace_id, db, session_required_role=WorkspaceRole.MEMBER
    )

    # Count current members
    member_count_result = await db.execute(
        select(func.count(WorkspaceMember.id)).where(WorkspaceMember.workspace_id == workspace_id)
    )
    member_count = member_count_result.scalar() or 0

    # Count pending invitations (not accepted, not expired)
    pending_count_result = await db.execute(
        select(func.count(WorkspaceInvitation.id)).where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.accepted_at.is_(None),
            or_(
                WorkspaceInvitation.expires_at.is_(None),
                WorkspaceInvitation.expires_at > utcnow(),
            ),
        )
    )
    pending_count = pending_count_result.scalar() or 0

    # Calculate quota stats using EffectiveQuotaService
    from services.effective_quota_service import EffectiveQuotaService

    effective = await EffectiveQuotaService(db).get_effective_quotas(workspace_id)
    max_members = effective["max_members"]
    total_used = member_count + pending_count
    available = max(0, max_members - total_used)
    percentage = (total_used / max_members * 100) if max_members > 0 else 0

    return {
        "current_members": member_count,
        "pending_invitations": pending_count,
        "total_used": total_used,
        "limit": max_members,
        "available": available,
        "percentage": round(percentage, 2),
        "can_invite": total_used < max_members,
    }
