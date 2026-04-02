"""Qdrant vector database client.

Issue #1 specification:
- 1ユーザー1コレクション設計 (kagura_user_{user_id})
- Multilingual tokenizer (Qdrant 1.15+ built-in, 日本語自動対応)
- Full-text index (summary + context_summary)
- Keyword index (scope, type, context.context_id)

Based on: kagura-ai/src/kagura/core/memory/backends/qdrant_rag.py
"""

from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    Modifier,
    PointIdsList,
    PointStruct,
    Range,
    SparseVector,
    SparseVectorParams,
    TextIndexParams,
    TokenizerType,
    VectorParams,
)

from config.database import QDRANT_URL
from utils.exceptions import QdrantError
from utils.logger import get_logger
from utils.sparse_vector import build_query_sparse_vector
from utils.synonyms import expand_query_tokens
from utils.tokenizer import augment_reading_tokens, tokenize_and_reading

logger = get_logger(__name__)

# Singleton Qdrant client
_qdrant_client: AsyncQdrantClient | None = None

# Single Collection Migration: Collection name constant
KAGURA_MEMORIES_COLLECTION = "kagura_memories"


def get_collection_name(model: str, dimensions: int) -> str:
    """Get Qdrant collection name for a given embedding model.

    Default model (text-embedding-3-small, 512) maps to the legacy collection name
    for backward compatibility. Other models get a unique collection.

    Args:
        model: Embedding model name
        dimensions: Vector dimensions

    Returns:
        Collection name string
    """
    if model == "text-embedding-3-small" and dimensions == 512:
        return KAGURA_MEMORIES_COLLECTION
    slug = model.replace("-", "_").replace(":", "_").replace(".", "_").lower()
    return f"kagura_memories_{slug}_{dimensions}"


def _validate_uuid_format(value: str, field_name: str) -> None:
    """Validate UUID format to prevent injection attacks.

    Issue #273 H-4: Prevent filter injection via malformed UUIDs.

    Args:
        value: UUID string to validate
        field_name: Field name for error message

    Raises:
        ValueError: If UUID format is invalid

    Example:
        >>> _validate_uuid_format("123e4567-e89b-12d3-a456-426614174000", "workspace_id")  # OK
        >>> _validate_uuid_format("", "workspace_id")  # ValueError
        >>> _validate_uuid_format("invalid-uuid", "workspace_id")  # ValueError
    """
    from config.constants import ERROR_MSG_INVALID_UUID

    if not value or not isinstance(value, str):
        raise ValueError(ERROR_MSG_INVALID_UUID.format(field=field_name, value=value or "(empty)"))

    # Try to parse as UUID to validate format
    try:
        UUID(value)
    except (ValueError, AttributeError) as e:
        raise ValueError(ERROR_MSG_INVALID_UUID.format(field=field_name, value=value)) from e


def _build_tag_filter_conditions(filters: dict[str, Any]) -> list[FieldCondition]:
    """Build FieldConditions for tag filtering.

    Issue #67: Exact-match tag filtering only. Tags are NOT added to
    BM25 text search to avoid score inflation for tag-heavy memories.
    Issue #79: Support AND logic via tags_match="all".

    Args:
        filters: Filter dict with optional tags list and tags_match mode

    Returns:
        List of FieldConditions for tag matching (empty if no tags)
    """
    filter_tags = filters.get("tags")
    if not isinstance(filter_tags, list) or not filter_tags:
        return []

    # Validate: only non-empty strings, bounded to 50 tags
    valid_tags = [t for t in filter_tags if isinstance(t, str) and t][:50]
    if not valid_tags:
        return []

    tags_match = filters.get("tags_match", "any")
    if tags_match not in ("any", "all"):
        raise ValueError(f"Invalid tags_match value: {tags_match!r}. Must be 'any' or 'all'.")

    if tags_match == "all":
        # AND logic: memory must have ALL specified tags
        return [FieldCondition(key="tags", match=MatchValue(value=tag)) for tag in valid_tags]
    else:
        # OR logic (default): memory must have ANY of the specified tags
        return [FieldCondition(key="tags", match=MatchAny(any=valid_tags))]


