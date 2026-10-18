"""API routes for context search configuration management.

Issue #130: Context-scoped Search & Reranker Settings UI
Provides REST API endpoints for managing context-level hybrid search and reranker settings.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from config.constants import EMBEDDING_MODEL_REGISTRY
from config.settings import get_settings
from db.base import get_db
from db.qdrant import chunk_points_supported
from models.config import ContextSearchConfig
from models.schemas import ContextSearchConfigResponse, ContextSearchConfigUpdate
from repositories.config_repository import ContextSearchConfigRepository
from services.capacity_lock import CapacityPolicy
from services.content_chunk_policy import content_chunks_status
from services.permission_service import (
    PermissionService,
    check_content_chunks_change,
    gate_content_chunks_change,
)
from utils.exceptions import AuthorizationError, MemoryCloudException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/contexts", tags=["context-search-config"])


async def build_search_config_response(
    repo: ContextSearchConfigRepository,
    perm_service: PermissionService,
    user_id: str,
    context_id: UUID,
    config: ContextSearchConfig,
) -> ContextSearchConfigResponse:
    """The search-config response, with the server-computed #2015 fields (D7).

    Shared by GET, PUT and reset. ``content_chunks_can_change`` is the PUT's
    own rule, asked of the caller: the owner rule, and to turn the flag on the
    store, the plan and the cap (#2103; turning it off is always allowed).
    ``content_chunks_backfill_pending`` is counted only while the flag is on.
    #2103 / #2080 add whether the chunks are active, the plan's cap, the
    workspace's count and why the toggle is disabled. They inform the UI —
    the PUT checks all of it again.
    """
    try:
        await check_content_chunks_change(perm_service, user_id, context_id)
        owner = True
    except AuthorizationError:
        owner = False
    stored = config.content_chunks_enabled is True
    chunks = await content_chunks_status(repo.db, context_id, stored=stored, include_usage=True)
    can_change = owner and (stored or chunks.can_enable)
    reason = chunks.reason or (None if owner else "permission")
    model = config.embedding_model
    provider = (
        EMBEDDING_MODEL_REGISTRY[model][1]
        if model in EMBEDDING_MODEL_REGISTRY
        else get_settings().embedding_provider
    )
    pending = (
        await repo.content_chunk_backfill_pending(context_id)
        if config.content_chunks_enabled is True
        else 0
    )
    return ContextSearchConfigResponse.model_validate(config).model_copy(
        update={
            "content_chunks_can_change": can_change,
            "content_chunks_supported": chunk_points_supported(),
            "embedding_provider": provider,
            "content_chunks_backfill_pending": pending,
            "content_chunks_effective": chunks.effective,
            "content_chunks_limit": chunks.limit,
            "content_chunks_in_use": chunks.in_use or 0,
            "content_chunks_disabled_reason": reason,
        }
    )


@router.get("/{context_id}/search-config", response_model=ContextSearchConfigResponse)
async def get_context_search_config(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Get context search configuration.

    Retrieves hybrid search weights, fetch factor, and reranker settings for a context.

    Args:
        context_id: Context UUID
        current_user: Current authenticated user
        db: Database session

    Returns:
        ContextSearchConfigResponse with all settings

    Raises:
        HTTPException: 404 if context not found
    """
    user_id = user.get("user_id")
    logger.info("get_search_config_requested", user_id=user_id, context_id=str(context_id))

    try:
        # Check context write permission (owner/editor only)
        perm_service = PermissionService(db)
        await perm_service.check_context_write(user_id, context_id, capacity=CapacityPolicy.ALLOWED)

        repo = ContextSearchConfigRepository(db)
        config = await repo.create_or_get(context_id)

        logger.info(
            "get_search_config_success",
            user_id=user_id,
            context_id=str(context_id),
            semantic_weight=float(config.semantic_weight),
            fetch_factor=config.fetch_factor,
        )

        return await build_search_config_response(repo, perm_service, user_id, context_id, config)

    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error(
            "get_search_config_failed",
            user_id=user_id,
            context_id=str(context_id),
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve search configuration",
        ) from e


@router.put("/{context_id}/search-config", response_model=ContextSearchConfigResponse)
async def update_context_search_config(
    context_id: UUID,
    update_data: ContextSearchConfigUpdate,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Update context search configuration.

    Updates hybrid search weights, fetch factor, and/or reranker settings.
    Validates that weights sum to 1.0 and model matches provider.

    Context owners and editors may change every field except
    ``content_chunks_enabled``: changing that one (on or off) takes the context
    owner or a workspace owner/admin, since turning it on sends memory content
    to the embedding and rerank providers (#2015). Sending the stored value is
    not a change and is allowed.

    Args:
        context_id: Context UUID
        update_data: Configuration update request
        current_user: Current authenticated user
        db: Database session

    Returns:
        Updated ContextSearchConfigResponse

    Raises:
        HTTPException: 400 if validation fails, 403 if the caller may not
            change content_chunks_enabled, 404 if context not found, 500 on error
        ValidationError: 422 when turning content_chunks_enabled on with the
            LanceDB vector store, which has no content chunk vectors (#2072)
        FeatureNotAvailableError: 403 ``FEAT-001`` when turning it on on a plan
            without ``content_chunks`` (only where CONTENT_CHUNKS_REQUIRE_PLAN
            is on, #2103)
        QuotaExceededError: 429 ``QUOTA-001`` when turning it on with the
            workspace at the plan's ``max_content_chunk_contexts`` (likewise)
    """
    user_id = user.get("user_id")
    logger.info(
        "update_search_config_requested",
        user_id=user_id,
        context_id=str(context_id),
        updates=update_data.model_dump(exclude_unset=True),
    )

    try:
        # Check context write permission (owner/editor only)
        perm_service = PermissionService(db)
        await perm_service.check_context_write(user_id, context_id, capacity=CapacityPolicy.BLOCKED)
        # #1941/#1948: search settings are writes the MCP twin
        # (update_search_config) refuses on a capacity-locked workspace — the
        # write check above carries CapacityPolicy.BLOCKED.

        # Pydantic will automatically validate on model creation
        # Update config
        repo = ContextSearchConfigRepository(db)
        # #2015: changing content_chunks_enabled takes the context owner (or a
        # workspace owner/admin); the stored value is a no-op and not written.
        current = await repo.get_by_context(context_id)
        # Fail closed: a missing row is the default (off), so the gate always
        # runs before any write path is reached.
        current_enabled = current is not None and current.content_chunks_enabled is True
        update_data = await gate_content_chunks_change(
            perm_service,
            user_id,
            context_id,
            update_data,
            current_enabled=current_enabled,
        )
        config = await repo.update(context_id, update_data)

        logger.info(
            "update_search_config_success",
            user_id=user_id,
            context_id=str(context_id),
            semantic_weight=float(config.semantic_weight),
            bm25_weight=float(config.bm25_weight),
            fetch_factor=config.fetch_factor,
            use_rerank=config.use_rerank,
        )

        return await build_search_config_response(repo, perm_service, user_id, context_id, config)

    except (HTTPException, MemoryCloudException):
        raise
    except ValueError as e:
        logger.error(
            "update_search_config_not_found",
            user_id=user_id,
            context_id=str(context_id),
            error=str(e),
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except Exception as e:
        logger.error(
            "update_search_config_failed",
            user_id=user_id,
            context_id=str(context_id),
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update search configuration",
        ) from e


@router.post("/{context_id}/search-config/reset", response_model=ContextSearchConfigResponse)
async def reset_context_search_config(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Reset context search configuration to defaults.

    Resets all settings to default values:
    - semantic_weight: 0.6
    - bm25_weight: 0.4
    - fetch_factor: 3
    - use_rerank / reranker_provider / reranker_model: the deployment default
      (DEFAULT_USE_RERANK / DEFAULT_RERANKER_PROVIDER / DEFAULT_RERANKER_MODEL,
      #1572; unset: false / 'voyage' / 'rerank-2')
    - content_chunks_enabled: false — while it is on, that is a change and takes
      the context owner or a workspace owner/admin (#2015)

    Args:
        context_id: Context UUID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Reset ContextSearchConfigResponse

    Raises:
        HTTPException: 403 if content_chunks_enabled is on and the caller is
            not the context owner, 404 if context not found, 500 on error
    """
    user_id = user.get("user_id")
    logger.info("reset_search_config_requested", user_id=user_id, context_id=str(context_id))

    try:
        # Check context write permission (owner/editor only)
        perm_service = PermissionService(db)
        await perm_service.check_context_write(user_id, context_id, capacity=CapacityPolicy.BLOCKED)

        repo = ContextSearchConfigRepository(db)
        # #2015: a reset turns content_chunks_enabled off. While it is on that
        # is a change and takes the context owner (or a workspace owner/admin);
        # while it is off the reset does not write the flag at all.
        current = await repo.get_by_context(context_id)
        chunks_on = current is not None and current.content_chunks_enabled is True
        if chunks_on:
            await check_content_chunks_change(perm_service, user_id, context_id)
        config = await repo.reset_to_default(context_id, reset_content_chunks=chunks_on)

        logger.info("reset_search_config_success", user_id=user_id, context_id=str(context_id))

        return await build_search_config_response(repo, perm_service, user_id, context_id, config)

    except (HTTPException, MemoryCloudException):
        raise
    except ValueError as e:
        logger.error(
            "reset_search_config_not_found",
            user_id=user_id,
            context_id=str(context_id),
            error=str(e),
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except Exception as e:
        logger.error(
            "reset_search_config_failed",
            user_id=user_id,
            context_id=str(context_id),
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset search configuration",
        ) from e
