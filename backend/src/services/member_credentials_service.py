"""Member Credentials Service for Zero-knowledge Credential Management.

Migration 034: Member-scoped API Keys and OAuth Apps.

Implements:
- Lazy initialization (get or create on first access)
- Zero-knowledge visibility (only owner can view secrets)
- Permission-based management (Owner/Admin/Member hierarchy)
"""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    APIKey,
    Context,
    ContextMember,
    ExternalAPIKey,
    OAuth2Client,
    OAuth2Token,
    WorkspaceMember,
)
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import AuthorizationError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)


class MemberCredentialsService:
    """Service for managing member-scoped credentials.

    Zero-knowledge model:
    - Only credential owner can view plaintext secrets
    - Owner/Admin can revoke/delete others' credentials
    - Admin can only manage member/viewer credentials (not owner/admin)

    Attributes:
        db: AsyncSession for database access
    """

    def __init__(self, db: AsyncSession):
        """Initialize Member Credentials Service.

        Args:
            db: Async database session
        """
        self.db = db

    async def _get_workspace_owner(self, workspace_id: UUID) -> str:
        """Get workspace owner ID.

        Issue #275 DRY: Common helper to avoid code duplication.

        Args:
            workspace_id: Workspace ID

        Returns:
            Owner user ID

        Raises:
            ValidationError: If no owner found (should never happen)
        """
        owner_id = await self.db.scalar(
            select(WorkspaceMember.user_id).where(
                and_(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role == "owner",
                )
            )
        )

        if not owner_id:
            logger.error(
                "no_workspace_owner_found",
                workspace_id=str(workspace_id),
            )
            raise ValidationError(f"No workspace owner found for workspace {workspace_id}")

        return owner_id

    async def _transfer_ownership(
        self,
        model_class,
        workspace_id: UUID,
        user_id: str,
        owner_id: str,
        filter_field: str,
        entity_name: str,
    ) -> int:
        """Transfer ownership of records to workspace owner.

        Issue #275 DRY: Generic ownership transfer to avoid code duplication.

        Args:
            model_class: SQLAlchemy model class
            workspace_id: Workspace ID (used for filtering and logging only)
            user_id: Current owner (being removed)
            owner_id: New owner (workspace owner)
            filter_field: Field name to filter by user_id ('user_id' or 'created_by')
            entity_name: Entity name for logging

        Returns:
            Number of records transferred
        """
        # Build filter conditions
        filters = [getattr(model_class, filter_field) == user_id]

        # Add workspace_id filter if model has it
        if hasattr(model_class, "workspace_id"):
            filters.append(model_class.workspace_id == workspace_id)

        result = cast(
            CursorResult[Any],
            await self.db.execute(
                update(model_class).where(and_(*filters)).values({filter_field: owner_id})
            ),
        )

        count = result.rowcount

        logger.info(
            f"{entity_name}_transferred_to_owner",
            workspace_id=str(workspace_id),
            from_user=user_id,
            to_owner=owner_id,
            count=count,
        )

        return count

    async def get_or_create_credentials(
        self,
        workspace_id: UUID,
        user_id: str,
        requester_id: str,
        requester_role: str | None = None,
    ) -> dict[str, Any]:
        """Get or create default API keys for a member (Lazy initialization).

        Note: OAuth Apps are managed separately via /oauth/clients API.

        Args:
            workspace_id: Workspace ID
            user_id: Target user ID
            requester_id: Requesting user ID (for permission check)
            requester_role: The requester's already-known workspace role, when a
                caller has resolved it (e.g. the programmatic owner-key GET path,
                where ``authorize_workspace_management`` already fetched the
                caller's membership). Threaded into ``_check_can_view`` to skip a
                duplicate ``WorkspaceMember`` SELECT. ``None`` → look it up.

        Returns:
            {
                "api_keys": [
                    {
                        "id": int,
                        "name": str,
                        "key_prefix": str,
                        "plaintext_key": str | None,  # Only if visible + owner
                        "is_visible": bool,
                        "visibility_expires_at": str | None,
                        "created_at": str,
                        "revoked_at": str | None,
                    },
                    ...
                ]
            }

        Raises:
            AuthorizationError: If requester doesn't have permission (403)
        """
        # Permission check: Can view credentials?
        await self._check_can_view(
            requester_id, workspace_id, user_id, requester_role=requester_role
        )

        # Get ALL api keys for user + workspace.
        # Issue #626: public-bound keys have ``workspace_id=NULL`` (mutually
        # exclusive with the #169 workspace scoping). To surface them in the
        # credentials UI for the workspace where their bound context lives,
        # OR in via the ``bound_context_id``'s workspace. Without this OR,
        # public-bound keys are invisible to ``get_or_create_credentials``
        # and the entire #626 frontend flow (binding badge, per-id revoke,
        # one-time-reveal of plaintext) renders nothing.
        from models.auth import Context as _Context

        result = await self.db.execute(
            select(APIKey)
            .where(
                and_(
                    APIKey.user_id == user_id,
                    or_(
                        APIKey.workspace_id == workspace_id,
                        APIKey.bound_context_id.in_(
                            select(_Context.id).where(_Context.workspace_id == workspace_id)
                        ),
                    ),
                    APIKey.revoked_at.is_(None),
                )
            )
            .order_by(APIKey.created_at.desc())
        )
        api_keys = result.scalars().all()

        # No lazy initialization - user creates keys manually

        # Determine visibility (Zero-knowledge!)
        is_owner = requester_id == user_id

        return {
            "api_keys": [self._serialize_api_key(k, show_secret=is_owner) for k in api_keys],
        }

    async def check_can_manage(
        self, requester_id: str, workspace_id: UUID, target_user_id: str, action: str
    ) -> bool:
        """Check if requester can perform action on target user's credentials.

        New permission matrix (Issue #xxx):
        - Self: create, regenerate, hide, revoke, delete
        - Owner: regenerate, revoke, delete (admin/member/viewer only, NOT owner)
        - Admin: regenerate, revoke, delete (member/viewer only)
        - Member/Viewer: self only

        Args:
            requester_id: Requesting user ID
            workspace_id: Workspace ID
            target_user_id: Target user ID
            action: Action to perform (create, regenerate, hide, revoke, delete)

        Returns:
            True if allowed, False otherwise
        """
        # Self-management: always allowed
        if requester_id == target_user_id:
            return action in ["create", "regenerate", "hide", "revoke", "delete"]

        # Get requester's workspace role
        requester_role = await self.get_workspace_role(requester_id, workspace_id)

        # Owner: Regenerate/Revoke/Delete admin/member/viewer (no create)
        if requester_role == "owner":
            target_role = await self.get_workspace_role(target_user_id, workspace_id)
            return action in ["regenerate", "revoke", "delete"] and target_role in [
                "admin",
                "member",
                "viewer",
            ]

        # Admin: Regenerate/Revoke/Delete member/viewer only (no create)
        if requester_role == "admin":
            target_role = await self.get_workspace_role(target_user_id, workspace_id)
            return action in ["regenerate", "revoke", "delete"] and target_role in [
                "member",
                "viewer",
            ]

        # Member/Viewer: No management of others
        return False

    async def _check_can_view(
        self,
        requester_id: str,
        workspace_id: UUID,
        target_user_id: str,
        requester_role: str | None = None,
    ) -> None:
        """Check if requester can view target user's credentials page.

        View permission:
        - Self: Always allowed
        - Owner/Admin: Can view others' pages (but not secrets)

        Args:
            requester_id: Requesting user ID
            workspace_id: Workspace ID
            target_user_id: Target user ID
            requester_role: The requester's already-known workspace role. When
                supplied (non-self path), it is trusted instead of re-querying
                ``WorkspaceMember`` — the programmatic owner-key GET path already
                resolved it via ``authorize_workspace_management``. ``None`` →
                look it up.

        Raises:
            AuthorizationError: If not allowed (403)
        """
        # Self: always allowed
        if requester_id == target_user_id:
            return

        # Check if requester is workspace owner/admin. Reuse a caller-provided
        # role to avoid a duplicate SELECT; otherwise look it up.
        if requester_role is None:
            requester_role = await self.get_workspace_role(requester_id, workspace_id)

        if requester_role not in ["owner", "admin"]:
            raise AuthorizationError("Insufficient permissions")

    async def get_workspace_role(self, user_id: str, workspace_id: UUID) -> str | None:
        """Get user's role in workspace.

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            Role string (owner/admin/member/viewer) or None if not a member
        """
        result = await self.db.execute(
            select(WorkspaceMember).where(
                and_(
                    WorkspaceMember.user_id == user_id,
                    WorkspaceMember.workspace_id == workspace_id,
                )
            )
        )
        member = result.scalar_one_or_none()

        return member.role if member else None

    async def cleanup_member_credentials(self, workspace_id: UUID, user_id: str) -> dict[str, Any]:
        """Clean up all credentials for a removed member.

        Issue #196: Called when member is removed from workspace
        (manual removal or auto-removal on plan downgrade).
        Issue #275: Added ExternalAPIKey cleanup.

        Deletes:
        - All API keys for this user + workspace
        - All OAuth clients for this user + workspace
        - All OAuth tokens for this user + workspace
        - All workspace-scoped external API keys

        Args:
            workspace_id: Workspace ID
            user_id: User ID being removed

        Returns:
            Dict with cleanup stats:
                - api_keys_deleted: int
                - oauth_clients_deleted: int
                - oauth_tokens_revoked: int
                - external_keys_deleted: int
        """
        # 1. Delete API keys (bulk operation)
        # Issue #200: Use rowcount for efficient counting without loading data
        api_keys_result = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(APIKey).where(
                    and_(APIKey.user_id == user_id, APIKey.workspace_id == workspace_id)
                )
            ),
        )
        api_keys_count = api_keys_result.rowcount

        # 2. Delete OAuth clients and get client_ids for token revocation
        # Issue #200: Use delete().returning() + scalars() for efficient data retrieval
        oauth_clients_result = await self.db.execute(
            delete(OAuth2Client)
            .where(
                and_(OAuth2Client.owner_id == user_id, OAuth2Client.workspace_id == workspace_id)
            )
            .returning(OAuth2Client.client_id)
        )
        client_ids = list(oauth_clients_result.scalars().all())
        oauth_clients_count = len(client_ids)

        # 3. Revoke OAuth tokens for this user's OAuth clients (bulk update)

        if client_ids:
            # Issue #200: Use rowcount for efficient counting without loading data
            now = utcnow()
            tokens_result = cast(
                CursorResult[Any],
                await self.db.execute(
                    update(OAuth2Token)
                    .where(
                        and_(
                            OAuth2Token.client_id.in_(client_ids),
                            OAuth2Token.revoked == False,  # noqa: E712
                        )
                    )
                    .values(
                        revoked=True,
                        access_token_revoked_at=now,
                        refresh_token_revoked_at=now,
                    )
                ),
            )
            tokens_count = tokens_result.rowcount
        else:
            tokens_count = 0

        # 4. Delete workspace-scoped ExternalAPIKeys (Issue #275)
        ext_keys_result = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(ExternalAPIKey).where(
                    and_(
                        ExternalAPIKey.user_id == user_id,
                        ExternalAPIKey.workspace_id == workspace_id,
                    )
                )
            ),
        )
        ext_keys_count = ext_keys_result.rowcount

        logger.info(
            "member_credentials_cleaned_up",
            workspace_id=str(workspace_id),
            user_id=user_id,
            api_keys=api_keys_count,
            oauth_clients=oauth_clients_count,
            tokens_revoked=tokens_count,
            external_keys=ext_keys_count,
        )

        return {
            "api_keys_deleted": api_keys_count,
            "oauth_clients_deleted": oauth_clients_count,
            "oauth_tokens_revoked": tokens_count,
            "external_keys_deleted": ext_keys_count,
        }

    async def cleanup_context_members(self, workspace_id: UUID, user_id: str) -> dict[str, Any]:
        """Clean up all context memberships for a removed member.

        Issue #275 Task 10: Called when member is removed from workspace.
        Deletes all ContextMember records for this user across all workspace contexts.

        Args:
            workspace_id: Workspace ID
            user_id: User ID being removed

        Returns:
            Dict with cleanup stats:
                - context_members_deleted: int
        """
        # Get all context IDs for this workspace
        contexts_result = await self.db.execute(
            select(Context.id).where(
                and_(
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                )
            )
        )
        context_ids = [row[0] for row in contexts_result.all()]

        # Delete context members (bulk operation)
        if context_ids:
            context_members_result = cast(
                CursorResult[Any],
                await self.db.execute(
                    delete(ContextMember).where(
                        and_(
                            ContextMember.context_id.in_(context_ids),
                            ContextMember.user_id == user_id,
                        )
                    )
                ),
            )
            context_members_count = context_members_result.rowcount
        else:
            context_members_count = 0

        logger.info(
            "context_members_cleaned_up",
            workspace_id=str(workspace_id),
            user_id=user_id,
            context_members_deleted=context_members_count,
        )

        return {
            "context_members_deleted": context_members_count,
        }

    async def cleanup_member_memories(
        self, workspace_id: UUID, user_id: str, owner_id: str | None = None
    ) -> dict[str, Any]:
        """Transfer member's memories to workspace owner.

        Issue #275 Critical: Organizational knowledge should not be lost when member leaves.
        Issue #275 DRY: Uses common _transfer_ownership() pattern.

        Pattern: Simple model with workspace_id field.

        Args:
            workspace_id: Workspace ID
            user_id: User ID being removed
            owner_id: Optional workspace owner ID (N+1 optimization)

        Returns:
            Dict with transfer stats
        """
        from models.memory import Memory

        # Get owner if not provided (fallback for single member removal)
        if owner_id is None:
            owner_id = await self._get_workspace_owner(workspace_id)

        # Transfer memories using common pattern
        memories_transferred = await self._transfer_ownership(
            model_class=Memory,
            workspace_id=workspace_id,
            user_id=user_id,
            owner_id=owner_id,
            filter_field="user_id",
            entity_name="memories",
        )

        # Clear deleted_by references (orphaned audit trail)
        await self.db.execute(
            update(Memory)
            .where(
                and_(
                    Memory.workspace_id == workspace_id,
                    Memory.deleted_by == user_id,
                )
            )
            .values(deleted_by=None)
        )

        return {
            "memories_transferred": memories_transferred,
            "new_owner": owner_id,
        }

    async def cleanup_member_contexts(
        self, workspace_id: UUID, user_id: str, owner_id: str | None = None
    ) -> dict[str, Any]:
        """Transfer context ownership to workspace owner.

        Issue #275 Critical: Context ownership should be transferred when member leaves.
        Issue #275 DRY: Uses common _transfer_ownership() pattern.

        Pattern: Simple model with workspace_id field.

        Args:
            workspace_id: Workspace ID
            user_id: User ID being removed
            owner_id: Optional workspace owner ID (N+1 optimization)

        Returns:
            Dict with transfer stats
        """
        # Get owner if not provided (fallback for single member removal)
        if owner_id is None:
            owner_id = await self._get_workspace_owner(workspace_id)

        # Transfer contexts using common pattern
        contexts_transferred = await self._transfer_ownership(
            model_class=Context,
            workspace_id=workspace_id,
            user_id=user_id,
            owner_id=owner_id,
            filter_field="created_by",
            entity_name="contexts",
        )

        return {
            "contexts_transferred": contexts_transferred,
            "new_owner": owner_id,
        }

    async def cleanup_member_resource_tokens(
        self, workspace_id: UUID, user_id: str, owner_id: str | None = None
    ) -> dict[str, Any]:
        """Transfer resource token ownership to workspace owner.

        Issue #275 Critical: Resource tokens should remain active when member leaves.
        Issue #275 Security: Filter by workspace via Context.resource_id relationship.

        Pattern: Special case - NO direct workspace_id field.
        Cannot use _transfer_ownership() due to indirect workspace relationship.

        ResourceToken relationship chain:
        ResourceToken.resource_id → Context.resource_id → Context.workspace_id

        Args:
            workspace_id: Workspace ID
            user_id: User ID being removed
            owner_id: Optional workspace owner ID (N+1 optimization)

        Returns:
            Dict with transfer stats
        """
        from models.resource import ResourceToken

        # Get owner if not provided (fallback for single member removal)
        if owner_id is None:
            owner_id = await self._get_workspace_owner(workspace_id)

        # SECURITY: Get resource_ids via Context to enforce workspace boundary
        # Cannot filter ResourceToken.workspace_id (field doesn't exist)
        resource_ids_result = await self.db.execute(
            select(Context.resource_id)
            .where(
                and_(
                    Context.workspace_id == workspace_id,
                    Context.resource_id.is_not(None),
                )
            )
            .distinct()
        )
        resource_ids = [row[0] for row in resource_ids_result.all()]

        # Transfer resource tokens only for this workspace's resource_ids
        if resource_ids:
            result = cast(
                CursorResult[Any],
                await self.db.execute(
                    update(ResourceToken)
                    .where(
                        and_(
                            ResourceToken.created_by == user_id,
                            ResourceToken.resource_id.in_(resource_ids),  # ✅ Workspace boundary
                        )
                    )
                    .values(created_by=owner_id)
                ),
            )
            count = result.rowcount
        else:
            count = 0

        logger.info(
            "resource_tokens_transferred_to_owner",
            workspace_id=str(workspace_id),
            from_user=user_id,
            to_owner=owner_id,
            count=count,
            resource_ids_count=len(resource_ids),
        )

        return {"resource_tokens_transferred": count}

    async def cleanup_member_invitations(self, workspace_id: UUID, user_id: str) -> dict[str, Any]:
        """Delete workspace invitations sent by the removed member.

        Issue #275 Critical: Orphaned invitations should be cleaned up.

        Args:
            workspace_id: Workspace ID
            user_id: User ID being removed

        Returns:
            Dict with cleanup stats:
                - invitations_deleted: int
        """
        from models.auth import WorkspaceInvitation

        # Delete invitations created by this member (pending only)
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(WorkspaceInvitation).where(
                    and_(
                        WorkspaceInvitation.workspace_id == workspace_id,
                        WorkspaceInvitation.invited_by == user_id,
                        WorkspaceInvitation.accepted_at.is_(None),  # Pending invitations only
                    )
                )
            ),
        )

        logger.info(
            "member_invitations_deleted",
            workspace_id=str(workspace_id),
            user_id=user_id,
            invitations_deleted=result.rowcount,
        )

        return {"invitations_deleted": result.rowcount}

    def _serialize_api_key(self, api_key: APIKey, show_secret: bool) -> dict[str, Any]:
        """Serialize API key with zero-knowledge enforcement.

        Migration 035: Decrypt plaintext_encrypted if visible + owner.

        Args:
            api_key: APIKey instance
            show_secret: Whether to include plaintext key (owner only)

        Returns:
            Serialized API key dict
        """

        # Check visibility: not hidden AND (no expiration OR not expired yet)
        is_visible = api_key.hidden_at is None and (
            api_key.visibility_expires_at is None or api_key.visibility_expires_at > utcnow()
        )

        # Plaintext key: Decrypt if visible + owner
        plaintext_key = None
        if show_secret and is_visible and api_key.plaintext_encrypted:
            try:
                from utils.encryption import get_encryptor

                plaintext_key = get_encryptor().decrypt(api_key.plaintext_encrypted)
            except Exception as e:
                logger.error("api_key_decryption_failed", key_id=api_key.id, error=str(e))

        return {
            "id": api_key.id,
            "name": api_key.name,
            "key_prefix": api_key.key_prefix,
            "plaintext_key": plaintext_key,
            "is_visible": is_visible,
            "visibility_expires_at": to_utc_iso(api_key.visibility_expires_at),
            "created_at": to_utc_iso(api_key.created_at),
            "last_used_at": to_utc_iso(api_key.last_used_at),
            "revoked_at": to_utc_iso(api_key.revoked_at),
            "expires_at": to_utc_iso(api_key.expires_at),  # #1165: surface owner-set expiry
            "bound_context_id": (
                str(api_key.bound_context_id) if api_key.bound_context_id else None
            ),
        }