def _build_search_filter(
    workspace_id: str,
    context_id: str,
    user_id: str,
    is_shared_context: bool = False,
    filters: dict[str, Any] | None = None,
) -> Filter | None:
    """Build combined isolation + metadata filter for search queries.

    Shared by semantic search and BM25 search to avoid duplication.

    Args:
        workspace_id: Workspace ID (isolation)
        context_id: Context ID (isolation)
        user_id: User ID (isolation, skipped for shared contexts)
        is_shared_context: If True, skip user_id filter
        filters: Optional metadata filters (scope, type, importance, tags)

    Returns:
        Qdrant Filter or None
    """
    conditions: list[Condition] = [
        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
        FieldCondition(key="context_id", match=MatchValue(value=context_id)),
    ]

    if not is_shared_context:
        conditions.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))

    if filters:
        if "scope" in filters:
            conditions.append(FieldCondition(key="scope", match=MatchValue(value=filters["scope"])))
        if "type" in filters:
            conditions.append(FieldCondition(key="type", match=MatchValue(value=filters["type"])))
        importance_condition = _build_importance_range_condition(filters)
        if importance_condition:
            conditions.append(importance_condition)
        tag_conditions = _build_tag_filter_conditions(filters)
        conditions.extend(tag_conditions)

    return Filter(must=conditions) if conditions else None


def _build_importance_range_condition(filters: dict[str, Any]) -> FieldCondition | None:
    """Build FieldCondition for importance range filtering.

    Issue #139: Helper function to avoid code duplication.

    Args:
        filters: Filter dict with optional importance range

    Returns:
        FieldCondition for importance range or None

    Raises:
        ValueError: If range values are invalid (gte > lte)

    Example:
        >>> filters = {"importance": {"gte": 0.9, "lte": 0.95}}
        >>> condition = _build_importance_range_condition(filters)
    """
    if "importance" not in filters or not isinstance(filters["importance"], dict):
        return None

    importance_filter = filters["importance"]
    range_kwargs = {}

    for op in ["gte", "lte", "gt", "lt"]:
        if op in importance_filter:
            range_kwargs[op] = importance_filter[op]

    if not range_kwargs:
        return None

    # Validate range consistency
    if "gte" in range_kwargs and "lte" in range_kwargs:
        if range_kwargs["gte"] > range_kwargs["lte"]:
            raise ValueError(
                f"gte ({range_kwargs['gte']}) cannot be greater than lte ({range_kwargs['lte']})"
            )

    if "gt" in range_kwargs and "lt" in range_kwargs:
        if range_kwargs["gt"] >= range_kwargs["lt"]:
            raise ValueError(
                f"gt ({range_kwargs['gt']}) must be less than lt ({range_kwargs['lt']})"
            )

    return FieldCondition(key="importance", range=Range(**range_kwargs))


def get_qdrant_client() -> AsyncQdrantClient:
    """Get singleton Qdrant client.

    Issue #273 Security: Supports API key authentication for production.

    Returns:
        AsyncQdrantClient instance

    Raises:
        QdrantError: If connection fails
    """
    global _qdrant_client

    if _qdrant_client is None:
        try:
            from config.settings import get_settings

            settings = get_settings()

            # Issue #273 Security Review: Add API key auth for production
            if settings.qdrant_api_key:
                _qdrant_client = AsyncQdrantClient(url=QDRANT_URL, api_key=settings.qdrant_api_key)
                logger.info("qdrant_client_initialized", url=QDRANT_URL, authenticated=True)
            else:
                _qdrant_client = AsyncQdrantClient(url=QDRANT_URL)
                logger.info("qdrant_client_initialized", url=QDRANT_URL, authenticated=False)
        except Exception as e:
            raise QdrantError(f"Failed to connect to Qdrant: {e}") from e

    return _qdrant_client


