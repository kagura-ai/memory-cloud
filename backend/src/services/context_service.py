"""Context Service for multi-collection memory workspace.

Issue #82: Context-based Multi-Collection Support
Issue #115 Phase B: Workspace-level Multi-tenancy

Manages workspace contexts and their associated Qdrant collections.
Each context maps to a separate collection for memory isolation.
Contexts are owned by workspaces, not individual users.
"""

import re
from uuid import UUID

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember
from models.sleep import SleepMode
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Constants
DEFAULT_CONTEXT_NAME = "default"
DEFAULT_CONTEXT_DESCRIPTION = "Default context (auto-created)"
CONTEXT_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")


class ContextService:
    """Service for managing user contexts.

    Each context maps to a separate Qdrant collection, allowing users
    to organize memories into isolated namespaces (work, personal, etc.).

    Attributes:
        db: AsyncSession for database access
    """

    def __init__(self, db: AsyncSession):
        """Initialize context service.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db

    # ========================================================================
    # Collection Name Utilities
    # ========================================================================

    @staticmethod
    def validate_context_name(name: str) -> None:
        """Validate context name format.

        Args:
            name: Context name to validate

        Raises:
            ValidationError: If name is invalid
        """
        if not name:
            raise ValidationError("Context name cannot be empty")

        if len(name) > 100:
            raise ValidationError("Context name must be 100 characters or less")

        if not CONTEXT_NAME_PATTERN.match(name):
            raise ValidationError(
                "Context name must contain only lowercase letters, numbers, hyphens, and underscores"
            )

    # ========================================================================
    # Context CRUD Operations
    # ========================================================================

    async def create_context(
        self,
        workspace_id: UUID,
        name: str,
        display_name: str | None = None,
        description: str | None = None,
        summary: str | None = None,
        usage_guide: str | None = None,
        created_by: str | None = None,
        create_collection: bool = True,
        embedding_model: str | None = None,  # DEPRECATED: Use EMBEDDING_MODEL env var
        is_private: bool = True,
    ) -> Context:
        """Create a new context for workspace.

        Issue #115 Phase B: Contexts are now owned by workspaces, not users.
        Issue #146: Contexts require embedding model selection (immutable).
        Issue #160: Added summary and usage_guide for LLM context understanding.
        Issue #165: Added privacy control and role-based permissions.

        Args:
            workspace_id: Workspace ID (owner)
            name: Context name (lowercase alphanumeric + hyphen/underscore)
            display_name: Human-readable display name (optional, defaults to name)
            description: Optional context description
            summary: Optional LLM-oriented context summary (200-500 chars)
            usage_guide: Optional LLM-oriented memory usage guidelines
            created_by: User ID who created this context (optional)
            create_collection: Whether to create Qdrant collection (default: True)
            embedding_model: Embedding model to use (default: small, immutable after creation)
            is_private: Privacy flag (default: TRUE = private, FALSE = shared)

        Returns:
            Created Context instance

        Raises:
            ValidationError: If name invalid, exists, role insufficient, or plan tier insufficient
        """
        # Get workspace for validation
        from models.auth import Workspace, WorkspaceMember

        stmt = select(Workspace).where(Workspace.id == workspace_id)
        result = await self.db.execute(stmt)
        workspace = result.scalar_one_or_none()
        if not workspace:
            raise ValidationError(f"Workspace {workspace_id} not found")

        # Role-based validation (Issue #165)
        if created_by:
            stmt = select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == created_by,
            )
            result = await self.db.execute(stmt)
            workspace_member = result.scalar_one_or_none()

            if workspace_member:
                # Check role: Owner/Admin can create contexts
                if workspace_member.role not in ["owner", "admin"]:
                    raise ValidationError("Only workspace owners and admins can create contexts.")

                # Private contexts: Owner only
                if is_private and workspace_member.role != "owner":
                    raise ValidationError(
                        "Only workspace owners can create private contexts. "
                        "Admins can create shared contexts."
                    )

        # Plan tier validation for shared contexts (Issue #165)
        if not is_private:
            if workspace.plan_name in ["free", "basic"]:
                raise ValidationError(
                    "Shared contexts require Pro plan. Upgrade to share contexts with team members."
                )

        # Determine embedding model: parameter > global setting
        from config.constants import EMBEDDING_MODEL_REGISTRY

        settings = get_settings()
        actual_embedding_model = embedding_model or settings.embedding_model
        if actual_embedding_model in EMBEDDING_MODEL_REGISTRY:
            actual_dimensions = EMBEDDING_MODEL_REGISTRY[actual_embedding_model][0]
        else:
            actual_dimensions = settings.embedding_dimensions

        # Validate name format
        self.validate_context_name(name)

        # Check if context already exists in this workspace
        existing = await self.get_context_by_name_for_workspace(workspace_id, name)
        if existing:
            raise ValidationError(f"Context '{name}' already exists in this workspace")

        # Create context record
        context = Context(
            workspace_id=workspace_id,
            name=name,
            display_name=display_name if display_name else name,  # Default to name
            description=description,
            summary=summary,
            usage_guide=usage_guide,
            created_by=created_by,
            is_private=is_private,
        )
        self.db.add(context)
        await self.db.commit()
        await self.db.refresh(context)

        logger.info(
            "context_created",
            workspace_id=str(workspace_id),
            context_id=str(context.id),
            context_name=name,
            created_by=created_by,
            embedding_model=actual_embedding_model,
            embedding_dimensions=actual_dimensions,
        )

        # Create context search config with embedding settings (Issue #146)
        from models.config import ContextSearchConfig

        search_config = ContextSearchConfig(
            context_id=context.id,
            semantic_weight=0.6,  # Default
            fetch_factor=3,  # Default
            use_rerank=False,  # Default: OFF (Issue #146)
            reranker_provider="voyage",  # Default
            reranker_model="rerank-2-lite",  # Default
            embedding_model=actual_embedding_model,
            embedding_dimensions=actual_dimensions,
        )
        self.db.add(search_config)
        await self.db.commit()

        logger.info(
            "context_search_config_created",
            context_id=str(context.id),
            embedding_model=actual_embedding_model,
            dimensions=actual_dimensions,
        )

        # Create Qdrant collection (model-specific)
        if create_collection:
            from db.qdrant import get_collection_name

            collection = get_collection_name(actual_embedding_model, actual_dimensions)
            await self._ensure_context_collection(
                str(workspace_id), name, embedding_dim=actual_dimensions, collection_name=collection
            )

        # Note: Do NOT auto-set as current context
        # Users should explicitly switch to the new context if desired

        return context

    async def list_contexts_for_workspace(self, workspace_id: UUID) -> list[Context]:
        """List all contexts for workspace.

        Issue #115 Phase B: Workspace-based context listing.

        Args:
            workspace_id: Workspace ID

        Returns:
            List of Context instances
        """
        result = await self.db.execute(
            select(Context)
            .where(Context.workspace_id == workspace_id, Context.deleted_at.is_(None))
            .order_by(Context.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_contexts(self, user_id: str) -> list[Context]:
        """List all contexts accessible to user (via workspace membership).

        Issue #115 Phase B: Lists contexts from user's current workspace.
        Issue #234: Respects allowed_context_ids whitelist for member/viewer.

        Args:
            user_id: User ID

        Returns:
            List of Context instances
        """
        from services.permission_service import PermissionService

        # Get user's current workspace
        workspace_id = await self._get_user_current_workspace_id(user_id)
        if not workspace_id:
            return []

        # Use PermissionService for proper access filtering (Issue #234)
        perm_service = PermissionService(self.db)
        try:
            return await perm_service.get_accessible_contexts(user_id, workspace_id)
        except Exception:
            return []

    async def get_context(self, user_id: str, context_id: UUID) -> Context:
        """Get context by ID.

        Issue #115 Phase B: Verifies context access via workspace membership.
        Issue #234: Respects allowed_context_ids whitelist for member/viewer.

        Args:
            user_id: User ID (for access verification via workspace membership)
            context_id: Context UUID

        Returns:
            Context instance

        Raises:
            NotFoundException: If context not found or user has no access
        """
        result = await self.db.execute(
            select(Context).where(
                Context.id == context_id,
                Context.deleted_at.is_(None),
            )
        )
        context = result.scalar_one_or_none()

        if not context:
            raise NotFoundException(f"Context not found: {context_id}")

        # Get workspace membership
        workspace_member = await self._get_workspace_member(user_id, context.workspace_id)
        if not workspace_member:
            raise NotFoundException(f"Context not found: {context_id}")

        # Issue #165: Privacy check - private contexts are creator-only
        if context.is_private and context.created_by != user_id:
            raise NotFoundException(f"Context not found: {context_id}")

        # Issue #234: Check allowed_context_ids whitelist for member/viewer
        if workspace_member.role in ("member", "viewer"):
            if workspace_member.allowed_context_ids is not None:
                if context_id not in workspace_member.allowed_context_ids:
                    raise NotFoundException(f"Context not found: {context_id}")

        return context

    async def get_context_by_name_for_workspace(
        self, workspace_id: UUID, name: str
    ) -> Context | None:
        """Get context by name within an workspace.

        Issue #115 Phase B: Workspace-scoped context lookup.

        Args:
            workspace_id: Workspace ID
            name: Context name

        Returns:
            Context instance or None if not found
        """
        result = await self.db.execute(
            select(Context).where(
                Context.workspace_id == workspace_id,
                Context.name == name,
                Context.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_context_by_name(self, user_id: str, name: str) -> Context | None:
        """Get context by name (for user's current workspace).

        Issue #115 Phase B: Backward-compatible method that uses current workspace.
        Issue #234: Respects allowed_context_ids whitelist for member/viewer.

        Args:
            user_id: User ID
            name: Context name

        Returns:
            Context instance or None if not found
        """
        workspace_id = await self._get_user_current_workspace_id(user_id)
        if not workspace_id:
            return None

        context = await self.get_context_by_name_for_workspace(workspace_id, name)
        if not context:
            return None

        # Issue #234: Use get_context to apply whitelist check
        try:
            return await self.get_context(user_id, context.id)
        except Exception:
            return None  # Access denied or not found

    async def update_context(
        self,
        user_id: str,
        context_id: UUID,
        display_name: str | None = None,
        description: str | None = None,
        summary: str | None = None,
        usage_guide: str | None = None,
        is_private: bool | None = None,
        is_public: bool | None = None,  # Issue #238
        resource_id: str | None = None,  # Issue #238
        is_locked: bool | None = None,  # Issue #85
        sleep_mode: SleepMode | None = None,
    ) -> Context:
        """Update context display_name, description, summary, usage guide, and privacy.

        Issue #160: Added summary and usage_guide fields.
        Migration 034: Added is_private field (Shared → Private removes non-owner members).
        Migration 039: Added display_name field.
        Issue #238: Added is_public and resource_id fields for Public contexts.

        Args:
            user_id: User ID
            context_id: Context UUID
            display_name: New display name (None to leave unchanged)
            description: New description (None to leave unchanged)
            summary: New summary (None to leave unchanged)
            usage_guide: New usage guide (None to leave unchanged)
            is_private: New privacy setting (None to leave unchanged)
            is_public: New public access setting (None to leave unchanged)
            resource_id: Resource ID for public contexts (None to leave unchanged)
            is_locked: Lock status (None to leave unchanged)
            sleep_mode: Sleep maintenance mode (None to leave unchanged).

        Returns:
            Updated Context instance

        Raises:
            NotFoundException: If context not found
        """
        context = await self.get_context(user_id, context_id)

        if display_name is not None:
            context.display_name = display_name
        if description is not None:
            context.description = description
        if summary is not None:
            context.summary = summary
        if usage_guide is not None:
            context.usage_guide = usage_guide

        # Migration 034: Handle privacy transition
        if is_private is not None and context.is_private != is_private:
            await self._handle_privacy_transition(context, is_private, user_id)

        # Issue #238: Handle public access and resource_id
        if is_public is not None:
            logger.info(
                "setting_is_public",
                context_id=str(context_id),
                old_value=context.is_public,
                new_value=is_public,
            )
            # Issue #156: Clear resource_id when unpublishing
            if not is_public and context.is_public and context.resource_id:
                logger.info(
                    "clearing_resource_id_on_unpublish",
                    context_id=str(context_id),
                    old_resource_id=context.resource_id,
                )
                context.resource_id = None
            context.is_public = is_public
        if resource_id is not None:
            # Validate resource_id format (lowercase alphanumeric + underscore)
            import re

            if resource_id and not re.match(r"^[a-z0-9_]+$", resource_id):
                raise ValidationError(
                    f"Invalid resource_id format: {resource_id}. Must be lowercase alphanumeric and underscore only."
                )

            logger.info(
                "setting_resource_id",
                context_id=str(context_id),
                old_value=context.resource_id,
                new_value=resource_id,
            )
            context.resource_id = resource_id

        # Issue #85: Lock/unlock context
        if is_locked is not None:
            logger.info(
                "setting_is_locked",
                context_id=str(context_id),
                old_value=context.is_locked,
                new_value=is_locked,
            )
            context.is_locked = is_locked

        if sleep_mode is not None and context.sleep_mode != sleep_mode:
            logger.info(
                "setting_sleep_mode",
                context_id=str(context_id),
                old_value=context.sleep_mode,
                new_value=sleep_mode,
            )
            context.sleep_mode = sleep_mode

        await self.db.commit()
        await self.db.refresh(context)

        logger.info(
            "context_after_commit",
            context_id=str(context_id),
            is_public=context.is_public,
            resource_id=context.resource_id,
        )

        logger.info(
            "context_updated",
            user_id=user_id,
            context_id=str(context_id),
            privacy_changed=is_private is not None and context.is_private != is_private,
        )

        return context

    async def _handle_privacy_transition(
        self, context: Context, new_is_private: bool, owner_id: str
    ) -> None:
        """Handle context privacy transition.

        Migration 034: Shared → Private removes non-owner members.

        Args:
            context: Context instance
            new_is_private: New privacy setting
            owner_id: Context owner user ID
        """
        old_is_private = context.is_private

        # Shared → Private: Remove non-owner members
        if not old_is_private and new_is_private:
            from sqlalchemy import and_, delete

            result = await self.db.execute(
                delete(ContextMember).where(
                    and_(
                        ContextMember.context_id == context.id,
                        ContextMember.user_id != owner_id,
                    )
                )
            )

            removed_count = result.rowcount

            logger.info(
                "context_privacy_changed_to_private",
                context_id=str(context.id),
                owner_id=owner_id,
                members_removed=removed_count,
            )

            # Issue #234: Remove this context from all members' allowed_context_ids
            from sqlalchemy import func, update

            await self.db.execute(
                update(WorkspaceMember)
                .where(
                    WorkspaceMember.workspace_id == context.workspace_id,
                    WorkspaceMember.allowed_context_ids.isnot(None),
                )
                .values(
                    allowed_context_ids=func.array_remove(
                        WorkspaceMember.allowed_context_ids,
                        context.id,
                    )
                )
            )

            logger.info(
                "context_removed_from_allowed_context_ids",
                context_id=str(context.id),
            )

        # Update privacy flag
        context.is_private = new_is_private

        logger.info(
            "context_privacy_updated",
            context_id=str(context.id),
            old_is_private=old_is_private,
            new_is_private=new_is_private,
        )

    async def merge_contexts(
        self,
        user_id: str,
        source_context_id: UUID,
        target_context_id: UUID,
        delete_source: bool = False,
    ) -> dict:
        """Merge memories from source context into target context (Issue #90).

        Direct copy approach: no re-embedding needed for same embedding model.
        Copies memories in PostgreSQL and Qdrant points with updated context_id.

        Args:
            user_id: User ID (must own both contexts)
            source_context_id: Context to copy memories from
            target_context_id: Context to copy memories into
            delete_source: Soft-delete source after merge

        Returns:
            Dict with merge stats (merged count)

        Raises:
            NotFoundException: If context not found
            ValidationError: If contexts use different embedding models or other constraint violation
        """
        from uuid import uuid4

        from db.qdrant import copy_context_points, get_collection_name
        from models.config import ContextSearchConfig
        from models.memory import Memory
        from services.permission_service import PermissionService

        # Validate owner access to both contexts
        source = await self.get_context(user_id, source_context_id)
        target = await self.get_context(user_id, target_context_id)

        # Owner-only: merge is a privileged operation
        perm_service = PermissionService(self.db)
        await perm_service.check_context_owner(user_id, source_context_id)
        await perm_service.check_context_owner(user_id, target_context_id)

        # Same workspace required
        if source.workspace_id != target.workspace_id:
            raise ValidationError("Source and target contexts must be in the same workspace.")

        # Same context check
        if source_context_id == target_context_id:
            raise ValidationError("Source and target contexts must be different.")

        # Locked source with delete_source
        if delete_source and source.is_locked:
            raise ValidationError(
                "Source context is locked. Unlock it first via update_context(is_locked=false)."
            )

        # Validate same embedding model (single query for both configs)
        settings = get_settings()
        cfg_result = await self.db.execute(
            select(ContextSearchConfig).where(
                ContextSearchConfig.context_id.in_([source_context_id, target_context_id])
            )
        )
        configs = {row.context_id: row for row in cfg_result.scalars().all()}
        src_cfg = configs.get(source_context_id)
        tgt_cfg = configs.get(target_context_id)

        src_model = src_cfg.embedding_model if src_cfg else settings.embedding_model
        tgt_model = tgt_cfg.embedding_model if tgt_cfg else settings.embedding_model
        src_dims = src_cfg.embedding_dimensions if src_cfg else settings.embedding_dimensions
        tgt_dims = tgt_cfg.embedding_dimensions if tgt_cfg else settings.embedding_dimensions

        if src_model != tgt_model or src_dims != tgt_dims:
            raise ValidationError(
                f"Embedding model mismatch: source uses {src_model}({src_dims}d), "
                f"target uses {tgt_model}({tgt_dims}d). "
                "Only same-model merge is supported."
            )

        # Fetch source memories (non-deleted, with successful embeddings)
        result = await self.db.execute(
            select(Memory).where(
                Memory.context_id == source_context_id,
                Memory.deleted_at.is_(None),
                Memory.embedding_status == "success",
            )
        )
        source_memories = result.scalars().all()

        if not source_memories:
            # Still honor delete_source even if no memories to copy
            if delete_source:
                await self.delete_context(user_id, source_context_id)
            return {
                "merged": 0,
                "source_id": str(source_context_id),
                "target_id": str(target_context_id),
            }

        # Build ID mapping and copy memories in PostgreSQL
        memory_id_mapping: dict[str, str] = {}

        for mem in source_memories:
            new_id = uuid4()
            memory_id_mapping[str(mem.id)] = str(new_id)

            # Normalize nested context JSON to reference target context
            ctx_json = mem.context
            if isinstance(ctx_json, dict) and "context_id" in ctx_json:
                ctx_json = {**ctx_json, "context_id": str(target_context_id)}

            # Copy all data fields; resource_id/resource_doc_id/resource_version
            # are Computed columns (auto-derived from details)
            new_mem = Memory(
                id=new_id,
                user_id=mem.user_id,
                workspace_id=mem.workspace_id,
                context_id=target_context_id,
                summary=mem.summary,
                summary_embedding_id=new_id,
                context_summary=mem.context_summary,
                content=mem.content,
                context=ctx_json,
                details=mem.details,
                type=mem.type,
                importance=mem.importance,
                confidence=mem.confidence,
                tags=mem.tags,
                scope=mem.scope,
                long_term=mem.long_term,
                promoted_at=mem.promoted_at,
                use_count=0,
                access_count=0,
                client=mem.client,
                client_version=mem.client_version,
                source=mem.source,
                embedding_status="success",
                created_at=mem.created_at,
                updated_at=utcnow(),
            )
            self.db.add(new_mem)

        await self.db.flush()

        # Copy Qdrant points — rollback PG on failure for consistency
        collection = get_collection_name(src_model, src_dims)
        try:
            copied = await copy_context_points(
                workspace_id=str(source.workspace_id),
                source_context_id=str(source_context_id),
                target_context_id=str(target_context_id),
                memory_id_mapping=memory_id_mapping,
                collection_name=collection,
            )
        except Exception:
            await self.db.rollback()
            raise

        # Optional: delete source (_commit=False for atomic transaction)
        if delete_source:
            await self.delete_context(user_id, source_context_id, _commit=False)

        await self.db.commit()

        logger.info(
            "contexts_merged",
            source_context_id=str(source_context_id),
            target_context_id=str(target_context_id),
            merged=copied,
            delete_source=delete_source,
        )

        return {
            "merged": copied,
            "source_id": str(source_context_id),
            "target_id": str(target_context_id),
        }

    async def delete_context(
        self,
        user_id: str,
        context_id: UUID,
        _commit: bool = True,
    ) -> "Context":
        """Soft-delete context and its memories.

        Issue #84: Changed from hard-delete to soft-delete. Sets deleted_at
        timestamp instead of removing records, preserving data for recovery.
        Qdrant points are intentionally kept (filtered out by API access checks).

        Args:
            user_id: User ID (for access verification and audit trail)
            context_id: Context UUID

        Returns:
            The deleted Context object (with deleted_at set)

        Raises:
            NotFoundException: If context not found
            ValidationError: If trying to delete default context
        """
        context = await self.get_context(user_id, context_id)

        # Cannot delete default context
        if context.is_default:
            raise ValidationError("Cannot delete default context")

        # Issue #85: Guard for non-HTTP callers (MCP tools call service directly)
        if context.is_locked:
            raise ConflictError("Context is locked. Unlock it before deleting.")

        # Soft-delete memories
        from models.memory import Memory, NeuralMemoryEdge

        memories_result = await self.db.execute(
            select(Memory).where(
                and_(
                    Memory.workspace_id == context.workspace_id,
                    Memory.context_id == context.id,
                    Memory.deleted_at.is_(None),
                )
            )
        )
        memories_to_delete = list(memories_result.scalars().all())
        for memory in memories_to_delete:
            memory.deleted_at = utcnow()
            memory.deleted_by = user_id

        # Hard-delete neural edges (no soft-delete needed, reconstructable)
        await self.db.execute(
            delete(NeuralMemoryEdge).where(
                and_(
                    NeuralMemoryEdge.workspace_id == context.workspace_id,
                    NeuralMemoryEdge.context_id == context.id,
                )
            )
        )

        logger.info(
            "context_data_cleanup",
            workspace_id=str(context.workspace_id),
            context_name=context.name,
            memories_deleted=len(memories_to_delete),
        )

        # Remove context from all members' allowed_context_ids
        from sqlalchemy import func, update

        await self.db.execute(
            update(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == context.workspace_id,
                WorkspaceMember.allowed_context_ids.isnot(None),
            )
            .values(
                allowed_context_ids=func.array_remove(
                    WorkspaceMember.allowed_context_ids,
                    context.id,
                )
            )
        )

        # Issue #84: Soft-delete context record (previously hard-deleted)
        context.deleted_at = utcnow()
        context.deleted_by = user_id
        if _commit:
            await self.db.commit()

        logger.info(
            "context_soft_deleted",
            user_id=user_id,
            workspace_id=str(context.workspace_id),
            context_id=str(context_id),
            context_name=context.name,
        )

        return context

    # ========================================================================
    # Current Context Management
    # ========================================================================

    # Issue #246: get_current_context() and switch_context() removed
    # Context is now always explicit from Frontend URL or API parameter
    async def get_or_create_default_context(self, user_id: str) -> Context:
        """Get or create default context for user.

        Issue #213: DEPRECATED - automatic context creation removed.

        DEPRECATED: This method is deprecated. All users must manually create
        contexts via Web UI (/contexts). This method now always raises an error.

        Args:
            user_id: User ID

        Returns:
            Never returns (always raises exception)

        Raises:
            NotFoundException: Always raised (Issue #213)
        """
        # Issue #213: All users must create contexts manually (including default_user)
        logger.warning("context_creation_required", user_id=user_id)
        raise NotFoundException(
            "No context found. Please create a context in the web interface at /contexts"
        )

    # ========================================================================
    # Collection Resolution (DEPRECATED - Single Collection Migration)
    # ========================================================================
    # Note: get_user_collection_name() is no longer needed.
    # Collection is always "kagura_memories" internally.

    # ========================================================================
    # Qdrant Collection Management
    # ========================================================================

    async def _ensure_context_collection(
        self,
        workspace_id: str,
        context_name: str,
        embedding_dim: int = 512,
        collection_name: str | None = None,
    ) -> None:
        """Ensure the appropriate Qdrant collection exists for this context.

        Uses per-model collections (e.g., kagura_memories_qwen3_embedding_8b_4096).
        Default model (text-embedding-3-small, 512) maps to legacy "kagura_memories".

        Args:
            workspace_id: Workspace ID (as string)
            context_name: Context name (for logging)
            embedding_dim: Embedding dimension (default: 512)
            collection_name: Qdrant collection name (default: kagura_memories)
        """
        from db.qdrant import KAGURA_MEMORIES_COLLECTION, ensure_kagura_memories_collection

        target_collection = collection_name or KAGURA_MEMORIES_COLLECTION

        try:
            await ensure_kagura_memories_collection(embedding_dim, target_collection)

            logger.info(
                "single_collection_ensured",
                workspace_id=workspace_id,
                context_name=context_name,
                dim=embedding_dim,
            )

        except Exception as e:
            logger.error(
                "single_collection_ensure_failed",
                workspace_id=workspace_id,
                context_name=context_name,
                error=str(e),
            )
            raise

    async def _delete_context_collection(
        self,
        workspace_id: str,
        context_name: str,
        context_id: str | None = None,
    ) -> None:
        """Delete context's points from kagura_memories collection (single collection migration).

        Single Collection Migration: Instead of deleting an entire collection,
        this now deletes all points matching workspace_id + context_id from "kagura_memories".

        Args:
            workspace_id: Workspace ID (as string)
            context_name: Context name (for logging only)
            context_id: Context ID (required for point deletion)
        """
        from db.qdrant import delete_context_points

        try:
            if not context_id:
                logger.warning(
                    "context_id_missing_for_deletion",
                    workspace_id=workspace_id,
                    context_name=context_name,
                )
                return

            # Single collection migration: Delete points for this context
            await delete_context_points(workspace_id, context_id)

            logger.info(
                "context_points_deleted",
                workspace_id=workspace_id,
                context_id=context_id,
                context_name=context_name,
            )

        except Exception as e:
            logger.error(
                "context_points_deletion_failed",
                workspace_id=workspace_id,
                context_id=context_id,
                context_name=context_name,
                error=str(e),
            )
            raise

    async def get_context_stats(
        self,
        user_id: str,
        context_id: UUID,
    ) -> dict:
        """Get context statistics (memory count) from PostgreSQL.

        Single Collection Migration: Stats are now from PostgreSQL, not Qdrant collection.

        Args:
            user_id: User ID
            context_id: Context UUID

        Returns:
            Dict with stats (memory_count, etc.)
        """
        from models.memory import Memory

        context = await self.get_context(user_id, context_id)

        try:
            # Count memories in this context (from PostgreSQL)
            count_result = await self.db.execute(
                select(func.count(Memory.id)).where(
                    and_(
                        Memory.workspace_id == context.workspace_id,
                        Memory.context_id == context.id,
                        Memory.deleted_at.is_(None),
                    )
                )
            )
            memory_count = count_result.scalar() or 0

            return {
                "context_id": str(context_id),
                "context_name": context.name,
                "memory_count": memory_count,
                "status": "active",
            }

        except Exception as e:
            logger.warning(
                "context_stats_failed",
                context_id=str(context_id),
                error=str(e),
            )
            return {
                "context_id": str(context_id),
                "context_name": context.name,
                "memory_count": 0,
                "status": "error",
            }

    # ========================================================================
    # Helper Methods (Issue #115 Phase B)
    # ========================================================================

    async def _get_user_current_workspace_id(self, user_id: str) -> UUID | None:
        """Get user's current workspace ID.

        Args:
            user_id: User ID

        Returns:
            Workspace UUID or None if user has no workspace
        """
        result = await self.db.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            return None

        return user.current_workspace_id

    async def _is_workspace_member(self, user_id: str, workspace_id: UUID) -> bool:
        """Check if user is a member of workspace.

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            True if user is a member
        """
        result = await self.db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def _get_workspace_member(
        self, user_id: str, workspace_id: UUID
    ) -> WorkspaceMember | None:
        """Get workspace member record.

        Issue #234: Returns full member record for whitelist checking.

        Args:
            user_id: User ID
            workspace_id: Workspace ID

        Returns:
            WorkspaceMember instance or None
        """
        result = await self.db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def _ensure_personal_workspace(self, user_id: str) -> UUID:
        """Ensure user has a personal workspace.

        Creates one if it doesn't exist and sets it as current.

        Args:
            user_id: User ID

        Returns:
            Workspace UUID
        """

        # Get user info
        result = await self.db.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            raise NotFoundException(f"User not found: {user_id}")

        # Check if user already has an workspace (just not set as current)
        member_result = await self.db.execute(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
        )
        existing_membership = member_result.scalar_one_or_none()

        if existing_membership:
            # User has an workspace, set it as current
            user.current_workspace_id = existing_membership.workspace_id
            await self.db.commit()
            return existing_membership.workspace_id

        # Create new personal workspace
        user_name = user.name or user.email or "User"

        workspace = Workspace(
            name=f"{user_name}'s Workspace",
            description="Personal workspace (auto-created)",
            owner_user_id=user_id,
        )
        self.db.add(workspace)
        await self.db.flush()  # Get the ID

        # Add user as owner
        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user_id,
            role="owner",
        )
        self.db.add(membership)

        # Set as current workspace
        user.current_workspace_id = workspace.id
        await self.db.commit()

        logger.info(
            "personal_workspace_created",
            user_id=user_id,
            workspace_id=str(workspace.id),
        )

        return workspace.id

    # ========================================================================
    # Access Control Helpers
    # ========================================================================

    async def is_context_shared(self, context_id: UUID) -> bool:
        """Check if context is shared (is_private=false).

        Issue #XXX: Team collaboration - shared contexts allow workspace member access.

        Args:
            context_id: Context UUID

        Returns:
            True if context is shared (is_private=false), False otherwise

        Note:
            Returns False if context not found or is_private is NULL (safe default)
        """
        result = await self.db.execute(
            select(Context.is_private).where(
                Context.id == context_id,
                Context.deleted_at.is_(None),
            )
        )
        context_is_private = result.scalar_one_or_none()

        # Safe default: treat as private if context not found or NULL
        return context_is_private is False
