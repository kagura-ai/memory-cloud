"""Context Service for multi-collection memory workspace.

Issue #82: Context-based Multi-Collection Support
Issue #115 Phase B: Workspace-level Multi-tenancy

Manages workspace contexts and their associated Qdrant collections.
Each context maps to a separate collection for memory isolation.
Contexts are owned by workspaces, not individual users.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast, get_args
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import ObjectDeletedError

from auth.workspace_roles import WorkspaceRole
from config.plan_tiers import (
    has_feature,
    lowest_tier_with_limit,
    plan_display_name,
    quota_gate_details,
)
from config.settings import get_settings
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember
from models.sleep import SleepMode
from services.capacity_lock import CapacityPolicy
from services.identity_link_service import is_same_owner, owned_by
from services.plan_seats import ensure_member_seated
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    ExportTooLargeError,
    FeatureNotAvailableError,
    NotFoundException,
    QuotaExceededError,
    ValidationError,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from models.schemas import ContextExportResponse

logger = get_logger(__name__)

DEFAULT_CONTEXT_NAME = "default"
DEFAULT_CONTEXT_DESCRIPTION = "Default context (auto-created)"
CONTEXT_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")

# Issue #950: hard cap on a single context-portability export. One JSON body,
# no pagination — this valve keeps the contract stable until a streaming /
# workspace-wide variant lands. Sized well above any realistic closed-beta
# context; an over-cap context raises ExportTooLargeError (413).
EXPORT_MAX_MEMORIES = 50_000

# Issue #614: list_tags sort modes — single source of truth shared by REST
# Query type and MCP handler validation.
TagSortMode = Literal["count", "recent", "alpha"]

#: ``deleted_by`` written by the capacity-over purge (#1969): not a user id, so an
#: audit reader can tell policy deletions from the owner's own.
CAPACITY_PURGE_ACTOR = "system:capacity-purge"
#: Memories tombstoned per bulk UPDATE (and commit) by the capacity purge (#1969):
#: a former paid workspace can hold 100k memories, and the purge runs in the API
#: process — no ORM load of a whole context, no transaction across all of it.
PURGE_MEMORY_CHUNK = 1000


@dataclass(frozen=True)
class WorkspacePurgeReport:
    """What :meth:`ContextService.purge_workspace_data` removed.

    ``files_deleted`` counts uploaded rows (what a file list shows); cancelled
    reservations are purged and their bytes are in ``bytes_released``.
    ``aborted``: the purge stopped early because ``should_continue`` said so —
    the counts are what was removed before that.
    """

    contexts_deleted: int
    contexts_emptied: int
    memories_deleted: int
    files_deleted: int
    bytes_released: int
    aborted: bool = False


class ContextListingFailedError(Exception):
    """The permission-scoped context lookup failed (#1658).

    Raised by ``ContextService.list_contexts(..., raise_on_lookup_error=True)``
    only, so a caller can tell "the lookup failed" from "nothing is visible"
    without changing the empty-list fallback every other caller relies on.
    """


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
        #: Redis bytes a ``delete_context`` purged, released after the commit
        #: that made the purge durable (#1960, ``_release_pending_storage``).
        self._storage_release_pending: dict[UUID, int] = {}

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
            ValidationError: If name invalid, exists, or role insufficient
            FeatureNotAvailableError: If the workspace plan does not carry
                ``shared_contexts`` and ``is_private`` is False (#1644 S11)
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
                if workspace_member.role not in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
                    raise ValidationError("Only workspace owners and admins can create contexts.")

                # Private contexts: Owner only
                if is_private and workspace_member.role != WorkspaceRole.OWNER:
                    raise ValidationError(
                        "Only workspace owners can create private contexts. "
                        "Admins can create shared contexts."
                    )

        # #1941: a workspace over its Free capacity cannot grow until it is
        # cleaned up. After the role check, so a non-member learns nothing.
        from services.capacity_lock import ensure_not_capacity_locked

        await ensure_not_capacity_locked(self.db, workspace)

        # Plan tier validation for shared contexts (Issue #165)
        if not is_private:
            # Feature-based (#1548): the tier registry decides, so a new tier
            # is never silently excluded and an unknown tier fails closed.
            #
            # #1644 S11: raised as ``FeatureNotAvailableError`` (403
            # ``FEAT-001``), not ``ValidationError`` (422 ``VAL-001``). The
            # REST route already answers 403 ``FEAT-001`` for this exact
            # condition (``api/routes/contexts.py``), so one condition was
            # producing two different refusals depending on which door the
            # caller came through — MCP and ``workspace_service`` reach the
            # service directly.
            if not has_feature(workspace.plan_name, "shared_contexts"):
                raise FeatureNotAvailableError.for_feature(workspace.plan_name, "shared_contexts")

        # Determine embedding model: parameter > global setting
        from config.constants import EMBEDDING_MODEL_REGISTRY
        from config.embedding_policy import allowed_embedding_models

        settings = get_settings()
        # #1517: gate the CALLER-supplied model here rather than in the routes.
        # This is the one point every caller funnels through — REST, MCP, and
        # ``workspace_service``, which calls the service directly and so bypassed
        # both route checks. Getting it wrong is unrecoverable in place: the
        # embedding model is immutable after creation (#146).
        if embedding_model:
            allowed = allowed_embedding_models(settings.embedding_model_allowlist)
            if embedding_model not in allowed:
                # Distinguish "no such model" from "not offered here", because
                # the fix differs: correct the name vs. ask the operator.
                if embedding_model not in EMBEDDING_MODEL_REGISTRY:
                    raise ValidationError(
                        f"Unknown embedding model: {embedding_model}. "
                        f"Supported: {', '.join(allowed)}"
                    )
                raise ValidationError(
                    f"Embedding model not available on this deployment: "
                    f"{embedding_model}. Available: {', '.join(allowed)}"
                )

        actual_embedding_model = embedding_model or settings.embedding_model
        if actual_embedding_model in EMBEDDING_MODEL_REGISTRY:
            actual_dimensions = EMBEDDING_MODEL_REGISTRY[actual_embedding_model][0]
        else:
            # Only reachable via the operator-configured default, which the
            # gate above deliberately does not police: a self-host pointing
            # EMBEDDING_MODEL at a custom model is a deliberate act, and
            # EMBEDDING_DIMENSIONS is how they declare its size.
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
        from repositories.config_repository import new_search_config_row
        from services.content_chunk_policy import new_context_content_chunks

        # #2103 review F1: the default turns chunks on only where the plan gate
        # would let the owner do it (under the cap's lock, held to the commit).
        chunks_on = await new_context_content_chunks(
            self.db, workspace_id, default_on=settings.default_content_chunks_enabled
        )
        search_config = ContextSearchConfig(
            context_id=context.id,
            semantic_weight=0.6,  # Default
            fetch_factor=3,  # Default
            # #1572: reranker fields follow the deployment default (DEFAULT_USE_RERANK /
            # DEFAULT_RERANKER_PROVIDER / DEFAULT_RERANKER_MODEL); unset they are
            # today's values (off / voyage / rerank-2). #2015: content chunks
            # follow DEFAULT_CONTENT_CHUNKS_ENABLED (off unless set), #2103 with
            # the enable recorded.
            **new_search_config_row(settings, content_chunks_enabled=chunks_on),
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

    async def list_contexts(
        self, user_id: str, *, raise_on_lookup_error: bool = False
    ) -> list[Context]:
        """List all contexts accessible to user (via workspace membership).

        Issue #115 Phase B: Lists contexts from user's current workspace.
        Issue #234: Respects allowed_context_ids whitelist for member/viewer.

        Args:
            user_id: User ID
            raise_on_lookup_error: A failed access lookup (not a member of the
                current workspace, a database error) is an empty list by
                default; True raises ``ContextListingFailedError`` instead, so
                MCP ``list_contexts`` does not read it as an empty account
                (#1658).

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
        except FeatureNotAvailableError:
            # #1943: a member without a seat is refused, not shown an empty
            # workspace — the same 403 FEAT-001 every other route answers.
            raise
        except Exception as e:
            if raise_on_lookup_error:
                raise ContextListingFailedError(str(e)) from e
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
            raise NotFoundException("Context", str(context_id))

        # Get workspace membership
        workspace_member = await self._get_workspace_member(user_id, context.workspace_id)
        if not workspace_member:
            raise NotFoundException("Context", str(context_id))
        # #1943: remember's authorizer runs through here — a member without a
        # seat is refused after membership (the owner always has one).
        await ensure_member_seated(self.db, context.workspace_id, workspace_member)

        # Issue #165: Privacy check - private contexts are creator-only
        if context.is_private and context.created_by != user_id:
            if not await is_same_owner(self.db, user_id, context.created_by):
                raise NotFoundException("Context", str(context_id))
            # #1784: an account linked to the creator is checked as itself. A
            # member with no whitelist is suspended (Migration 042), and the
            # link does not lift that — same rule as check_context_access.
            if (
                workspace_member.role == WorkspaceRole.MEMBER
                and workspace_member.allowed_context_ids is None
            ):
                raise NotFoundException("Context", str(context_id))

        # Issue #234: Check allowed_context_ids whitelist for member/viewer
        if workspace_member.role in (WorkspaceRole.MEMBER, WorkspaceRole.VIEWER):
            if workspace_member.allowed_context_ids is not None:
                if context_id not in workspace_member.allowed_context_ids:
                    raise NotFoundException("Context", str(context_id))

        return context

    async def export_context(
        self,
        user_id: str,
        context_id: UUID,
        *,
        key_workspace_id: UUID | None = None,
    ) -> "ContextExportResponse":
        """Build a portable JSON snapshot of a context the caller can read (#950).

        Authorization mirrors ``GET /memory/list`` exactly: the context is
        resolved through ``PermissionService.resolve_context_for_workspace_read``
        (uniform 404 hides cross-workspace existence — CWE-639), and memory
        visibility follows the same ``owner_filter`` rule — a private context
        exports only the caller's own memories, a shared context exports every
        member's. An export therefore never reveals more than the caller could
        already read via ``/memory/list``. Vectors, neural edges, and sessions
        are omitted (regenerated / re-learned on re-import).

        Raises:
            NotFoundException: context missing, or caller lacks read access.
            ExportTooLargeError: context exceeds ``EXPORT_MAX_MEMORIES`` (413).
        """
        from models.config import ContextSearchConfig
        from models.memory import Memory
        from models.schemas import (
            ContextExportResponse,
            ExportedContextMeta,
            ExportedMemory,
            ExportedSearchConfig,
        )
        from services.permission_service import PermissionService

        ctx = await PermissionService(self.db).resolve_context_for_workspace_read(
            user_id=user_id,
            context_id=context_id,
            key_workspace_id=key_workspace_id,
            capacity=CapacityPolicy.ALLOWED,  # export is the way out of the lock
        )

        # Read-parity with GET /memory/list: private context => creator-only.
        owner_filter = user_id if ctx.is_private else None
        mq = select(Memory).where(
            Memory.context_id == context_id,
            Memory.deleted_at.is_(None),
        )
        if owner_filter is not None:
            mq = mq.where(owned_by(Memory.user_id, owner_filter))
        # Fetch cap+1 so an oversized context is detected in a single query,
        # avoiding a separate COUNT round-trip on the common (small) path.
        mq = mq.order_by(Memory.created_at).limit(EXPORT_MAX_MEMORIES + 1)
        rows = list((await self.db.execute(mq)).scalars().all())
        if len(rows) > EXPORT_MAX_MEMORIES:
            # len(rows) is the cap+1 probe value, i.e. "at least EXPORT_MAX+1" —
            # not the exact total (we deliberately avoid a second COUNT(*) on the
            # rare over-cap path). The limit in the error message is what the
            # caller acts on; the exact count is immaterial to the 413.
            raise ExportTooLargeError(len(rows), EXPORT_MAX_MEMORIES)

        cfg = (
            await self.db.execute(
                select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
            )
        ).scalar_one_or_none()
        search_config = (
            ExportedSearchConfig(
                semantic_weight=float(cfg.semantic_weight),
                bm25_weight=float(cfg.bm25_weight),
                fetch_factor=cfg.fetch_factor,
                use_rerank=cfg.use_rerank,
                reranker_provider=cfg.reranker_provider,
                reranker_model=cfg.reranker_model,
                embedding_model=cfg.embedding_model,
                embedding_dimensions=cfg.embedding_dimensions,
                reinforce_enabled=cfg.reinforce_enabled,
                reinforce_max_boost=float(cfg.reinforce_max_boost),
                reinforce_require_host_arbitration=cfg.reinforce_require_host_arbitration,
                routing_mode=cfg.routing_mode,
                content_chunks_enabled=cfg.content_chunks_enabled,
                llm_selector_enabled=cfg.llm_selector_enabled,
            )
            if cfg is not None
            else None
        )

        memories = [
            ExportedMemory(
                id=m.id,
                summary=m.summary,
                context_summary=m.context_summary,
                content=m.content,
                details=m.details,
                type=m.type,
                importance=m.importance,
                confidence=m.confidence,
                tags=m.tags or [],
                context=m.context,
                scope=m.scope,
                delivery_mode=m.delivery_mode,
                created_at=m.created_at,
                updated_at=m.updated_at,
                source_uri=m.source_uri,
                source_type=m.source_type,
            )
            for m in rows
        ]

        return ContextExportResponse(
            exported_at=utcnow(),
            context=ExportedContextMeta(
                id=ctx.id,
                name=ctx.name,
                display_name=ctx.display_name,
                description=ctx.description,
                summary=ctx.summary,
                usage_guide=ctx.usage_guide,
                is_private=ctx.is_private,
                is_public=ctx.is_public,
                created_at=ctx.created_at,
                updated_at=ctx.updated_at,
            ),
            search_config=search_config,
            memory_count=len(memories),
            memories=memories,
        )

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
            # Issue #560: Tier quota check — only block "increase-only" transitions
            # (skip -> full/edges_only). Reductions (full/edges_only -> skip) are
            # always allowed so PRO workspaces grandfathered above the limit can
            # naturally taper down. The helper acquires SELECT FOR UPDATE on the
            # workspace row, closing the TOCTOU window between concurrent toggles.
            if context.sleep_mode == "skip" and sleep_mode != "skip":
                await self._assert_sleep_quota_or_raise(
                    workspace_id=context.workspace_id,
                    exclude_id=context.id,
                )
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

    async def _assert_sleep_quota_or_raise(
        self,
        workspace_id: UUID,
        exclude_id: UUID | None = None,
    ) -> None:
        """Assert that enabling sleep_mode for one more context fits the quota.

        Issue #560: Sleep Maintenance is a PRO-only, LLM-cost-bearing feature
        capped per workspace. The helper takes a row-level lock on the
        ``workspaces`` row so two concurrent ``update_context`` calls cannot
        both pass a ``count + 1 <= limit`` check before either commits — the
        lock is held until the calling transaction commits or rolls back.

        Reused as a service-layer helper so future admin bulk-override scripts
        can call it directly. ``create_context`` does NOT call this helper —
        ``Context.sleep_mode`` defaults to ``"skip"`` at the column level
        (Issue #558) and the request schema does not accept ``sleep_mode``,
        so a freshly-created context never contributes to the count.

        Args:
            workspace_id: Workspace owning the context being toggled.
            exclude_id: Context UUID to exclude from the count when its old
                ``sleep_mode`` is already non-skip (e.g. an admin script
                toggling between ``full`` and ``edges_only``). For the normal
                ``update_context`` path the caller has verified the old mode
                is ``skip`` before invoking, so passing the context's own id
                here is safe — it is already excluded by the
                ``sleep_mode != 'skip'`` filter.

        Raises:
            QuotaExceededError: Adding one more sleep-enabled context would
                exceed the workspace's effective limit (plan tier + addon).
        """
        workspace = (
            await self.db.execute(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
        ).scalar_one()

        limit = workspace.effective_sleep_enabled_contexts_limit

        # ``deleted_at.is_(None)`` matches the convention in
        # ``QuotaService.check_context_creation_allowed`` — soft-deleted
        # contexts must not inflate the active count, otherwise a workspace
        # that deletes its way back under the limit would still be blocked.
        stmt = select(func.count(Context.id)).where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
            Context.sleep_mode != "skip",
        )
        if exclude_id is not None:
            stmt = stmt.where(Context.id != exclude_id)
        current = (await self.db.execute(stmt)).scalar_one()

        if current + 1 > limit:
            addon_bonus = workspace.addon_sleep_contexts_bonus or 0
            # Two distinct rejection cases — surface them as distinct HTTP
            # status codes so clients can render the right action:
            #
            # - ``limit == 0`` (FREE/BASIC tier): true feature gate, the
            #   user's plan does not include this feature at all. Use
            #   ``FeatureNotAvailableError`` → 403. addon_bonus is
            #   irrelevant here because the zero-base-tier defense-in-depth
            #   rule clamps the effective limit to 0 regardless of addon.
            # - ``limit > 0`` (PRO at-or-above cap): true quota — the
            #   feature IS available, the user has just hit the cap.
            #   ``QuotaExceededError`` → 429.
            #
            # The Stripe SKU for ``extra_sleep_contexts`` is a Phase 2
            # follow-up (CHANGELOG ### Notes), so the over-cap message
            # currently directs the user to contact their workspace admin
            # rather than offering a self-serve purchase CTA. Update both
            # the message and the i18n keys when the SKU ships.
            if limit == 0:
                # #1644 S7: ``sleep_mode`` is enforced NUMERICALLY — it is in
                # no tier's ``features`` set and has no ``FEATURE_MIN_PLANS``
                # row — so the tier that lifts the refusal is derived from the
                # tier rows rather than from the feature registry. Registering
                # it as a real feature is registry surgery and out of scope.
                required_plan = lowest_tier_with_limit("sleep_enabled_contexts_limit", 0)
                raise FeatureNotAvailableError(
                    "Sleep Maintenance is a PRO-tier feature; "
                    "upgrade your plan to enable sleep_mode on contexts.",
                    feature="sleep_mode",
                    required_plan=required_plan,
                    required_plan_display=(
                        plan_display_name(required_plan) if required_plan else None
                    ),
                    current_plan=workspace.plan_name,
                )
            raise QuotaExceededError(
                (
                    f"Sleep-enabled contexts quota exceeded: "
                    f"{current + 1}/{limit} in use (plan limit "
                    f"{limit - addon_bonus} + addon bonus {addon_bonus}). "
                    f"Contact your workspace admin to request a higher cap."
                ),
                **quota_gate_details(
                    workspace.plan_name,
                    "sleep_enabled_contexts",
                    current=current,
                    limit=limit,
                    # #1644 S8: an addon-only cap. The over-cap path is reached
                    # only on a tier that already HAS the feature, and the
                    # headroom is bought as an addon, not as a tier — so there
                    # is no upgrade to advertise.
                    required_plan=None,
                    feature="sleep_mode",
                ),
                addon_bonus=addon_bonus,
                requested=current + 1,
            )

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

            result = cast(
                CursorResult[Any],
                await self.db.execute(
                    delete(ContextMember).where(
                        and_(
                            ContextMember.context_id == context.id,
                            ContextMember.user_id != owner_id,
                        )
                    )
                ),
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
        # MCP-only entry. merge_contexts is BLOCKED (a merge never lowers the
        # memory count): the handler's context resolvers check the capacity
        # lock (#1954). ALLOWED here because a BLOCKED owner check would also
        # run the #1943 writable-slot check, which a merge must not.
        await perm_service.check_context_owner(
            user_id, source_context_id, capacity=CapacityPolicy.ALLOWED
        )
        await perm_service.check_context_owner(
            user_id, target_context_id, capacity=CapacityPolicy.ALLOWED
        )
        # #1943: no writable-slot check here — a merge retires the source, so
        # it only ever brings the workspace closer to its context cap.

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

        # #1497: delete_context refuses a default context — but it is called AFTER
        # the rows and vectors have been copied, so the merge half-completed and
        # then raised. Check it up front, with the other pre-flights.
        if delete_source and source.is_default:
            raise ValidationError(
                "Cannot delete the default context. Merge with delete_source=false."
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

        # #1497: EVERY live memory moves, not only the embedded ones.
        #
        # This used to filter on ``embedding_status == "success"`` and then, with
        # delete_source=True, soft-delete the source anyway — so a pending or
        # failed memory was neither copied nor kept. It stayed parented to a
        # context the user had just removed, and the caller was told how many
        # were "merged" without being told what was left behind.
        #
        # The filter did protect something real: the Qdrant copy needs a vector,
        # and a non-success row has no point. But that is a reason to copy the
        # row WITHOUT a vector, not to discard it. #1496 established that
        # ``failed`` is a recoverable state — the sweep re-embeds those rows once
        # a credential exists — so discarding them throws away data that would
        # have come back on its own.
        result = await self.db.execute(
            select(Memory).where(
                Memory.context_id == source_context_id,
                Memory.deleted_at.is_(None),
            )
        )
        source_memories = result.scalars().all()

        # Build ID mapping and copy memories in PostgreSQL.
        #
        # #1497: only rows that HAVE a vector go into the mapping — that dict is
        # what drives the Qdrant copy, and asking it to fetch a point that was
        # never written just logs a skip.
        memory_id_mapping: dict[str, str] = {}
        rows_by_new_id: dict[str, Memory] = {}

        for mem in source_memories:
            new_id = uuid4()
            embedded = mem.embedding_status == "success"
            if embedded:
                memory_id_mapping[str(mem.id)] = str(new_id)

            # Normalize nested context JSON to reference target context
            ctx_json = mem.context
            if isinstance(ctx_json, dict) and "context_id" in ctx_json:
                ctx_json = {**ctx_json, "context_id": str(target_context_id)}

            # Copy all data fields; resource_id/resource_doc_id/resource_version
            # are Computed columns (auto-derived from details)
            # #1951: the copy lives in the target context, so it belongs to the
            # target's workspace — not to whatever the source row carried.
            new_mem = Memory(
                id=new_id,
                user_id=mem.user_id,
                workspace_id=target.workspace_id,
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
                # The usage-stat cluster resets as one group: this is a NEW
                # memory in a new context, with no history of its own. Splitting
                # it (carrying reference_count while zeroing access_count) would
                # break the access_count >= reference_count invariant documented
                # on the model.
                access_count=0,
                reference_count=0,
                client=mem.client,
                client_version=mem.client_version,
                source=mem.source,
                # #1497: provenance must survive the copy. source_type defaults
                # to "manual", and a row that arrived from a connector is
                # EXCLUDED from the pinned/bootstrap lane precisely because it is
                # not manual (repositories/memory.py, OWASP LLM01/LLM03). Letting
                # the default apply would silently promote connector content into
                # a lane that exists to keep it out — and merging into a trusted
                # context clears the other half of that gate at the same moment.
                source_type=mem.source_type,
                source_uri=mem.source_uri,
                delivery_mode=mem.delivery_mode,
                # #1497: a row with no vector is copied as `pending` so the #1496
                # sweep embeds it into its NEW home — process_pending_embedding
                # resolves the collection from context_id, so the copy lands in
                # the target's collection.
                #
                # `pending` rather than the source's own status, deliberately: a
                # failed row may have exhausted its retry budget, and copying
                # that verbatim would produce a memory born terminal that nothing
                # ever retries. A new row in a new context is a new episode,
                # which is what the budget already means elsewhere.
                embedding_status="success" if embedded else "pending",
                embedding_retry_count=0,
                embedding_error=None,
                created_at=mem.created_at,
                updated_at=utcnow(),
            )
            self.db.add(new_mem)
            rows_by_new_id[str(new_id)] = new_mem

        await self.db.flush()

        # #1798: the copies below exist before their rows are committed. Keep
        # the orphan sweep's delete pass out until this transaction ends.
        if memory_id_mapping:
            from db.point_writer_lock import hold_point_writer_lock

            await hold_point_writer_lock(self.db)

        # Copy Qdrant points — rollback PG on failure for consistency.
        # Guarded: with no embedded source rows there is nothing to fetch, and
        # skipping the call also keeps this path working on backends that do not
        # implement it.
        upserted: set[str] = set()
        if memory_id_mapping:
            collection = get_collection_name(src_model, src_dims)
            try:
                upserted = await copy_context_points(
                    workspace_id=str(source.workspace_id),
                    source_context_id=str(source_context_id),
                    target_context_id=str(target_context_id),
                    memory_id_mapping=memory_id_mapping,
                    collection_name=collection,
                )
            except Exception:
                await self.db.rollback()
                raise

        # #1497: a row we intended to mark `success` but whose vector did not
        # actually land must not claim to be embedded. Without this it would be
        # a brand-new memory that is invisible to search and that no automatic
        # path can repair, since the sweep only ever claims pending/processing/
        # failed — exactly the #1496 shape, manufactured by the merge itself.
        unvectored = 0
        for new_id in memory_id_mapping.values():
            if new_id not in upserted:
                rows_by_new_id[new_id].embedding_status = "pending"
                unvectored += 1

        # #2015: the copy carries summary points only. A target with content
        # chunk vectors gets its long merged memories re-queued, so the
        # embedding sweep rebuilds them with their chunk points — only while
        # its chunks are active (#2103 / #2080: store and plan included).
        from services.content_chunk_policy import content_chunks_effective
        from utils.content_chunks import split_content

        target_chunks_stored = (
            tgt_cfg is not None and getattr(tgt_cfg, "content_chunks_enabled", False) is True
        )
        if target_chunks_stored and await content_chunks_effective(
            self.db, target_context_id, stored=True
        ):
            requeued = 0
            for row in rows_by_new_id.values():
                if row.embedding_status == "success" and split_content(row.content):
                    row.embedding_status = "pending"
                    requeued += 1
            if requeued:
                logger.info(
                    "merge_content_chunks_requeued",
                    target_context_id=str(target_context_id),
                    memories=requeued,
                )
        elif target_chunks_stored and any(
            row.embedding_status == "success" and split_content(row.content)
            for row in rows_by_new_id.values()
        ):
            # #2107: an inactive target (plan, cap) gets no chunks now; the
            # gap is stamped, and the reconciler re-queues these memories
            # once the target's chunks are active again.
            from db.qdrant import chunk_points_supported
            from services.content_chunk_gaps import record_content_chunk_gap

            if chunk_points_supported():
                await record_content_chunk_gap(self.db, target_context_id, reason="merge")

        # #1497: never remove the source unless every LIVE row reached the target.
        #
        # Counted independently, straight from the table — NOT against
        # `source_memories`. Comparing the copy to the selection that produced it
        # is a tautology: narrow the SELECT and both sides shrink together, so
        # the guard passes while rows are left behind. That is exactly the
        # regression it exists to stop, and the first version of this check made
        # exactly that mistake (caught in review on #1499).
        if delete_source:
            live_result = await self.db.execute(
                select(func.count())
                .select_from(Memory)
                .where(
                    Memory.context_id == source_context_id,
                    Memory.deleted_at.is_(None),
                )
            )
            live_rows = live_result.scalar_one()
            if live_rows != len(rows_by_new_id):
                await self.db.rollback()
                raise ValidationError(
                    f"Merge transferred {len(rows_by_new_id)} of {live_rows} memories; "
                    "refusing to delete the source context."
                )

        # Optional: delete source (_commit=False for atomic transaction)
        if delete_source:
            # #1960: the source's files fold into the target with its memories
            # instead of being purged with the source. Dedup is per workspace,
            # so re-binding cannot collide; the quota does not move.
            from models.file_objects import FileObject

            await self.db.execute(
                update(FileObject)
                .where(
                    FileObject.context_id == source_context_id,
                    FileObject.deleted_at.is_(None),
                )
                .values(context_id=target_context_id)
                .execution_options(synchronize_session=False)
            )
            await self.delete_context(user_id, source_context_id, _commit=False)

        try:
            await self.db.commit()
        except Exception:
            # Nothing was purged, so nothing is released (#1960).
            self._storage_release_pending = {}
            raise

        # #1798: the source's own points go once the merge is durable — the
        # copies above carry the target's context_id and are not touched.
        # #1960: the files moved to the target, so nothing is normally pending
        # here; a reservation that slipped in between is released all the same.
        if delete_source:
            await self._remove_context_points(str(source.workspace_id), str(source_context_id))
            await self._release_pending_storage()

        # `merged` counts ROWS transferred, which is what the caller can see in
        # the context's memory count. It used to count Qdrant points, so a
        # source full of unembedded memories reported 0 while the UI had just
        # shown their real number.
        merged = len(rows_by_new_id)
        pending = merged - len(upserted)

        logger.info(
            "contexts_merged",
            source_context_id=str(source_context_id),
            target_context_id=str(target_context_id),
            merged=merged,
            pending_embedding=pending,
            unvectored=unvectored,
            delete_source=delete_source,
        )

        return {
            "merged": merged,
            # #1497: name what is not searchable YET rather than leaving the
            # caller to infer it. These rows transferred; only their index is
            # deferred, and the sweep builds it in the new context.
            "pending_embedding": pending,
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

        #1798: the context's points are removed from the vector store once the
        soft-delete is committed. They used to be kept, and nothing ever
        removed them — not even the tombstone purge. The rows stay until that
        purge; ``services/context_restore.py`` (#1804) brings the context back
        from them and the embedding sweep rebuilds the vectors.

        #1960: the context's files are soft-deleted with it (same timestamp)
        and their bytes leave the storage quota, so deleting a context frees
        storage — the capacity-over lock (#1941) lifts without deleting files
        one by one. The Redis counter is released after the commit
        (``_release_pending_storage``); a ``_commit=False`` caller does that
        itself. A restore within the file GC window brings the files back.

        Args:
            user_id: User ID (for access verification and audit trail)
            context_id: Context UUID
            _commit: False when the caller owns the transaction; that caller
                then removes the points itself after its own commit.

        Returns:
            The deleted Context object (with deleted_at set)

        Raises:
            NotFoundException: If context not found
            ValidationError: If trying to delete default context
        """
        # A previous delete on this service that was rolled back must not
        # release its bytes with this one.
        self._storage_release_pending = {}
        context = await self.get_context(user_id, context_id)

        # Cannot delete default context
        if context.is_default:
            raise ValidationError("Cannot delete default context")

        # Issue #85: Guard for non-HTTP callers (MCP tools call service directly)
        if context.is_locked:
            raise ConflictError("Context is locked. Unlock it before deleting.")

        # #1804: the context and its memories share one timestamp, which is
        # how a restore (services/context_restore.py) tells the memories this
        # deletion tombstoned from ones forgotten before it. #1960: the files
        # are paired the same way, so ``context.deleted_at`` must never be
        # rewritten while the context is deleted — the files would orphan.
        deleted_at = utcnow()
        memories_deleted = await self._tombstone_memories_orm(
            context, actor=user_id, deleted_at=deleted_at
        )
        await self._purge_context_files_and_edges(context, deleted_at=deleted_at)

        logger.info(
            "context_data_cleanup",
            workspace_id=str(context.workspace_id),
            context_name=context.name,
            memories_deleted=memories_deleted,
        )

        await self._detach_context(context, actor=user_id, deleted_at=deleted_at)
        if _commit:
            await self.db.commit()

        logger.info(
            "context_soft_deleted",
            user_id=user_id,
            workspace_id=str(context.workspace_id),
            context_id=str(context_id),
            context_name=context.name,
        )

        if _commit:
            await self._remove_context_points(str(context.workspace_id), str(context_id))
            await self._release_pending_storage()

        return context

    async def _tombstone_memories_orm(
        self, context: Context, *, actor: str, deleted_at: datetime
    ) -> int:
        """Soft-delete the context's live memories through the ORM (``delete_context``).

        Loads every row into the session; the capacity purge uses
        :meth:`_tombstone_memories_chunked` instead. Does not commit.
        """
        from models.memory import Memory

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
            memory.deleted_at = deleted_at
            memory.deleted_by = actor
        return len(memories_to_delete)

    async def _tombstone_memories_chunked(
        self,
        context: Context,
        *,
        actor: str,
        deleted_at: datetime,
        chunk: int | None = None,
        should_continue: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[int, bool]:
        """Soft-delete the context's live memories in bulk UPDATEs of ``chunk`` rows (#1969).

        Commits after each chunk, so no transaction spans a large context and no
        row is loaded as an ORM object (``synchronize_session=False``: objects
        already in the session are not updated — callers read counts, not
        instances). Loops until a chunk updates nothing. ``chunk`` defaults to
        :data:`PURGE_MEMORY_CHUNK`.

        ``should_continue`` is awaited after every committed chunk, so a
        re-subscribe during one huge context stops the purge within one chunk.

        Returns:
            ``(total tombstoned, aborted)`` — ``aborted`` when ``should_continue``
            said stop; the chunks committed before that stay tombstoned.
        """
        from models.memory import Memory

        return await self._tombstone_chunked(
            [
                Memory.workspace_id == context.workspace_id,
                Memory.context_id == context.id,
            ],
            actor=actor,
            deleted_at=deleted_at,
            chunk=chunk,
            should_continue=should_continue,
        )

    async def _tombstone_chunked(
        self,
        where: list[ColumnElement[bool]],
        *,
        actor: str,
        deleted_at: datetime,
        chunk: int | None = None,
        should_continue: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[int, bool]:
        """The chunked tombstone loop over the live memories matching ``where``.

        See :meth:`_tombstone_memories_chunked` for the contract.
        """
        from models.memory import Memory

        chunk = PURGE_MEMORY_CHUNK if chunk is None else chunk
        total = 0
        while True:
            if total and should_continue is not None and not await should_continue():
                return total, True
            ids = (
                select(Memory.id)
                .where(*where, Memory.deleted_at.is_(None))
                .limit(chunk)
                .scalar_subquery()
            )
            result = cast(
                "CursorResult[Any]",
                await self.db.execute(
                    update(Memory)
                    .where(Memory.id.in_(ids))
                    .values(deleted_at=deleted_at, deleted_by=actor)
                    .execution_options(synchronize_session=False)
                ),
            )
            done = result.rowcount or 0
            if done == 0:
                return total, False
            await self.db.commit()
            total += done

    async def _purge_context_files_and_edges(
        self, context: Context, *, deleted_at: datetime
    ) -> int:
        """Soft-delete the context's files and hard-delete its neural edges.

        #1960: the files go with the context, stamped with its timestamp so a
        restore within the GC window finds them
        (``FileStorageService.restore_context_files``). Their Redis bytes are
        ADDED to ``_storage_release_pending`` (released after the caller's
        commit). Does not commit. Returns the uploaded rows soft-deleted.
        """
        from models.memory import NeuralMemoryEdge
        from services.file_storage_service import FileStorageService

        purged = await FileStorageService(self.db).purge_live_context_files(
            [context.id], deleted_at=deleted_at
        )
        for ws_id, size in purged.released.items():
            self._storage_release_pending[ws_id] = (
                self._storage_release_pending.get(ws_id, 0) + size
            )

        # Hard-delete neural edges (no soft-delete needed, reconstructable)
        await self.db.execute(
            delete(NeuralMemoryEdge).where(
                and_(
                    NeuralMemoryEdge.workspace_id == context.workspace_id,
                    NeuralMemoryEdge.context_id == context.id,
                )
            )
        )
        return purged.visible

    async def _detach_context(self, context: Context, *, actor: str, deleted_at: datetime) -> None:
        """Drop the context from member whitelists, cancel its running analyses,
        and stamp the row deleted (``deleted_at`` / ``deleted_by``). Does not commit.
        """
        # Remove context from all members' allowed_context_ids
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

        # #1241/#1243: cancel any in-flight analysis run for this context.
        # Deleting a context is the strongest "stop everything" signal a
        # user can send — without this, the background pipeline kept
        # charging the workspace's BYOK key for minutes and then persisted
        # a full result set for a context that no longer exists (invisible
        # to every reader after #1243's liveness join). Flipping the row
        # makes the reporter's locked cancel guard skip the persist;
        # cancel_run_task stops the in-process compute (best-effort — see
        # tasks/analysis_tasks.py for the multi-worker caveat).
        from models.analysis import MemoryAnalysis

        running_analyses = list(
            (
                await self.db.execute(
                    select(MemoryAnalysis).where(
                        and_(
                            MemoryAnalysis.workspace_id == context.workspace_id,
                            MemoryAnalysis.context_id == context.id,
                            MemoryAnalysis.status == "running",
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        cancelled_runs = []
        for run in running_analyses:
            # Copilot review (#1250): re-check under the same row lock the
            # reporter's terminal writes take. The unlocked SELECT above is
            # check-then-act — persist_results could commit 'succeeded'
            # between it and this write, and a plain attribute flip on the
            # identity-mapped instance would clobber that committed state.
            # ``refresh(with_for_update=True)`` repopulates under the lock;
            # skip runs that reached a terminal state while we waited.
            try:
                await self.db.refresh(run, with_for_update=True)
            except ObjectDeletedError:
                # Hard-deleted while we waited (workspace CASCADE) —
                # nothing left to cancel.
                continue
            if run.status != "running":
                continue
            run.status = "cancelled"
            run.cancellation_reason = "context_deleted"
            run.finished_at = utcnow()
            cancelled_runs.append(run)
        if cancelled_runs:
            from tasks.analysis_tasks import cancel_run_task

            for run in cancelled_runs:
                cancel_run_task(run.id)
            logger.info(
                "context_delete_cancelled_running_analyses",
                context_id=str(context.id),
                cancelled_run_ids=[str(r.id) for r in cancelled_runs],
            )

        # Issue #84: Soft-delete context record (previously hard-deleted)
        context.deleted_at = deleted_at
        context.deleted_by = actor

    async def purge_workspace_data(
        self,
        workspace_id: UUID,
        *,
        actor: str,
        should_continue: Callable[[], Awaitable[bool]] | None = None,
    ) -> WorkspacePurgeReport:
        """Delete every memory, context and file of a workspace (#1969 capacity purge).

        A system operation: no membership, privacy, whitelist, ``is_locked`` or
        capacity check — the policy decided. For every live context the
        memories are tombstoned in chunks (:meth:`_tombstone_memories_chunked`,
        a commit per chunk), then its files and neural edges are purged as
        :meth:`delete_context` does, and every context except the default one
        is detached and soft-deleted, in one short transaction. After that
        commit its vectors are removed and its Redis bytes released. The
        default context stays, empty, so the workspace is usable on Free.
        Non-default contexts go first and the default one last, so an abort
        leaves the default context's memories intact. Then every memory of the
        workspace still live outside those contexts (no context, or under a
        context deleted earlier) is tombstoned the same way, and the
        workspace-scoped files (no context) are purged last.

        ``should_continue`` is awaited before each context, after every memory
        chunk and before the workspace-scoped files; when it returns False (the
        workspace re-subscribed or was erased meanwhile, or the owner started
        deleting data themselves) the purge stops and
        the partial report comes back with ``aborted=True``. A stop between
        chunks leaves that context live (not detached, files untouched) with
        the chunks already committed tombstoned.

        Soft deletes: the tombstone sweep hard-deletes memories after
        ``CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`` and the file GC after
        ``FILE_SOFT_DELETE_RETENTION``. Until then the admin restore
        (``services/context_restore.py``) can bring a deleted context back,
        with two exceptions:

        * The default context is never deleted, so a context restore cannot
          bring its memories back. Ops recovery within the tombstone
          retention: ``UPDATE memories SET deleted_at = NULL, deleted_by =
          NULL, embedding_status = 'pending' WHERE workspace_id = :ws AND
          context_id = :default_ctx AND deleted_by = 'system:capacity-purge'``
          (the embedding sweep then rebuilds the vectors).
        * A crash part-way through a context leaves the chunks already
          committed stamped with a ``deleted_at`` other than the one the
          context gets on the next run, so those memories are not restored
          with the context. Acceptable for a policy purge.

        Args:
            workspace_id: The workspace to empty.
            actor: Written as ``deleted_by`` (:data:`CAPACITY_PURGE_ACTOR`).
            should_continue: Optional abort check (see above).

        Returns:
            What was removed. Commits; never leaves an open write.
        """
        from services.file_storage_service import FileStorageService

        contexts = list(
            (
                await self.db.execute(
                    select(Context)
                    .where(Context.workspace_id == workspace_id, Context.deleted_at.is_(None))
                    .order_by(Context.created_at, Context.id)
                )
            )
            .scalars()
            .all()
        )
        contexts.sort(key=lambda c: c.is_default)  # stable: default last, rest by age
        deleted = emptied = memories = files = released_total = 0
        aborted = False
        for context in contexts:
            if should_continue is not None and not await should_continue():
                aborted = True
                break
            self._storage_release_pending = {}
            deleted_at = utcnow()
            tombstoned, aborted = await self._tombstone_memories_chunked(
                context, actor=actor, deleted_at=deleted_at, should_continue=should_continue
            )
            memories += tombstoned
            if aborted:
                break
            files += await self._purge_context_files_and_edges(context, deleted_at=deleted_at)
            if context.is_default:
                emptied += 1
            else:
                await self._detach_context(context, actor=actor, deleted_at=deleted_at)
                deleted += 1
            await self.db.commit()
            released_total += sum(self._storage_release_pending.values())
            await self._remove_context_points(str(workspace_id), str(context.id))
            await self._release_pending_storage()

        if not aborted and should_continue is not None and not await should_continue():
            aborted = True
        if not aborted:
            # The lock counts every live memory of the workspace, but the loop
            # above only reached those in live contexts. ``context_id`` is
            # nullable, and a memory can still be live under a context that was
            # deleted earlier: tombstone those too, or the workspace stays over
            # the cap after the purge. Their vectors are left to the orphan
            # vector sweep (services/orphan_vector_sweep.py) — there is no cheap
            # per-memory delete without knowing each point's collection, and
            # hits are hydrated from live rows, so none of them is readable.
            from models.memory import Memory

            leftover, aborted = await self._tombstone_chunked(
                [Memory.workspace_id == workspace_id],
                actor=actor,
                deleted_at=utcnow(),
                should_continue=should_continue,
            )
            memories += leftover
        if not aborted:
            self._storage_release_pending = {}
            ws_files, ws_bytes = await FileStorageService(self.db).purge_workspace_scoped_files(
                workspace_id, deleted_at=utcnow()
            )
            await self.db.commit()
            files += ws_files
            released_total += ws_bytes
            if ws_bytes:
                self._storage_release_pending = {workspace_id: ws_bytes}
                await self._release_pending_storage()

        report = WorkspacePurgeReport(
            contexts_deleted=deleted,
            contexts_emptied=emptied,
            memories_deleted=memories,
            files_deleted=files,
            bytes_released=released_total,
            aborted=aborted,
        )
        logger.info(
            "workspace_data_purged", workspace_id=str(workspace_id), actor=actor, **asdict(report)
        )
        return report

    async def _release_pending_storage(self) -> None:
        """Release the Redis storage counter for the files a delete purged (#1960).

        Runs after the commit that made the soft-deletes durable, so Redis
        follows the database (the R5 contract ``delete_file`` keeps). A
        failure here leaves the counter high until the next DB sweep, never
        low. Clears the pending map so a second call is a no-op.
        """
        pending = self._storage_release_pending
        self._storage_release_pending = {}
        if not pending:
            return
        from services import storage_quota_service

        for workspace_id, size_bytes in pending.items():
            if size_bytes > 0:
                await storage_quota_service.release_storage_bytes(
                    workspace_id=workspace_id, size_bytes=size_bytes
                )

    async def _remove_context_points(self, workspace_id: str, context_id: str) -> None:
        """Remove a deleted context's points from the vector store (#1798).

        Runs after the Postgres commit and never raises: the delete has
        already happened for every reader (hits are hydrated from live rows),
        so a vector-store outage must not turn it into an error. What this
        leaves behind, the orphan sweep (services/orphan_vector_sweep.py)
        removes.
        """
        from db.qdrant import delete_context_points_everywhere

        try:
            deleted = await delete_context_points_everywhere(workspace_id, context_id)
        except Exception as e:
            logger.warning(
                "context_points_delete_failed",
                workspace_id=workspace_id,
                context_id=context_id,
                error=str(e),
            )
            return
        logger.info(
            "context_points_removed",
            workspace_id=workspace_id,
            context_id=context_id,
            points=sum(deleted.values()),
            collections=sorted(deleted),
        )

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
        raise NotFoundException("Context", "create a context in the web interface at /contexts")

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
        collections: list[str] | None = None,
    ) -> None:
        """Delete a context's points from every kagura_memories* collection.

        Single Collection Migration: Instead of deleting an entire collection,
        this deletes all points matching workspace_id + context_id. #1798: it
        covers the per-model collections too, not only the default one.

        Args:
            workspace_id: Workspace ID (as string)
            context_name: Context name (for logging only)
            context_id: Context ID (required for point deletion)
            collections: The collections to ask, when the caller has listed
                them once for many contexts
        """
        from db.qdrant import delete_context_points_everywhere

        try:
            if not context_id:
                logger.warning(
                    "context_id_missing_for_deletion",
                    workspace_id=workspace_id,
                    context_name=context_name,
                )
                return

            # Single collection migration: Delete points for this context
            await delete_context_points_everywhere(
                workspace_id, context_id, collections=collections
            )

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

    # Single source of truth — derived from TagSortMode so adding a new mode
    # requires only updating the Literal type.
    _LIST_TAGS_SORT_MODES = get_args(TagSortMode)

    async def aggregate_tags(
        self,
        user_id: str,
        context_id: UUID,
        *,
        limit: int = 50,
        min_count: int = 1,
        sort: TagSortMode = "count",
        prefix: str = "",
        q: str | None = None,
        with_tags: list[str] | None = None,
        key_workspace_id: UUID | None = None,
    ) -> dict:
        """Aggregate tags across non-deleted memories in a context (Issue #614).

        Mirrors the canonical aggregation pattern from
        ``tasks.sleep_tasks._refresh_hub_tag_cache``: the inner
        ``SELECT DISTINCT id, unnest(tags)::text`` collapses intra-memory
        duplicate tags so a memory with ``tags=["python","python"]`` counts
        as 1 for ``python``, not 2 (Copilot loop 2 lesson from #223). The
        ``deleted_at IS NULL`` filter excludes soft-deleted memories, and the
        ``workspace_id`` filter (in addition to ``context_id``) preserves the
        workspace boundary for shared contexts.

        Args:
            user_id: Caller's user ID for access check.
            context_id: Target context UUID.
            limit: Maximum tags to return (1-500). Validated here so future
                internal callers can't slip through unbounded values.
            min_count: Minimum memory count per tag (default 1, i.e. include
                one-off tags so tag-drift typos are visible by default — see
                Issue #614 DX review). Range 1-10000.
            sort: One of ``"count"`` / ``"recent"`` / ``"alpha"``.
            prefix: Case-insensitive prefix filter for autocomplete. ``%`` /
                ``_`` / ``#`` are escaped to literal characters via
                ``ILIKE ... ESCAPE '#'`` so the parameter cannot be used as a
                wildcard probe.
            q: Optional case-insensitive substring filter on ``summary``
                (#618). Facets the cloud to tags on matching memories;
                whitespace-only is treated as no filter.
            with_tags: Optional multi-tag AND drill-down (#830). When set,
                aggregate only over memories whose tags contain ALL of these
                values (``tags @> with_tags``), and exclude the ``with_tags``
                values from the returned tags. AND-combined with ``q``. Bound
                as a single ``varchar[]`` param (never interpolated). Capped at
                50 tags, each ≤ 200 chars.
            key_workspace_id: #963 pure API-key workspace scope, or None.
                The context is resolved like every other context read
                (``resolve_context_for_workspace_read``): the key scope and
                an agent binding (#1275) apply.

        Returns:
            ``{"context_name": str, "rows": list[dict]}`` where each row is
            ``{"tag": str, "count": int, "last_used_at": datetime | None}``.
            ``context_name`` is carried back so MCP callers can include it
            in the response envelope without re-resolving the context.
            ``last_used_at`` is the naive UTC
            ``GREATEST(created_at, updated_at)`` of the most recent memory
            carrying the tag.

        Raises:
            NotFoundException: Context not found or caller lacks access.
            ValidationError: Invalid ``sort`` mode.
        """
        from sqlalchemy import text

        if sort not in self._LIST_TAGS_SORT_MODES:
            raise ValidationError(
                f"Invalid sort mode '{sort}'. Must be one of: "
                f"{', '.join(self._LIST_TAGS_SORT_MODES)}."
            )
        if type(limit) is not int or not (1 <= limit <= 500):
            raise ValidationError(f"limit must be an integer in [1, 500]; got {limit!r}.")
        if type(min_count) is not int or not (1 <= min_count <= 10_000):
            raise ValidationError(f"min_count must be an integer in [1, 10000]; got {min_count!r}.")

        # Uniform 404 disclosure: not-found and access-denied are indistinguishable
        # (CWE-639) — the read resolver conflates both into NotFoundException.
        # The tag list is a cleanup / discovery read, allowed while locked.
        from services.permission_service import PermissionService

        context = await PermissionService(self.db).resolve_context_for_workspace_read(
            user_id,
            context_id,
            capacity=CapacityPolicy.ALLOWED,
            key_workspace_id=key_workspace_id,
            # The audit vocabulary's lane for reads that list a context.
            operation="list",
        )

        if prefix:
            escaped = prefix.replace("#", "##").replace("%", "#%").replace("_", "#_")
            prefix_pattern = f"{escaped}%"
        else:
            prefix_pattern = ""

        # #618: optional ``q`` facets the cloud to tags on memories whose summary
        # matches (same substring semantics as GET /memory/list?q). Escape
        # #/%/_ to literals so it can't be used as a wildcard probe.
        q_clean = (q or "").strip()
        if q_clean:
            eq = q_clean.replace("#", "##").replace("%", "#%").replace("_", "#_")
            q_pattern = f"%{eq}%"
        else:
            q_pattern = ""

        # #830: optional ``with_tags`` facets the cloud to tags that co-occur
        # with ALL of the given tags (multi-tag AND drill-down). The matched
        # memory set is narrowed via ``tags @> with_tags`` (GIN-indexed), and
        # the with_tags values themselves are excluded from the returned cloud
        # ("what else can I add"). Bound as a single ``varchar[]`` param — never
        # interpolated — so each tag value is safe. An empty list is a no-op:
        # ``tags @> '{}'`` is always true and ``tag <> ALL('{}')`` excludes
        # nothing, so the result matches the pre-#830 (#618) behaviour exactly.
        # Bind the STRIPPED value (not the raw one): a request like
        # ``?with_tags=%20python%20`` must match memories tagged ``python`` and
        # self-exclude ``python`` from the cloud, not bind a literal `" python "`
        # that matches nothing (Copilot review on PR #833).
        with_tags_clean = [s for t in (with_tags or []) if (s := t.strip())]
        if len(with_tags_clean) > 50:
            raise ValidationError("with_tags accepts at most 50 tags.")
        if any(len(t) > 200 for t in with_tags_clean):
            raise ValidationError("each with_tags value must be at most 200 characters.")

        # sort is allow-list validated above; safe to interpolate.
        sort_clause = {
            "count": "ORDER BY cnt DESC, tag ASC",
            "recent": "ORDER BY last_used_at DESC NULLS LAST, tag ASC",
            "alpha": "ORDER BY lower(tag) ASC, tag ASC",
        }[sort]

        sql = text(
            f"""
            WITH scope AS (
                SELECT id,
                       tags,
                       GREATEST(created_at, updated_at) AS last_at
                FROM memories
                WHERE workspace_id = CAST(:workspace_id AS uuid)
                  AND context_id = CAST(:context_id AS uuid)
                  AND deleted_at IS NULL
                  AND tags IS NOT NULL
                  AND cardinality(tags) > 0
                  AND (:q_pattern = '' OR summary ILIKE :q_pattern ESCAPE '#')
                  AND tags @> CAST(:with_tags AS varchar[])
            ),
            tag_rows AS (
                -- DISTINCT id, tag collapses intra-array duplicates so a
                -- memory with ['python','python'] counts once. last_at is
                -- functionally determined by id, so including it does not
                -- change distinctness.
                SELECT DISTINCT id, unnest(tags)::text AS tag, last_at
                FROM scope
            ),
            tag_counts AS (
                SELECT
                    tag,
                    COUNT(*) AS cnt,
                    MAX(last_at) AS last_used_at
                FROM tag_rows
                WHERE (:prefix_pattern = '' OR tag ILIKE :prefix_pattern ESCAPE '#')
                  AND tag <> ALL(CAST(:with_tags AS varchar[]))
                GROUP BY tag
                HAVING COUNT(*) >= :min_count
            )
            SELECT tag, cnt, last_used_at
            FROM tag_counts
            {sort_clause}
            LIMIT :limit
            """
        )
        result = await self.db.execute(
            sql,
            {
                "workspace_id": str(context.workspace_id),
                "context_id": str(context_id),
                "prefix_pattern": prefix_pattern,
                "q_pattern": q_pattern,
                "with_tags": with_tags_clean,
                "min_count": min_count,
                "limit": limit,
            },
        )
        rows = [
            {
                "tag": row.tag,
                "count": int(row.cnt),
                "last_used_at": row.last_used_at,
            }
            for row in result
        ]
        return {"context_name": context.name, "rows": rows}

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
            raise NotFoundException("User", user_id)

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
            role=WorkspaceRole.OWNER,
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
