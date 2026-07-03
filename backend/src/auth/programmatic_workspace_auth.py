"""Programmatic (API-key) authorization for workspace-management endpoints.

Issue #1164 / #1165: the member/invitation endpoints (`workspaces.py`,
`invitations.py`) and the member-credential endpoints (`member_credentials.py`)
open to programmatic auth. This module centralizes the principal-discrimination
rule so all of those routes gate identically:

- **Session principal** (carries the OIDC ``sub`` claim): behavior unchanged —
  the endpoint's existing role gate is applied via ``session_required_role``.
- **API-key principal** (carries ``api_key_workspace_id`` — a *presence* test,
  never truthiness, because global keys carry the key with value ``None``):
  accepted, **workspace-OWNER only**, on the PATH ``workspace_id`` (not the
  principal's ``current_workspace_id``). A workspace-scoped key bound to a
  different workspace is confined to a **uniform 404** (Issue #963 pattern),
  checked BEFORE the owner lookup so it cannot be used to probe existence.
- **OAuth Bearer principal** (carries ``oauth_scope``): **403** on every
  endpoint in this release — every ``kagura auth login`` device token carries
  ``memory:read memory:write``, so accepting OAuth here would silently turn
  every user's ordinary MCP token into a member-management credential. Rejected
  until a dedicated ``workspace:admin`` scope exists.
- **Fail closed**: any principal not positively identified as a session is
  treated as programmatic (owner-required or, if unidentifiable, rejected).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import WorkspaceMember
from services.permission_service import PermissionService
from utils.exceptions import AuthorizationError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthorizedPrincipal:
    """Result of a successful workspace-management authorization.

    Attributes:
        kind: ``"api_key"`` or ``"session"`` — callers vary response shape on
            this (e.g. programmatic principals get token-less invitation lists).
        member: The caller's ``WorkspaceMember`` row (from the permission
            lookup). For an API-key principal this is the owner membership;
            for a session it is the caller's membership at ``session_required_role``
            or above. Callers reuse ``member.role`` for downstream guards (e.g.
            the #254 owner-change check) without a second DB lookup.
    """

    kind: str
    member: WorkspaceMember


# OAuth bearer tokens are rejected on these surfaces until a dedicated
# workspace-admin scope is designed (see module docstring).
_OAUTH_REJECTED_MSG = (
    "OAuth bearer tokens cannot manage workspace members or credentials. "
    "Use a workspace-owner API key."
)


def is_session_principal(user: dict) -> bool:
    """True iff ``user`` is a session principal (carries the OIDC ``sub``)."""
    return "sub" in user


def is_oauth_principal(user: dict) -> bool:
    """True iff ``user`` is an OAuth bearer principal."""
    return "oauth_scope" in user


def is_api_key_principal(user: dict) -> bool:
    """True iff ``user`` is an API-key principal (presence, not truthiness)."""
    return "api_key_workspace_id" in user


async def authorize_workspace_management(
    user: dict,
    workspace_id: UUID,
    db: AsyncSession,
    *,
    session_required_role: WorkspaceRole,
) -> AuthorizedPrincipal:
    """Authorize a workspace-management request across auth modes.

    Args:
        user: The authenticated principal dict (session / API-key / OAuth).
        workspace_id: The PATH workspace id — the resource being managed. This
            is deliberately NOT the principal's ``current_workspace_id``:
            ``require_workspace_owner`` reads only the latter and is the wrong
            gate for a path-parameterized route.
        db: Async DB session for the permission lookup.
        session_required_role: The role a SESSION principal must hold for this
            endpoint (unchanged per-endpoint semantics). Ignored for API-key
            principals, which are always owner-gated.

    Returns:
        An ``AuthorizedPrincipal`` carrying the principal ``kind``
        (``"api_key"`` / ``"session"``) and the caller's ``WorkspaceMember``
        row, so callers can vary response shape and reuse ``member.role`` for
        downstream guards without a second lookup.

    Raises:
        AuthorizationError: 403 — OAuth principal, non-owner API key, session
            below the required role, or an unidentifiable principal.
        NotFoundException: 404 — a workspace-scoped API key whose bound
            workspace differs from the path workspace (#963 confinement).
    """
    user_id = user.get("user_id")
    # Fail closed on a malformed principal: a missing user_id must produce a
    # clean 403, never a 500 from PermissionService querying with None
    # (Copilot review, PR #1170).
    if not user_id:
        logger.warning("workspace_mgmt_missing_user_id", workspace_id=str(workspace_id))
        raise AuthorizationError("Unrecognized principal for workspace management")
    perm_service = PermissionService(db)

    # OAuth → reject before any lookup.
    if is_oauth_principal(user):
        logger.warning(
            "workspace_mgmt_oauth_denied", user_id=user_id, workspace_id=str(workspace_id)
        )
        raise AuthorizationError(_OAUTH_REJECTED_MSG)

    # API-key → owner-only, with #963 confinement first.
    if is_api_key_principal(user):
        key_workspace_id = user.get("api_key_workspace_id")
        if key_workspace_id is not None and key_workspace_id != workspace_id:
            # Uniform 404 — never reveal that the path workspace exists to a
            # key bound elsewhere (no existence probing).
            logger.warning(
                "workspace_mgmt_scoped_key_confined",
                user_id=user_id,
                key_workspace_id=str(key_workspace_id),
                path_workspace_id=str(workspace_id),
            )
            raise NotFoundException("Workspace not found")
        member = await perm_service.check_workspace_owner(user_id, workspace_id)
        return AuthorizedPrincipal(kind="api_key", member=member)

    # Session → unchanged endpoint role gate.
    if is_session_principal(user):
        member = await perm_service.check_workspace_access(
            user_id, workspace_id, required_role=session_required_role
        )
        return AuthorizedPrincipal(kind="session", member=member)

    # Fail closed: not positively a session, not a recognized programmatic
    # principal → deny.
    logger.warning(
        "workspace_mgmt_unrecognized_principal", user_id=user_id, workspace_id=str(workspace_id)
    )
    raise AuthorizationError("Unrecognized principal for workspace management")


async def audit_programmatic_workspace_action(
    db: AsyncSession,
    principal: AuthorizedPrincipal,
    user: dict,
    workspace_id: UUID,
    *,
    action: str,
    target: str,
    resource: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Write an AuditLog row for a PROGRAMMATIC workspace-management mutation.

    Issue #1164: today ``AuditLog`` only records public-bound key creation, so a
    stolen-owner-key incident on the member/invitation surface would be
    forensically invisible. This records programmatic (API-key) successes with
    the actor, workspace, action, and target. Session actions are intentionally
    NOT audited here — this call is a no-op for session principals so the web-UI
    path keeps its existing (unaudited) behavior unchanged.

    ``resource`` defaults to ``workspace:{workspace_id}`` (the member/invitation
    surface, whose resource IS the workspace). The member-credential surface
    (#1165) passes ``resource=f"api_key:{id}"`` so the row points at the specific
    minted/revoked key. When the acting API key prefix is present, it is recorded
    under ``metadata["key_prefix"]``; callers that also want to record the
    minted/revoked key prefix must pass it via ``metadata`` under a DISTINCT key
    (e.g. ``minted_key_prefix`` / ``revoked_key_prefix``) so it is not clobbered.

    The row is added to the session but NOT committed — the caller commits it
    atomically with the mutation it is auditing.
    """
    if principal.kind != "api_key":
        return

    actor_id = user.get("user_id")
    if not actor_id:
        # AuditLog.user_id is non-nullable — a missing actor would raise a DB
        # integrity error and fail the (already-authorized) mutation. Audit is
        # best-effort observability, so skip-with-warning instead of crashing.
        # In practice authorize_workspace_management already rejected a
        # principal without user_id, so this is defense in depth (Copilot
        # review, PR #1170).
        logger.warning(
            "workspace_mgmt_audit_skipped_no_actor",
            action=action,
            workspace_id=str(workspace_id),
        )
        return

    from models.auth import AuditLog

    # Issue #1164: attribute the acting API key by its non-secret prefix (from
    # the authenticated principal — safe to log) so a stolen-owner-key incident
    # is traceable to the specific key, not just the owner user_id.
    audit_metadata = {
        "workspace_id": str(workspace_id),
        "target": target,
        "via": "api_key",
        **(metadata or {}),
    }
    key_prefix = user.get("api_key_prefix")
    if key_prefix:
        audit_metadata["key_prefix"] = key_prefix

    db.add(
        AuditLog(
            user_email=user.get("email") or f"{actor_id}@api",
            user_id=actor_id,
            action=action,
            resource=resource if resource is not None else f"workspace:{workspace_id}",
            user_metadata=audit_metadata,
        )
    )
    logger.info(
        "workspace_mgmt_programmatic_action",
        action=action,
        actor_id=actor_id,
        workspace_id=str(workspace_id),
        target=target,
    )
