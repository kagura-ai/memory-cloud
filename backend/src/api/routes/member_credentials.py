"""Member Credentials API Routes.

Migration 034: Member-scoped API Keys and OAuth Apps with Zero-knowledge visibility.
Issue #252: Session-only authentication (no API keys)

Endpoints:
- GET /workspaces/{workspace_id}/members/{user_id}/credentials
- POST /workspaces/{workspace_id}/members/{user_id}/credentials/api-key/hide
- POST /workspaces/{workspace_id}/members/{user_id}/credentials/api-key/regenerate
- DELETE /workspaces/{workspace_id}/members/{user_id}/credentials/api-key
- Similar for OAuth apps
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import APIKeyManager, apply_zero_knowledge_hide
from auth.dependencies import APIKeyOrSessionUser, SessionUser
from auth.programmatic_workspace_auth import (
    audit_programmatic_workspace_action,
    authorize_workspace_management,
    is_api_key_principal,
    is_oauth_principal,
    is_session_principal,
)
from auth.workspace_roles import WorkspaceRole
from db.base import get_db
from models.schemas import (
    CreateAPIKeyRequest,
    MemberAPIKeyResponse,
    MemberCredentialsResponse,
    RegenerateAPIKeyResponse,
    RegenerateOAuthSecretResponse,
)
from services.member_credentials_service import MemberCredentialsService
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import AuthorizationError, BadRequestError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/members",
    tags=["member-credentials"],
)


# ============================================================================
# Helper Functions
# ============================================================================


def _reject_oauth(user: dict, action: str) -> None:
    """Reject an OAuth bearer principal on the credential surface (#1165).

    Every ``kagura auth login`` device token carries ``memory:read
    memory:write``, so accepting OAuth here would silently turn every user's
    MCP token into a credential-management credential. One helper keeps the
    policy message consistent across the three credential endpoints.
    """
    if is_oauth_principal(user):
        raise AuthorizationError(
            message=f"OAuth bearer tokens cannot {action}. Use a workspace-owner API key."
        )


def _minted_key_response(
    new_key,
    plaintext_key: str,
    *,
    is_visible: bool,
    visibility_expires_at: str | None,
    last_used_at: str | None,
) -> dict:
    """Build the one-time ``MemberAPIKeyResponse`` dict for a freshly minted key.

    Shared by the owner-provisioned mint (force-hidden: not visible) and the
    session self-mint (visible for its auto-hide window) — the two differ only
    in ``is_visible`` / ``visibility_expires_at`` / ``last_used_at``.
    """
    return {
        "id": new_key.id,
        "name": new_key.name,
        "key_prefix": new_key.key_prefix,
        "plaintext_key": plaintext_key,  # shown once
        "is_visible": is_visible,
        "visibility_expires_at": visibility_expires_at,
        "created_at": to_utc_iso(new_key.created_at),
        "last_used_at": last_used_at,
        "revoked_at": None,
        "expires_at": to_utc_iso(new_key.expires_at),  # #1165: owner-set expiry
        "bound_context_id": (str(new_key.bound_context_id) if new_key.bound_context_id else None),
    }


async def check_permission(
    service: MemberCredentialsService,
    requester_id: str,
    workspace_id: UUID,
    target_user_id: str,
    action: str,
) -> None:
    """Check permission and raise HTTPException if not allowed.

    Args:
        service: MemberCredentialsService instance
        requester_id: Requesting user ID
        workspace_id: Workspace ID
        target_user_id: Target user ID
        action: Action to perform

    Raises:
        HTTPException: 403 if not authorized
    """
    can_perform = await service.check_can_manage(
        requester_id=requester_id,
        workspace_id=workspace_id,
        target_user_id=target_user_id,
        action=action,
    )

    if not can_perform:
        raise AuthorizationError(message=f"Not authorized to {action} credentials")


async def _require_downgrade_target(
    db: AsyncSession,
    user_id: str,
    workspace_id: UUID,
    *,
    action_desc: str,
) -> None:
    """Owner-provisioned mint/revoke may only target a member/viewer.

    Owner-key provisioning is strictly privilege-DOWNGRADE (service identities):
    the target's workspace role must be ``member``/``viewer``. Shared by
    ``_owner_provisioned_mint`` and ``delete_api_key_by_id`` so the allowed-target
    set stays in lockstep across mint and revoke — diverging the two would open a
    silent authorization gap on one path (max code-review, PR #1171).

    Args:
        db: Async DB session.
        user_id: The TARGET member's user id.
        workspace_id: The PATH workspace id (permission scope).
        action_desc: Sentence prefix for the 403 message, completed with
            "member/viewer targets, not role=...".

    Raises:
        AuthorizationError: 403 if the target's role is not member/viewer.
    """
    target_role = await MemberCredentialsService(db).get_workspace_role(user_id, workspace_id)
    if target_role not in (WorkspaceRole.MEMBER, WorkspaceRole.VIEWER):
        # ``.value`` via getattr (preserving the not-a-member ``None`` case):
        # ``!r`` on a StrEnum member leaks the Python repr
        # ``<WorkspaceRole.ADMIN: 'admin'>`` to API clients (#1180).
        raise AuthorizationError(
            message=(
                f"{action_desc} member/viewer targets, "
                f"not role={getattr(target_role, 'value', target_role)!r}."
            )
        )


async def _owner_provisioned_mint(
    workspace_id: UUID,
    user_id: str,
    data: CreateAPIKeyRequest,
    user: dict,
    db: AsyncSession,
) -> dict:
    """Issue #1165: mint an API key for another member with a workspace-owner key.

    Guardrails (all 403/400 before any write):
    - owner-only on the PATH workspace (via authorize_workspace_management, which
      also applies the #963 scoped-key confinement → uniform 404);
    - 403 when ``target == caller`` (anti self-replication — a leaked owner key
      must not mint fresh keys for itself and defeat revocation);
    - 403 unless the target's workspace role is ``member``/``viewer`` (owner-key
      minting is strictly privilege-DOWNGRADE provisioning for service identities);
    - 400 if ``expires_days`` omitted (never-expiring CI keys are unacceptable here);
    - 400 if ``bound_context_id`` set (public-bound keys stay self-only).
    The minted key is force-hidden (``hidden_at=now``) so ``plaintext_key`` exists
    only in this single 201 response; a follow-up GET returns it null.
    """
    # Owner gate FIRST on the path workspace (+ #963 confinement). The helper
    # fails closed on a malformed principal missing user_id (clean 403, never a
    # KeyError/500), so read caller_id via .get() only after it passes (Copilot
    # review, PR #1171). session_required_role is unused for the API-key
    # principal but must be supplied.
    principal = await authorize_workspace_management(
        user, workspace_id, db, session_required_role=WorkspaceRole.OWNER
    )
    caller_id = user.get("user_id")

    if caller_id == user_id:
        raise AuthorizationError(
            message="An owner key cannot mint keys for itself. Use session self-mint."
        )

    await _require_downgrade_target(
        db, user_id, workspace_id, action_desc="Owner-provisioned keys can only be minted for"
    )

    if data.expires_days is None:
        raise BadRequestError(
            message="expires_days is required for owner-provisioned API keys (1-3650)."
        )
    if data.bound_context_id is not None:
        raise BadRequestError(
            message="bound_context_id is not allowed for owner-provisioned keys "
            "(public-bound keys are self-only)."
        )

    manager = APIKeyManager(db)
    try:
        plaintext_key, new_key = await manager.create_key(
            name=data.name,
            user_id=user_id,
            workspace_id=workspace_id,
            expires_days=data.expires_days,
            auto_hide_minutes=0,  # visibility_expires_at = now
        )
        # Force-hide immediately so plaintext is never re-revealed via GET, and
        # null the encrypted-at-rest copy too (Migration-035 zero-knowledge). The
        # hourly auto-hide sweeper only clears rows with hidden_at IS NULL, so a
        # force-hidden row it never touches would otherwise retain a
        # Fernet-decryptable copy of a live long-lived key indefinitely (max
        # code-review, PR #1171). Shared with hide_key so the two never drift.
        apply_zero_knowledge_hide(new_key)

        # Audit via the shared helper (#1164) — it records the ACTING owner key
        # under metadata["key_prefix"] and hardcodes via/workspace_id/target; we
        # override the resource to point at the minted key and carry the minted
        # prefix under a distinct key so the actor prefix is not clobbered.
        await audit_programmatic_workspace_action(
            db,
            principal,
            user,
            workspace_id,
            action="member_api_key_provisioned",
            target=user_id,
            resource=f"api_key:{new_key.id}",
            metadata={
                "minted_key_prefix": new_key.key_prefix,  # the MINTED key
                "expires_days": data.expires_days,
            },
        )
        await db.commit()

        logger.info(
            "member_api_key_provisioned",
            key_id=new_key.id,
            actor_id=caller_id,
            target=user_id,
            workspace_id=str(workspace_id),
        )
        # Force-hidden: plaintext exists only in this one 201 response.
        return _minted_key_response(
            new_key,
            plaintext_key,
            is_visible=False,
            visibility_expires_at=None,
            last_used_at=None,
        )
    except ValueError as e:
        raise BadRequestError(message=str(e)) from e


@router.get("/{user_id}/credentials", response_model=MemberCredentialsResponse)
async def get_member_credentials(
    workspace_id: UUID,
    user_id: str,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> MemberCredentialsResponse:
    """Get or create member credentials (Lazy initialization).

    Migration 034: Zero-knowledge model (unchanged for sessions).
    - A session caller sees plaintext ONLY for their OWN key while it is still
      visible (``get_or_create_credentials`` includes ``plaintext_key`` only
      when ``requester_id == user_id``). Session owner/admin viewing ANOTHER
      member sees metadata only.
    - Issue #1165: an API-key OWNER principal may also view another member's key,
      but the response is ALWAYS metadata-only (``plaintext_key`` nulled) —
      programmatic responses get logged, so plaintext must never appear.
    - OAuth bearer principals are rejected (403).

    Raises:
        HTTPException: If not authorized.
    """
    # Issue #1165: OAuth rejected; API-key principal must be workspace owner.
    _reject_oauth(user, "view member credentials")
    programmatic = is_api_key_principal(user)
    caller_role: str | None = None
    if programmatic:
        # authorize_workspace_management already fetched the caller's owner
        # WorkspaceMember row; reuse its role so the view-permission check below
        # does not re-SELECT the same row (max code-review, PR #1171).
        principal = await authorize_workspace_management(
            user, workspace_id, db, session_required_role=WorkspaceRole.OWNER
        )
        caller_role = principal.member.role

    service = MemberCredentialsService(db)

    try:
        credentials = await service.get_or_create_credentials(
            workspace_id=workspace_id,
            user_id=user_id,
            requester_id=user["user_id"],
            requester_role=caller_role,
        )

        # Issue #1165: metadata-only for programmatic principals — never leak
        # plaintext into a response that may be logged.
        if programmatic:
            for key in credentials.get("api_keys", []):
                if isinstance(key, dict):
                    key["plaintext_key"] = None

        # Get target user's workspace role (for permission checks)
        target_role = await service.get_workspace_role(user_id, workspace_id)
        if target_role is None:
            # Target is not (or no longer) a workspace member. Without this the
            # non-optional MemberCredentialsResponse.target_user_role would fail
            # validation and the blanket ``except Exception`` below would turn a
            # wrong/removed target into an opaque 500. Raise HTTPException (not
            # NotFoundException, which subclasses MemoryCloudException and WOULD
            # be swallowed here) so ``except HTTPException: raise`` returns a
            # clean 404 (v0.42 max review).
            raise HTTPException(status_code=404, detail="Member not found")

        return MemberCredentialsResponse(**credentials, target_user_role=target_role)
    except HTTPException:
        raise
    except ValueError as e:
        logger.error("get_member_credentials_invalid", error=str(e), user_id=user_id)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("get_member_credentials_failed", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.post("/{user_id}/credentials/api-key/hide")
async def hide_api_key(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually hide API key (Owner only).

    Migration 034: Zero-knowledge - only owner can hide.

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not owner or key not found
    """
    # Permission check: owner only
    if user["user_id"] != user_id:
        raise AuthorizationError(message="Only owner can hide API key")

    # Get API key (most recent active key)
    from sqlalchemy import and_, select

    from models.auth import APIKey

    result = await db.execute(
        select(APIKey)
        .where(
            and_(
                APIKey.user_id == user_id,
                APIKey.workspace_id == workspace_id,
                APIKey.revoked_at.is_(None),
            )
        )
        .order_by(APIKey.created_at.desc())
        .limit(1)
    )
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Hide key
    manager = APIKeyManager(db)
    try:
        await manager.hide_key(api_key.id, user_id)
        await db.commit()
        return {"status": "hidden", "key_id": api_key.id}
    except PermissionError as e:
        # CWE-639: keep the raw PermissionError text off the wire (log-only
        # ``reason``); the handler emits the uniform "Insufficient permissions".
        raise AuthorizationError(reason=str(e)) from e
    except ValueError as e:
        # Sibling of the 403 above — converted together so this handler does
        # not emit a mixed {detail}/{error} shape (#992 Phase 1).
        raise NotFoundException("API key") from e


@router.post(
    "/{user_id}/credentials/api-key/regenerate",
    response_model=RegenerateAPIKeyResponse,
)
async def regenerate_api_key(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> RegenerateAPIKeyResponse:
    """Regenerate API key (Permission-based).

    Migration 034: Creates new key, revokes old one.

    New permission hierarchy:
    - Self: Always allowed
    - Owner: Can regenerate admin/member/viewer's keys
    - Admin: Can regenerate member/viewer's keys

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        New plaintext key (shown once)

    Raises:
        HTTPException: If not authorized or key not found
    """
    # Permission check: hierarchical
    service = MemberCredentialsService(db)
    await check_permission(service, user["user_id"], workspace_id, user_id, "regenerate")

    from sqlalchemy import and_, select

    from models.auth import APIKey

    # Get old key (most recent active key)
    result = await db.execute(
        select(APIKey)
        .where(
            and_(
                APIKey.user_id == user_id,
                APIKey.workspace_id == workspace_id,
                APIKey.revoked_at.is_(None),
            )
        )
        .order_by(APIKey.created_at.desc())
        .limit(1)
    )
    old_key = result.scalar_one_or_none()

    if not old_key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Revoke old key

    old_key.revoked_at = utcnow()

    # Create new key
    manager = APIKeyManager(db)
    new_plaintext_key, new_key = await manager.create_key(
        name=old_key.name,
        user_id=user_id,
        workspace_id=workspace_id,
        auto_hide_minutes=10,
    )

    await db.commit()

    logger.info(
        "api_key_regenerated",
        old_key_id=old_key.id,
        new_key_id=new_key.id,
        user_id=user_id,
    )

    return RegenerateAPIKeyResponse(
        key=new_plaintext_key,
        key_prefix=new_key.key_prefix,
        key_id=new_key.id,
    )


@router.post(
    "/{user_id}/credentials/api-keys", response_model=MemberAPIKeyResponse, status_code=201
)
async def create_api_key(
    workspace_id: UUID,
    user_id: str,
    data: CreateAPIKeyRequest,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create new API key.

    Issue #1165: two principals.
    - **Session** (web UI): self-mint only (``#252`` unchanged) — the caller
      may only mint their OWN key. ``expires_days`` is NOT accepted on this path
      (400 if supplied — session keys keep their historical no-expiry behavior);
      ``bound_context_id`` is still allowed (#626 public-bound path).
    - **API-key owner** (programmatic): may mint for ANOTHER member, gated to
      workspace-OWNER on the path workspace. Guardrails: 403 when target ==
      caller (anti self-replication, #252 threat); 403 unless the target's
      workspace role is ``member``/``viewer`` (privilege-downgrade provisioning
      only); ``expires_days`` REQUIRED (400 if omitted — never-expiring CI keys
      are not an acceptable default); ``bound_context_id`` rejected (400 —
      public-bound keys stay self-only); the minted key is force-hidden so the
      plaintext exists only in this one 201 response.
    OAuth bearer principals are rejected (403).

    Issue #626: If ``data.bound_context_id`` is supplied, the key is stored
    as a public-bound key (``api_keys.workspace_id`` is left NULL) and is
    attributed to that one ``is_public=true`` context for the rate-limit /
    audit / revoke surface on ``/api/v1/public/{ctx}/*``. The binding is
    immutable — to change it, revoke this key and create a new one.

    Args:
        workspace_id: Workspace ID (from URL — used as permission scope; also
            assigned to ``api_keys.workspace_id`` unless a bound context is set)
        user_id: Target user ID
        data: API key creation data (name, auto_hide_minutes, bound_context_id)
        user: Current user (from auth)
        db: Database session

    Returns:
        Created API key with plaintext (shown once)

    Raises:
        HTTPException: If not owner, name already exists, tier gate fails,
            or the bound context is missing / not public / not in this workspace.
    """
    # Issue #1165: OAuth bearer tokens must never mint long-lived credentials.
    _reject_oauth(user, "mint API keys")

    # Issue #1165: owner-provisioned programmatic minting for ANOTHER member.
    if is_api_key_principal(user):
        return await _owner_provisioned_mint(workspace_id, user_id, data, user, db)

    # Fail closed: only a POSITIVELY-identified session may reach the self-mint
    # path. Reaching it by elimination (not OAuth, not API-key => assume session)
    # would let an unrecognized principal shape mint a key; mirror the shared
    # authorize_workspace_management helper's positive-session default. Legit
    # sessions always carry the OIDC ``sub`` (SessionManager.create_session
    # requires it) (v0.42 max review).
    if not is_session_principal(user):
        raise AuthorizationError(message="Unrecognized principal for API key creation")

    # Session path (#252 unchanged): self-mint only.
    if user["user_id"] != user_id:
        raise AuthorizationError(
            message="You can only create your own API keys in a session. "
            "To provision a key for another member, use a workspace-owner API key."
        )

    # Issue #1165: expires_days is an owner-provisioned-only field. Session
    # self-mint keeps its historical "no expiry" behavior and does not plumb
    # expires_days to APIKeyManager — reject it explicitly rather than silently
    # ignoring a client-supplied value (Copilot review, PR #1171).
    if data.expires_days is not None:
        raise BadRequestError(
            message="expires_days is only supported for owner-provisioned keys "
            "(via a workspace-owner API key), not session self-mint."
        )

    # Issue #626: Public-bound key creation gating + scope decision.
    # When ``bound_context_id`` is supplied, the key is public-bound and the
    # ``api_keys.workspace_id`` column is left NULL (mutually exclusive per
    # the DB CHECK constraint). The URL ``workspace_id`` continues to scope
    # permission (the workspace must own the context, the plan must include
    # ``public_contexts``) but does NOT scope the key itself.
    bound_context_uuid: UUID | None = None
    key_scope_workspace_id: UUID | None = workspace_id
    if data.bound_context_id is not None:
        try:
            bound_context_uuid = UUID(data.bound_context_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="bound_context_id must be a valid UUID"
            ) from exc

        # Verify the context exists, is public, and belongs to this workspace.
        from sqlalchemy import select as _select

        from models.auth import Context

        ctx_row = await db.execute(_select(Context).where(Context.id == bound_context_uuid))
        ctx = ctx_row.scalar_one_or_none()
        if ctx is None:
            raise HTTPException(status_code=404, detail="bound_context_id: context not found")
        if ctx.workspace_id != workspace_id:
            raise AuthorizationError(
                message="bound_context_id: context does not belong to this workspace"
            )
        if ctx.is_public is not True:
            raise HTTPException(
                status_code=422,
                detail="bound_context_id: context is not public (set is_public=true first)",
            )
        # Ownership gate: only the context creator can mint a public-bound key
        # against it. In a multi-member PRO workspace this prevents a non-admin
        # member from minting per-key buckets (and an audit-trail footprint)
        # against another member's public context. Workspace owner/admin gets
        # this implicitly by creating their own context to bind.
        # Strict match: ``created_by == user_id`` only. Legacy contexts with
        # ``created_by IS NULL`` (pre-#160 backfill survivors) are NOT
        # eligible — there is no way to prove client-side OR server-side
        # which workspace member should own a null-created context, so
        # defaulting-to-permit would let any workspace member mint keys
        # against it. The owner can resolve by updating the context's
        # ``created_by`` (via a separate admin action) or by recreating
        # the context.
        if ctx.created_by != user_id:
            raise AuthorizationError(
                message=(
                    "bound_context_id: only the context creator can mint a "
                    "public-bound key against this context"
                )
            )

        # Tier gate: workspace plan must include the ``public_contexts`` feature
        # AND have a non-zero per-key minute quota. The two checks defend in
        # depth: the feature-flag check is the primary intent (PRO+), but a
        # custom plan / env override that left ``bound_public_calls_per_minute``
        # at 0 would otherwise mint a key that 429s on every public-endpoint
        # call. Refuse creation up-front so the operator sees the
        # misconfiguration immediately, not after distributing a dead key.
        from sqlalchemy import select as _select_ws

        from config.plan_tiers import get_plan_tier, has_feature
        from models.auth import Workspace

        ws_row = await db.execute(_select_ws(Workspace).where(Workspace.id == workspace_id))
        ws = ws_row.scalar_one_or_none()
        # ``ws.plan_name`` itself is nullable for legacy rows that
        # pre-date the plan_name backfill — coerce to "free" so
        # ``has_feature`` / ``get_plan_tier`` don't see ``None`` and 500.
        plan_name = (ws.plan_name if ws is not None else None) or "free"
        if not has_feature(plan_name, "public_contexts"):
            raise AuthorizationError(
                message="Workspace plan does not include the public_contexts feature"
            )
        if get_plan_tier(plan_name).bound_public_calls_per_minute <= 0:
            raise AuthorizationError(
                message=(
                    "Workspace plan does not provision a per-key quota for "
                    "public-bound API keys (bound_public_calls_per_minute=0)"
                )
            )

        # Mutual exclusion with workspace scope (#169).
        key_scope_workspace_id = None

    manager = APIKeyManager(db)

    try:
        plaintext_key, new_key = await manager.create_key(
            name=data.name,
            user_id=user_id,
            workspace_id=key_scope_workspace_id,
            bound_context_id=bound_context_uuid,
            auto_hide_minutes=data.auto_hide_minutes,
        )

        # Issue #626: audit log entry for public-bound key creation. Other
        # key types are intentionally not audited here — the codebase does
        # not write AuditLog for owner/workspace key creation today, and
        # adding it for public-bound only matches the heightened scrutiny
        # of credentials that grant public access.
        if bound_context_uuid is not None:
            from models.auth import AuditLog

            db.add(
                AuditLog(
                    user_email=user.get("email") or f"{user_id}@api",
                    user_id=user_id,
                    action="public_bound_key_created",
                    resource=f"api_key:{new_key.id}",
                    user_metadata={
                        "bound_context_id": str(bound_context_uuid),
                        "workspace_id": str(workspace_id),
                        "key_name": data.name,
                    },
                )
            )

        await db.commit()

        logger.info(
            "api_key_created",
            key_id=new_key.id,
            user_id=user_id,
            name=data.name,
            bound_context_id=str(bound_context_uuid) if bound_context_uuid else None,
        )

        # Return with plaintext (shown once); visible for its auto-hide window.
        return _minted_key_response(
            new_key,
            plaintext_key,
            is_visible=True,
            visibility_expires_at=to_utc_iso(new_key.visibility_expires_at),
            last_used_at=to_utc_iso(new_key.last_used_at),
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_api_key_failed", error=str(e), user_id=user_id)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.delete("/{user_id}/credentials/api-key")
async def delete_api_key(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete API key (Permission-based).

    Permission matrix:
    - Owner: Delete own key
    - Workspace Owner: Delete any member's key
    - Workspace Admin: Delete member/viewer's key

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not authorized or key not found
    """
    service = MemberCredentialsService(db)

    # Permission check
    await check_permission(service, user["user_id"], workspace_id, user_id, "delete")

    # Get and delete key
    from sqlalchemy import and_, select

    from models.auth import APIKey

    result = await db.execute(
        select(APIKey)
        .where(
            and_(
                APIKey.user_id == user_id,
                APIKey.workspace_id == workspace_id,
                APIKey.revoked_at.is_(None),  # Only delete active keys
            )
        )
        .order_by(APIKey.created_at.desc())
        .limit(1)
    )
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    await db.delete(api_key)
    await db.commit()

    logger.info("api_key_deleted", key_id=api_key.id, user_id=user_id)

    return {"status": "deleted", "key_id": api_key.id}


@router.delete("/{user_id}/credentials/api-keys/{key_id}")
async def delete_api_key_by_id(
    workspace_id: UUID,
    user_id: str,
    key_id: int,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete/revoke a specific API key by ID.

    Issue #626: per-id endpoint (also revokes public-bound keys). The URL
    ``workspace_id`` is the permission scope, not a filter on the key's column.

    Issue #1165: two principals.
    - **Session**: self-only (``#252`` unchanged) — hard delete of your own key.
    - **API-key OWNER** (programmatic): may revoke ANOTHER member's key (target
      must be member/viewer). This is a **soft revoke** (``revoked_at=now``, row
      retained for forensics), and the audit row is written BEFORE the state
      change. OAuth bearer principals are rejected (403).

    Raises:
        HTTPException: 403 if not permitted, 404 if key not found.
    """
    _reject_oauth(user, "revoke API keys")
    # Fail closed on a malformed principal missing user_id — .get() (not
    # indexing) so a bad dict yields a clean 403, never a KeyError/500. The
    # programmatic branch's authorize_workspace_management re-validates; the
    # session branch's self-only check (caller_id != user_id) rejects a None
    # caller (Copilot review, PR #1171).
    caller_id = user.get("user_id")
    programmatic = is_api_key_principal(user)
    principal = None
    if programmatic:
        # Owner gate on the path workspace (+ #963 confinement).
        principal = await authorize_workspace_management(
            user, workspace_id, db, session_required_role=WorkspaceRole.OWNER
        )
        if user_id != caller_id:
            # Cross-member revoke is owner-provisioned; restrict target role.
            await _require_downgrade_target(
                db, user_id, workspace_id, action_desc="Owner-provisioned revocation is limited to"
            )
    else:
        # Fail closed: only a POSITIVELY-identified session may reach the
        # self-only delete path (not OAuth, not API-key => assume session would
        # let an unrecognized principal shape through). Legit sessions always
        # carry the OIDC ``sub`` (v0.42 max review).
        if not is_session_principal(user):
            raise AuthorizationError(message="Unrecognized principal for API key deletion")
        if caller_id != user_id:
            # Session path (#252 unchanged): self-only.
            raise AuthorizationError(
                message="You can only delete your own API keys in a session. "
                "To revoke another member's key, use a workspace-owner API key."
            )

    from sqlalchemy import and_, select

    from models.auth import APIKey, Context

    result = await db.execute(
        select(APIKey).where(and_(APIKey.id == key_id, APIKey.user_id == user_id))
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    # A soft-revoked row is a retained forensic record (#1165). Any further
    # delete/revoke must not touch it: a SESSION self-delete would HARD-delete
    # the evidence (the member erasing the key an owner just revoked on them),
    # and a repeated programmatic revoke would overwrite the original
    # revoked_at timestamp and append a duplicate audit row. Treat an
    # already-revoked key as not-found for both paths — uniform 404 so the
    # forensic row's continued existence is not revealed (v0.42 max review).
    if api_key.revoked_at is not None:
        raise HTTPException(status_code=404, detail="API key not found")

    # Verify the URL ``workspace_id`` matches the key's real scope.
    # Without this, an authenticated user could pass an arbitrary
    # ``workspace_id`` in the path and the AuditLog row would record a
    # workspace that has nothing to do with the deleted key. Owner-only
    # enforcement above prevents privilege escalation, but auditing
    # integrity matters: the workspace in the audit trail must reflect
    # where the key was actually scoped.
    if api_key.workspace_id is not None:
        # Workspace-scoped key (#169) — URL workspace must match the column.
        if api_key.workspace_id != workspace_id:
            raise AuthorizationError(message="API key does not belong to this workspace")
    elif api_key.bound_context_id is not None:
        # Public-bound key (#626) — the binding's workspace must match.
        ctx_row = await db.execute(select(Context).where(Context.id == api_key.bound_context_id))
        bound_ctx = ctx_row.scalar_one_or_none()
        # If the bound context was deleted (SET NULL would have already
        # nulled bound_context_id, but in-flight requests may race), the
        # key has no anchoring workspace — fall through to delete without
        # the workspace-match assertion.
        if bound_ctx is not None and bound_ctx.workspace_id != workspace_id:
            raise AuthorizationError(
                message="API key is bound to a context in a different workspace"
            )
    else:
        # Global / owner-scoped key (both columns NULL) — not anchored to any
        # workspace. For a SESSION self-delete or a programmatic SELF-revoke the
        # URL workspace is purely permission scope and the owner-only check above
        # is the only gate. But a programmatic CROSS-member revoke cannot be
        # anchored here: the path-workspace owner has no proven authority over a
        # member's account-global key — it may be the key that member uses in
        # their OTHER workspaces. Refuse with a uniform 404 rather than revoke
        # across a workspace boundary (and misattribute the audit row to this
        # workspace) (max code-review, PR #1171).
        if programmatic and user_id != caller_id:
            raise NotFoundException("API key")

    # Capture binding info before mutation — the audit-log entry below
    # needs the original ``bound_context_id``.
    bound_ctx_id = api_key.bound_context_id

    # Issue #1165: ALL programmatic (API-key) revocations are SOFT revokes
    # (revoked_at set, row retained for forensics) + audited — including an
    # owner revoking their OWN key, so a programmatic credential action is
    # never a silent hard delete (Copilot review, PR #1171). The audit row and
    # the revoked_at update commit atomically in one transaction. Session
    # self-delete keeps the existing hard-delete semantics (#252, #626).
    if programmatic:
        # authorize_workspace_management above returned a (non-None) api_key
        # principal for the programmatic branch.
        assert principal is not None
        # Audit via the shared helper (#1164): it records the ACTING owner key
        # under metadata["key_prefix"]; the revoked key's prefix is carried under
        # a distinct key so the actor prefix is not clobbered. resource points at
        # the revoked key, not the workspace.
        await audit_programmatic_workspace_action(
            db,
            principal,
            user,
            workspace_id,
            action="member_api_key_revoked",
            target=user_id,
            resource=f"api_key:{key_id}",
            metadata={
                "revoked_key_prefix": api_key.key_prefix,  # the REVOKED key
                "self_revoke": user_id == caller_id,
            },
        )
        api_key.revoked_at = utcnow()
        # Zero-knowledge (Migration-035): drop any at-rest plaintext on revoke.
        # The hourly auto-hide sweeper only clears rows with revoked_at IS NULL,
        # so a key revoked inside its visibility window would otherwise retain a
        # Fernet-decryptable plaintext copy indefinitely (v0.42 max review).
        apply_zero_knowledge_hide(api_key)
        await db.commit()
        logger.info(
            "member_api_key_revoked",
            key_id=key_id,
            actor_id=caller_id,
            target=user_id,
            workspace_id=str(workspace_id),
        )
        return {"status": "revoked", "key_id": key_id}

    await db.delete(api_key)

    # Issue #626: audit log entry for public-bound key revocation.
    if bound_ctx_id is not None:
        from models.auth import AuditLog

        db.add(
            AuditLog(
                user_email=user.get("email") or f"{user_id}@api",
                user_id=user_id,
                action="public_bound_key_revoked",
                resource=f"api_key:{key_id}",
                user_metadata={
                    "bound_context_id": str(bound_ctx_id),
                    "workspace_id": str(workspace_id),
                },
            )
        )

    await db.commit()

    logger.info(
        "api_key_deleted",
        key_id=key_id,
        user_id=user_id,
        bound_context_id=str(bound_ctx_id) if bound_ctx_id is not None else None,
    )

    return {"status": "deleted", "key_id": key_id}


# ============================================================================
# OAuth App Endpoints
# ============================================================================


@router.post("/{user_id}/credentials/oauth/hide")
async def hide_oauth_app(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually hide OAuth app secret (Owner only).

    Migration 034: Zero-knowledge - only owner can hide.

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not owner or app not found
    """
    # Permission check: owner only
    if user["user_id"] != user_id:
        raise AuthorizationError(message="Only owner can hide OAuth app")

    from sqlalchemy import and_, select

    from models.auth import OAuth2Client

    result = await db.execute(
        select(OAuth2Client).where(
            and_(
                OAuth2Client.owner_id == user_id,
                OAuth2Client.workspace_id == workspace_id,
            )
        )
    )
    oauth_app = result.scalar_one_or_none()

    if not oauth_app:
        raise HTTPException(status_code=404, detail="OAuth app not found")

    # Hide app
    oauth_app.hidden_at = utcnow()
    oauth_app.visibility_expires_at = None  # Cancel auto-hide
    oauth_app.plaintext_secret_encrypted = None  # Migration 035: Delete encrypted secret

    await db.commit()

    logger.info("oauth_app_hidden", client_id=oauth_app.client_id, user_id=user_id)

    return {"status": "hidden", "client_id": oauth_app.client_id}


@router.post(
    "/{user_id}/credentials/oauth/regenerate",
    response_model=RegenerateOAuthSecretResponse,
)
async def regenerate_oauth_secret(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> RegenerateOAuthSecretResponse:
    """Regenerate OAuth client secret (Permission-based).

    Migration 034: Generates new secret, updates hash.

    New permission hierarchy:
    - Self: Always allowed
    - Owner: Can regenerate admin/member/viewer's secrets
    - Admin: Can regenerate member/viewer's secrets

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        New plaintext secret (shown once)

    Raises:
        HTTPException: If not authorized or app not found
    """
    # Permission check: hierarchical
    service = MemberCredentialsService(db)
    can_regenerate = await service.check_can_manage(
        requester_id=user["user_id"],
        workspace_id=workspace_id,
        target_user_id=user_id,
        action="regenerate",
    )

    if not can_regenerate:
        raise AuthorizationError(message="Not authorized to regenerate OAuth secret")

    import hashlib
    import secrets
    from datetime import timedelta

    from sqlalchemy import and_, select

    from models.auth import OAuth2Client

    # Get OAuth app
    result = await db.execute(
        select(OAuth2Client).where(
            and_(
                OAuth2Client.owner_id == user_id,
                OAuth2Client.workspace_id == workspace_id,
            )
        )
    )
    oauth_app = result.scalar_one_or_none()

    if not oauth_app:
        raise HTTPException(status_code=404, detail="OAuth app not found")

    # Generate new secret
    new_secret = secrets.token_urlsafe(32)
    new_secret_hash = hashlib.sha256(new_secret.encode()).hexdigest()

    # Migration 035: Encrypt plaintext for storage
    from utils.encryption import get_encryptor

    plaintext_secret_encrypted = get_encryptor().encrypt(new_secret)

    # Update app
    oauth_app.client_secret_hash = new_secret_hash
    oauth_app.hidden_at = None  # Make visible
    oauth_app.visibility_expires_at = utcnow() + timedelta(minutes=10)  # 10 minutes
    oauth_app.plaintext_secret_encrypted = plaintext_secret_encrypted  # Migration 035

    await db.commit()

    logger.info(
        "oauth_secret_regenerated",
        client_id=oauth_app.client_id,
        user_id=user_id,
    )

    return RegenerateOAuthSecretResponse(
        client_secret=new_secret,
        client_id=oauth_app.client_id,
    )


@router.delete("/{user_id}/credentials/oauth")
async def delete_oauth_app(
    workspace_id: UUID,
    user_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete OAuth app (Permission-based).

    Permission matrix:
    - Owner: Delete own app
    - Workspace Owner: Delete any member's app
    - Workspace Admin: Delete member/viewer's app

    Args:
        workspace_id: Workspace ID
        user_id: Target user ID
        user: Current user (from auth)
        db: Database session

    Returns:
        Status message

    Raises:
        HTTPException: If not authorized or app not found
    """
    service = MemberCredentialsService(db)

    # Permission check
    can_delete = await service.check_can_manage(
        requester_id=user["user_id"],
        workspace_id=workspace_id,
        target_user_id=user_id,
        action="delete",
    )

    if not can_delete:
        raise AuthorizationError(message="Not authorized to delete OAuth app")

    # Get and delete app
    from sqlalchemy import and_, select

    from models.auth import OAuth2Client

    result = await db.execute(
        select(OAuth2Client).where(
            and_(
                OAuth2Client.owner_id == user_id,
                OAuth2Client.workspace_id == workspace_id,
            )
        )
    )
    oauth_app = result.scalar_one_or_none()

    if not oauth_app:
        raise HTTPException(status_code=404, detail="OAuth app not found")

    await db.delete(oauth_app)
    await db.commit()

    logger.info("oauth_app_deleted", client_id=oauth_app.client_id, user_id=user_id)

    return {"status": "deleted", "client_id": oauth_app.client_id}
