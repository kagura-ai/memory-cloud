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
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchText,
    MatchValue,
    PointIdsList,
    PointStruct,
    Range,
    TextIndexParams,
    TokenizerType,
    VectorParams,
)

from config.database import QDRANT_URL
from utils.exceptions import QdrantError
from utils.logger import get_logger

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


def _build_tag_filter_condition(filters: dict[str, Any]) -> FieldCondition | None:
    """Build FieldCondition for tag filtering (match any).

    Issue #67: Helper function to avoid code duplication.

    Args:
        filters: Filter dict with optional tags list

    Returns:
        FieldCondition for tag matching or None
    """
    filter_tags = filters.get("tags")
    if isinstance(filter_tags, list) and filter_tags:
        return FieldCondition(key="tags", match=MatchAny(any=filter_tags))
    return None


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
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Add memory point to Qdrant with 3-level isolation.

    Single Collection Migration: Always uses "kagura_memories" collection.
    Adds workspace_id, context_id, user_id to payload for filtering.

    Args:
        user_id: User ID (required)
        memory_id: Memory UUID
        vector: Embedding vector
        payload: Metadata payload
        workspace_id: Workspace ID (required for 3-level isolation)
        context_id: Context ID (required for 3-level isolation)

    Raises:
        QdrantError: If operation fails
        ValueError: If workspace_id or context_id is missing

    Note:
        Single Collection Migration: workspace_id, context_id, user_id are required
        for 3-level isolation (workspace, context, user).
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

    try:
        await client.upsert(
            collection_name=collection_name,
            points=[
                PointStruct(
                    id=str(memory_id),
                    vector=vector,
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
        # Build Qdrant filter with workspace-aware isolation
        # Issue #XXX: Team collaboration - shared contexts allow workspace member access
        conditions = [
            FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
            FieldCondition(key="context_id", match=MatchValue(value=context_id)),
        ]

        # Add user_id filter only for private contexts
        if not is_shared_context:
            conditions.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))

        # Additional filters (scope, type, importance, etc.)
        if filters:
            if "scope" in filters:
                conditions.append(
                    FieldCondition(key="scope", match=MatchValue(value=filters["scope"]))
                )

            if "type" in filters:
                conditions.append(
                    FieldCondition(key="type", match=MatchValue(value=filters["type"]))
                )

            # Issue #139: importance range filter support
            importance_condition = _build_importance_range_condition(filters)
            if importance_condition:
                conditions.append(importance_condition)

            # Issue #67: Tag filtering
            tag_condition = _build_tag_filter_condition(filters)
            if tag_condition:
                conditions.append(tag_condition)

        # Build filter
        qdrant_filter = None
        if conditions:
            qdrant_filter = Filter(must=conditions)

        # Search (qdrant-client 1.16+ API)
        results = await client.query_points(
            collection_name=collection_name,
            query=query_vector,
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
    is_shared_context: bool = False,  # NEW: Team collaboration support
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> list[dict]:
    """Full-text search in Qdrant (BM25/keyword search) with workspace-aware isolation.

    Uses Qdrant's Multilingual tokenizer for Japanese/English support.
    Single Collection Migration: Always uses "kagura_memories" collection.

    Args:
        user_id: User ID (required for isolation)
        query: Search query (natural language)
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
        # Build Qdrant filter with workspace-aware isolation
        # Issue #XXX: Team collaboration - shared contexts allow workspace member access
        conditions = [
            FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
            FieldCondition(key="context_id", match=MatchValue(value=context_id)),
        ]

        # Add user_id filter only for private contexts
        if not is_shared_context:
            conditions.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))

        # Additional filters (scope, type, importance, etc.)
        if filters:
            if "scope" in filters:
                conditions.append(
                    FieldCondition(key="scope", match=MatchValue(value=filters["scope"]))
                )

            if "type" in filters:
                conditions.append(
                    FieldCondition(key="type", match=MatchValue(value=filters["type"]))
                )

            # Issue #139: importance range filter support
            importance_condition = _build_importance_range_condition(filters)
            if importance_condition:
                conditions.append(importance_condition)

            # Issue #67: Tag filtering
            tag_condition = _build_tag_filter_condition(filters)
            if tag_condition:
                conditions.append(tag_condition)

        # Build filter
        qdrant_filter = None
        if conditions:
            qdrant_filter = Filter(must=conditions)

        # Full-text search using MatchText filter + scroll
        # Issue #1: Search tokenized fields (Sudachi lemmas) first, plus original fields
        from utils.tokenizer import tokenize_for_search

        tokenized_query = tokenize_for_search(query)
        text_conditions = [
            # Tokenized fields (accurate Japanese matching via lemmas)
            FieldCondition(key="summary_tokens", match=MatchText(text=tokenized_query)),
            FieldCondition(key="context_summary_tokens", match=MatchText(text=tokenized_query)),
            # Original fields (fallback for old memories without tokens)
            FieldCondition(key="summary", match=MatchText(text=query)),
            FieldCondition(key="context_summary", match=MatchText(text=query)),
            # Issue #67: Tags in BM25 search (writing variations, categories)
            FieldCondition(key="tags_text", match=MatchText(text=query)),
            FieldCondition(key="tags_text", match=MatchText(text=tokenized_query)),
        ]

        # Combine text conditions (should = OR) with other filters (must = AND)
        if qdrant_filter and qdrant_filter.must:
            combined_filter = Filter(
                should=text_conditions,  # Match in summary OR context_summary
                must=qdrant_filter.must,  # AND with other filters
            )
        else:
            combined_filter = Filter(should=text_conditions)

        # Use scroll to get matching points
        scroll_result = await client.scroll(
            collection_name=collection_name,
            scroll_filter=combined_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )

        points, next_page = scroll_result

        # ============================================================================
        # BUG FIX #83-1: Simple BM25-like scoring for full-text search
        # ============================================================================
        # Problem: Qdrant's scroll() API with MatchText filter does not return
        #          relevance scores (BM25). All results were hardcoded to score=1.0,
        #          making Hybrid Search ineffective (all keyword results ranked equally).
        #
        # Solution: Implement simple term frequency scoring as approximation of BM25.
        #           This gives higher scores to documents matching more query terms.
        #
        # Formula: score = base_score + (term_hit_boost × number_of_matching_terms)
        #          - base_score: 0.5 (baseline for any match)
        #          - term_hit_boost: 0.1 per matching term
        #          - Clamped to [0, 1] range
        #
        # Example: Query "Python エラー 解決"
        #          - Doc with all 3 terms: score = 0.5 + 0.1×3 = 0.8
        #          - Doc with 1 term: score = 0.5 + 0.1×1 = 0.6
        #
        # Note: This is a temporary solution. For production-grade BM25:
        #       - Option A: Use Qdrant's search() API with sparse vectors
        #       - Option B: Use external BM25 library (rank-bm25)
        #       - Option C: Implement proper BM25 with IDF calculation
        # ============================================================================

        # ============================================================================
        # BUG FIX #122-1: Unicode-aware tokenization for Japanese text
        # ============================================================================
        # Problem: re.findall(r"\w+", query) uses ASCII-only word matching.
        #          Japanese characters (ひらがな、カタカナ、漢字) are ignored,
        #          resulting in empty query_words for Japanese queries.
        #
        # Before: "認証エラー解決" → query_words = {} (empty!)
        # After:  "認証エラー解決" → query_words = {"認証エラー解決"}
        #
        # Solution: Use `regex` library with Unicode property escapes (\p{L}, \p{N})
        #           which match any Unicode letter/number across all scripts.
        # ============================================================================
        # Issue #1: Use tokenized query terms for TF scoring
        query_words = set(tokenized_query.split())

        results = []
        for point in points:
            # Issue #1: Use tokenized fields for accurate term matching
            summary_tokens = point.payload.get("summary_tokens") or ""
            ctx_tokens = point.payload.get("context_summary_tokens") or ""
            # Fallback to original fields for old memories without tokens
            if not summary_tokens:
                summary_tokens = (point.payload.get("summary") or "").lower()
            if not ctx_tokens:
                ctx_tokens = (point.payload.get("context_summary") or "").lower()
            # Issue #67: Include tags in term matching (pre-lowercased at storage time)
            tags_text = point.payload.get("tags_text") or ""
            combined_text = summary_tokens + " " + ctx_tokens + " " + tags_text

            hit_count = sum(1 for word in query_words if word in combined_text)

            # Calculate score:
            # - Base: 0.5 (any match gets baseline score)
            # - Boost: +0.1 per matching term
            # - Clamp: max 1.0
            score = 0.5 + (0.1 * hit_count)
            score = min(1.0, score)

            results.append(
                {
                    "id": point.id,
                    "score": score,
                    "payload": point.payload,
                }
            )

        return results

    except Exception as e:
        raise QdrantError(f"Full-text search failed: {e}") from e


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
            # Ensure pre-tokenized indexes exist (Issue #1: Japanese BM25)
            info = await client.get_collection(collection_name)
            existing_fields = set(info.payload_schema.keys()) if info.payload_schema else set()
            # Backfill text indexes for pre-tokenized fields (Issue #1)
            for field in ("summary_tokens", "context_summary_tokens", "tags_text"):
                if field not in existing_fields:
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
                    logger.info("created_missing_index", field=field, type="text")

            # Backfill keyword index for tags (Issue #67)
            if "tags" not in existing_fields:
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name="tags",
                    field_schema="keyword",  # type: ignore[arg-type]
                )
                logger.info("created_missing_index", field="tags", type="keyword")

            return

        # Create collection with vector config
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=embedding_dim,
                distance=Distance.COSINE,
            ),
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
        for field in ("summary_tokens", "context_summary_tokens"):
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

        # Issue #67: Tags indexes for filtering and BM25 search
        # Keyword index: enables exact-match filtering (recall with tags filter)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="tags",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        # Text index on tags_text: enables BM25 fulltext search across tags
        # tags_text is a space-joined string of all tags (stored alongside tags array)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="tags_text",
            field_schema=TextIndexParams(  # type: ignore[arg-type]
                type="text",  # type: ignore[arg-type]
                tokenizer=TokenizerType.WORD,
                min_token_len=1,
                max_token_len=30,
                lowercase=True,
            ),
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
                "tags_text",
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