# ============================================================================
# Single Collection Design (Issue #273)
# ============================================================================
# All memories stored in single collection: kagura_memories
# Isolation via payload filtering: workspace_id + context_id + user_id
# See migrations/062_*.sql and 063_*.sql for 3-level isolation implementation
# ============================================================================


async def add_memory_to_qdrant(
    user_id: str,
    memory_id: UUID,
    vector: list[float],
    payload: dict[str, Any],
    workspace_id: str,
    context_id: str,
    sparse_indices: list[int] | None = None,
    sparse_values: list[float] | None = None,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Add memory point to Qdrant with dense + sparse vectors and 3-level isolation.

    Issue #16: Supports named vectors (dense for semantic, bm25 for sparse BM25).

    Args:
        user_id: User ID (required)
        memory_id: Memory UUID
        vector: Dense embedding vector
        payload: Metadata payload
        workspace_id: Workspace ID (required for 3-level isolation)
        context_id: Context ID (required for 3-level isolation)
        sparse_indices: Sparse vector token indices (MurmurHash3)
        sparse_values: Sparse vector TF values

    Raises:
        QdrantError: If operation fails
        ValueError: If workspace_id or context_id is missing
    """
    client = get_qdrant_client()

    # Validate required parameters
    if not workspace_id or not context_id or not user_id:
        raise ValueError(
            "workspace_id, context_id, and user_id are required. "
            f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
        )

    # Issue #273 H-4: Validate UUID format to prevent filter injection
    _validate_uuid_format(workspace_id, "workspace_id")
    _validate_uuid_format(context_id, "context_id")

    # Add isolation fields to payload
    payload["workspace_id"] = workspace_id
    payload["context_id"] = context_id
    payload["user_id"] = user_id

    # Validate sparse vector inputs
    if (sparse_indices is None) ^ (sparse_values is None):
        raise ValueError("sparse_indices and sparse_values must be provided together or both None")
    if sparse_indices is not None and sparse_values is not None:
        if len(sparse_indices) != len(sparse_values):
            raise ValueError("sparse_indices and sparse_values must have the same length")

    try:
        # Issue #16: Named vectors (dense + sparse BM25)
        point_vector: dict[str, Any] = {"dense": vector}
        if sparse_indices and sparse_values:
            point_vector["bm25"] = SparseVector(indices=sparse_indices, values=sparse_values)

        await client.upsert(
            collection_name=collection_name,
            points=[
                PointStruct(
                    id=str(memory_id),
                    vector=point_vector,
                    payload=payload,
                )
            ],
        )

        logger.debug(
            "memory_added_to_qdrant",
            collection=collection_name,
            memory_id=str(memory_id),
            workspace_id=workspace_id,
            context_id=context_id,
        )

    except Exception as e:
        raise QdrantError(f"Failed to add memory: {e}") from e


async def search_memories_qdrant(
    user_id: str,
    query_vector: list[float],
    workspace_id: str,
    context_id: str,
    limit: int = 10,
    filters: dict[str, Any] | None = None,
    is_shared_context: bool = False,  # NEW: Team collaboration support
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> list[dict]:
    """Semantic search in Qdrant with workspace-aware isolation.

    Single Collection Migration: Always uses "kagura_memories" collection.

    Args:
        user_id: User ID (required for isolation)
        query_vector: Query embedding vector
        workspace_id: Workspace ID (required for isolation)
        context_id: Context ID (required for isolation)
        limit: Max results
        filters: Optional filters (scope, type, importance range, etc.)
        is_shared_context: If True, skip user_id filter (workspace members can access)

    Returns:
        List of scored results

    Raises:
        QdrantError: If search fails
        ValueError: If workspace_id, context_id, or user_id is missing

    Note:
        Single Collection Migration: workspace_id, context_id, user_id are required
        for isolation filtering. user_id filter is skipped for shared contexts.
    """
    client = get_qdrant_client()

    # CRITICAL: Validate isolation parameters (Security)
    if not workspace_id or not context_id or not user_id:
        raise ValueError(
            "Isolation requires workspace_id, context_id, and user_id. "
            f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
        )

    # Issue #273 H-4: Validate UUID format to prevent filter injection
    _validate_uuid_format(workspace_id, "workspace_id")
    _validate_uuid_format(context_id, "context_id")

    try:
        qdrant_filter = _build_search_filter(
            workspace_id, context_id, user_id, is_shared_context, filters
        )

        # Issue #16: Named vector "dense" for semantic search
        results = await client.query_points(
            collection_name=collection_name,
            query=query_vector,
            using="dense",
            limit=limit,
            query_filter=qdrant_filter,
        )

        return [
            {
                "id": point.id,
                "score": point.score,
                "payload": point.payload,
                "embedding": query_vector,
            }
            for point in results.points
        ]

    except Exception as e:
        raise QdrantError(f"Search failed: {e}") from e


async def update_memory_payload_in_qdrant(
    memory_id: UUID,
    payload_updates: dict[str, Any],
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Update payload fields of a Qdrant point without re-embedding.

    Uses set_payload to update only specified fields (e.g. tags, importance, type).

    Args:
        memory_id: Memory UUID
        payload_updates: Dict of payload fields to update
        collection_name: Qdrant collection name

    Raises:
        QdrantError: If operation fails
    """
    client = get_qdrant_client()

    try:
        await client.set_payload(
            collection_name=collection_name,
            payload=payload_updates,
            points=[str(memory_id)],
        )

        logger.debug(
            "memory_payload_updated_in_qdrant",
            collection=collection_name,
            memory_id=str(memory_id),
            updated_fields=list(payload_updates.keys()),
        )

    except Exception as e:
        raise QdrantError(f"Failed to update memory payload: {e}") from e


async def delete_memory_from_qdrant(
    user_id: str,
    memory_id: UUID,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Delete memory point from Qdrant (single collection migration).

    Single Collection Migration: Always uses "kagura_memories" collection.
    Since memory_id is globally unique, direct deletion by ID is safe.

    Args:
        user_id: User ID (for logging only)
        memory_id: Memory UUID (globally unique identifier)

    Raises:
        QdrantError: If deletion fails

    Note:
        Single Collection Migration: Always uses "kagura_memories" collection.
        Memory ID is globally unique, so direct deletion by ID is safe.
    """
    client = get_qdrant_client()

    try:
        await client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=[str(memory_id)]),
        )

        logger.debug(
            "memory_deleted_from_qdrant",
            collection=collection_name,
            memory_id=str(memory_id),
            user_id=user_id,
        )

    except Exception as e:
        raise QdrantError(f"Failed to delete memory: {e}") from e


async def search_memories_fulltext(
    user_id: str,
    query: str,
    workspace_id: str,
    context_id: str,
    limit: int = 10,
    filters: dict[str, Any] | None = None,
    is_shared_context: bool = False,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> list[dict]:
    """BM25 keyword search using Qdrant native sparse vectors.

    Issue #16: Replaces MatchText + manual TF scoring with native BM25.
    Qdrant handles IDF, TF saturation, and document length normalization.

    Args:
        user_id: User ID (required for isolation)
        query: Search query (natural language)
        workspace_id: Workspace ID (required for isolation)
        context_id: Context ID (required for isolation)
        limit: Max results
        filters: Optional filters (scope, type, importance, tags)
        is_shared_context: If True, skip user_id filter

    Returns:
        List of scored results with native BM25 scores

    Raises:
        QdrantError: If search fails
        ValueError: If isolation parameters are missing
    """
    client = get_qdrant_client()

    # CRITICAL: Validate isolation parameters (Security)
    if not workspace_id or not context_id or not user_id:
        raise ValueError(
            "Isolation requires workspace_id, context_id, and user_id. "
            f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
        )

    _validate_uuid_format(workspace_id, "workspace_id")
    _validate_uuid_format(context_id, "context_id")

    try:
        qdrant_filter = _build_search_filter(
            workspace_id, context_id, user_id, is_shared_context, filters
        )

        # Build sparse query vector: single Sudachi pass for lemmas + readings
        tokenized_query, query_reading, sudachi_tokens = tokenize_and_reading(query)
        combined_query = f"{tokenized_query} {query_reading}" if query_reading else tokenized_query

        augmented = augment_reading_tokens(query, sudachi_tokens=sudachi_tokens)
        if augmented:
            combined_query = f"{combined_query} {augmented}"

        expanded_query = expand_query_tokens(combined_query)
        query_indices, query_values = build_query_sparse_vector(expanded_query)

        if not query_indices:
            logger.debug("bm25_query_empty_after_tokenization", query=query[:50])
            return []

        # Native BM25 search via sparse vector (Qdrant handles IDF + length norm)
        results = await client.query_points(
            collection_name=collection_name,
            query=SparseVector(indices=query_indices, values=query_values),
            using="bm25",
            limit=limit,
            query_filter=qdrant_filter,
        )

        return [
            {
                "id": point.id,
                "score": point.score,
                "payload": point.payload,
            }
            for point in results.points
        ]

    except Exception as e:
        raise QdrantError(f"BM25 search failed: {e}") from e


# ============================================================================
# Single Collection Migration (Issue: Qdrant Single Collection)
# ============================================================================


async def ensure_kagura_memories_collection(
    embedding_dim: int = 512,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Ensure single kagura_memories collection exists with 3-level isolation indexes.

    Creates a single unified collection for all workspaces, contexts, and users.
    Isolation is achieved through payload filtering on workspace_id, context_id, user_id.

    This replaces the per-context collection design (kagura_workspace_{workspace_id}_context_{name})
    with a single collection + payload filtering approach for better scalability.

    Indexes created:
        - workspace_id (keyword) - CRITICAL for workspace-level isolation
        - context_id (keyword) - CRITICAL for context-level isolation
        - user_id (keyword) - CRITICAL for user-level isolation
        - summary (text, multilingual)
        - context_summary (text, multilingual)
        - scope (keyword)
        - type (keyword)
        - importance (float)

    Args:
        embedding_dim: Embedding dimension (default: 512 for text-embedding-3-small)

    Raises:
        QdrantError: If collection creation fails

    Example:
        >>> await ensure_kagura_memories_collection(512)
        # Creates "kagura_memories" collection with all indexes
    """
    client = get_qdrant_client()

    try:
        # Check if collection exists
        collections = await client.get_collections()
        exists = any(c.name == collection_name for c in collections.collections)

        if exists:
            # Issue #16: Check if collection has sparse vector config
            info = await client.get_collection(collection_name)
            params = getattr(info.config, "params", None) if info.config else None
            sparse_cfg = getattr(params, "sparse_vectors", None) if params else None
            has_sparse = sparse_cfg is not None and "bm25" in sparse_cfg

            if has_sparse:
                # Collection is up-to-date, ensure keyword indexes
                existing_fields = set(info.payload_schema.keys()) if info.payload_schema else set()
                if "tags" not in existing_fields:
                    await client.create_payload_index(
                        collection_name=collection_name,
                        field_name="tags",
                        field_schema="keyword",  # type: ignore[arg-type]
                    )
                    logger.info("created_missing_index", field="tags", type="keyword")
                return

            # Old collection without sparse vectors — requires manual migration
            import os

            if os.getenv("KAGURA_RECREATE_COLLECTIONS", "").lower() not in ("true", "1"):
                raise QdrantError(
                    f"Collection '{collection_name}' needs sparse vector migration. "
                    "Set KAGURA_RECREATE_COLLECTIONS=true to auto-recreate (destroys data)."
                )

            logger.warning(
                "collection_recreating",
                collection=collection_name,
                reason="missing sparse vector config",
            )
            await client.delete_collection(collection_name)

        # Create collection with dense + sparse vector config (Issue #16)
        await client.create_collection(
            collection_name=collection_name,
            vectors_config={
                "dense": VectorParams(
                    size=embedding_dim,
                    distance=Distance.COSINE,
                ),
            },
            sparse_vectors_config={
                "bm25": SparseVectorParams(
                    modifier=Modifier.IDF,
                ),
            },
        )

        logger.info(
            "single_collection_created",
            collection=collection_name,
            dim=embedding_dim,
        )

        # Create isolation indexes (CRITICAL)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="workspace_id",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="context_id",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="user_id",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        # Create full-text indexes
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="summary",
            field_schema=TextIndexParams(  # type: ignore[arg-type]
                type="text",  # type: ignore[arg-type]
                tokenizer=TokenizerType.MULTILINGUAL,
                min_token_len=2,
                max_token_len=20,
                lowercase=True,
            ),
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="context_summary",
            field_schema=TextIndexParams(  # type: ignore[arg-type]
                type="text",  # type: ignore[arg-type]
                tokenizer=TokenizerType.MULTILINGUAL,
                min_token_len=2,
                max_token_len=20,
                lowercase=True,
            ),
        )

        # Create pre-tokenized text indexes (Issue #1: Japanese BM25)
        # These fields store Sudachi-lemmatized tokens for accurate Japanese search
        for field in ("summary_tokens", "context_summary_tokens", "content_tokens"):
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=TextIndexParams(  # type: ignore[arg-type]
                    type="text",  # type: ignore[arg-type]
                    tokenizer=TokenizerType.WORD,
                    min_token_len=1,
                    max_token_len=30,
                    lowercase=True,
                ),
            )

        # Create keyword indexes
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="scope",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="type",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        # Create float index for importance range filtering
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="importance",
            field_schema="float",  # type: ignore[arg-type]
        )

        # Issue #67: Keyword index for tag filtering (exact match)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="tags",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        logger.info(
            "single_collection_indexes_created",
            collection=collection_name,
            indexes=[
                "workspace_id",
                "context_id",
                "user_id",
                "summary",
                "context_summary",
                "scope",
                "type",
                "importance",
                "tags",
            ],
        )

    except Exception as e:
        logger.error(
            "single_collection_creation_failed",
            collection=collection_name,
            error=str(e),
        )
        raise QdrantError(f"Failed to create kagura_memories collection: {e}") from e


