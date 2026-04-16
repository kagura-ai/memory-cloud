"""Per-context Qdrant routing — single source of truth.

Resolves (collection_name, EmbeddingService) from a context's
ContextSearchConfig row.  Both values originate from the same row so the
embedding dimensions and collection name are guaranteed to match.

All services MUST use these functions instead of querying
ContextSearchConfig independently, to prevent the split-brain bug pattern
documented in issues #324, #334, #338.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import get_collection_name
from models.config import ContextSearchConfig
from services.embedding_service import EmbeddingService
from utils.logger import get_logger

logger = get_logger(__name__)

_LEGACY_MODEL = "text-embedding-3-small"
_LEGACY_DIMS = 512


async def _fetch_config(db: AsyncSession, context_id: UUID) -> ContextSearchConfig | None:
    """Fetch ContextSearchConfig for a context (read-only, no auto-create)."""
    result = await db.execute(
        select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
    )
    return result.scalar_one_or_none()


async def resolve_context_routing(
    db: AsyncSession,
    context_id: UUID,
    *,
    default_service: EmbeddingService,
) -> tuple[str, EmbeddingService]:
    """Resolve Qdrant collection name and EmbeddingService for a context.

    Fetches ContextSearchConfig exactly once and derives both the collection
    name and a matching EmbeddingService from the same row.  When no config
    row exists, returns the legacy ``kagura_memories`` collection paired with
    *default_service* (the caller's pre-built default).

    The fallback collection is hardcoded to ``("text-embedding-3-small", 512)``
    — NOT derived from *default_service* — to keep all services on the same
    collection for legacy contexts even when an operator overrides
    ``settings.embedding_model``.  Cross-service consistency on the fallback
    path prevents silent split-brain (#338 loop 3).

    Args:
        db: Async SQLAlchemy session.
        context_id: Context UUID.
        default_service: Pre-constructed EmbeddingService returned on the
            fallback path.  Typically ``self.embedding_service`` from the
            calling service.

    Returns:
        ``(collection_name, embedding_service)`` sourced from the same config
        row.
    """
    config = await _fetch_config(db, context_id)

    if config:
        collection_name = get_collection_name(config.embedding_model, config.embedding_dimensions)
        embedding_service = EmbeddingService(
            db,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
        )
        return collection_name, embedding_service

    # No ContextSearchConfig row — legacy fallback.
    legacy_collection = get_collection_name(_LEGACY_MODEL, _LEGACY_DIMS)
    if default_service.model != _LEGACY_MODEL or default_service.dimensions != _LEGACY_DIMS:
        logger.warning(
            "context_routing_fallback_dim_mismatch",
            context_id=str(context_id),
            legacy_collection=legacy_collection,
            legacy_dim=_LEGACY_DIMS,
            service_model=default_service.model,
            service_dim=default_service.dimensions,
            hint="create a ContextSearchConfig row for this context to use the per-context routing path",
        )
    return legacy_collection, default_service


async def resolve_collection_name(
    db: AsyncSession,
    context_id: UUID,
) -> str:
    """Resolve Qdrant collection name for a context (no EmbeddingService).

    Convenience wrapper for callers that only need the collection name
    (e.g. delete, metadata-update paths).  Shares the same
    ``_fetch_config`` query as :func:`resolve_context_routing`.
    """
    config = await _fetch_config(db, context_id)
    if config:
        return get_collection_name(config.embedding_model, config.embedding_dimensions)
    return get_collection_name(_LEGACY_MODEL, _LEGACY_DIMS)