async def delete_context_points(
    workspace_id: str, context_id: str, collection_name: str = KAGURA_MEMORIES_COLLECTION
) -> int:
    """Delete all points for a specific context from kagura_memories collection.

    This is used when a context is deleted. Instead of deleting an entire collection
    (old design), we now delete all points matching workspace_id + context_id.

    Args:
        workspace_id: Workspace ID
        context_id: Context ID

    Returns:
        Number of points deleted (counted before deletion for verification)

    Raises:
        QdrantError: If deletion fails

    Example:
        >>> deleted = await delete_context_points("workspace-uuid", "context-uuid")
        >>> print(f"Deleted {deleted} points")
    """
    client = get_qdrant_client()

    try:
        # Build filter for workspace_id + context_id
        filter_conditions = Filter(
            must=[
                FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                FieldCondition(key="context_id", match=MatchValue(value=context_id)),
            ]
        )

        # Issue #273 Review: Count before delete for verification
        # Prevents silent failures - caller can verify deletion succeeded
        count_result = await client.count(
            collection_name=collection_name,
            count_filter=filter_conditions,
        )
        points_to_delete = count_result.count

        # Delete points matching the filter
        await client.delete(
            collection_name=collection_name,
            points_selector=filter_conditions,
        )

        logger.info(
            "context_points_deleted",
            collection=collection_name,
            workspace_id=workspace_id,
            context_id=context_id,
            count=points_to_delete,
        )

        return points_to_delete

    except Exception as e:
        logger.error(
            "context_points_deletion_failed",
            collection=collection_name,
            workspace_id=workspace_id,
            context_id=context_id,
            error=str(e),
        )
        raise QdrantError(f"Failed to delete context points: {e}") from e
