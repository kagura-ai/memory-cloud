"""Search service for Hybrid Search implementation.

Issue #1 specification:
- Semantic Search (OpenAI Embedding) 60%
- BM25/Full-text (Qdrant Multilingual) 40%
- Reranking (optional) - Issue #105: Multi-provider support (Voyage AI, Cohere)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import get_collection_name, search_memories_fulltext, search_memories_qdrant
from repositories.config_repository import ContextSearchConfigRepository
from services.embedding_service import EmbeddingService
from services.reranker_service import RerankerService
from utils.logger import get_logger

logger = get_logger(__name__)


class SearchService:
    """Service for Hybrid Search operations."""

    def __init__(self, db: AsyncSession):
        """Initialize search service.

        Args:
            db: Database session
        """
        self.db = db
        self.embedding_service = EmbeddingService(db)
        self.reranker_service = RerankerService(db)  # Issue #105: Cache instance

    async def hybrid_search(
        self,
        query: str,
        user_id: str,
        workspace_id: str,
        context_id: str,
        k: int = 10,
        use_rerank: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Hybrid Search: Semantic (60%) + BM25 (40%) with 3-level isolation.

        Single Collection Migration: Always uses "kagura_memories" collection.

        Args:
            query: Search query
            user_id: User ID (required)
            workspace_id: Workspace ID (required)
            context_id: Context ID (required)
            k: Number of results
            use_rerank: Use reranking if available
            filters: Optional filters

        Returns:
            List of search results with scores

        Example:
            >>> results = await search_service.hybrid_search(
            ...     "認証エラー", user_id="kiyota", k=5
            ... )
        """
        # Load context search configuration (Issue #130)
        config = await self._get_search_config(context_id)
        fetch_size = k * config.fetch_factor

        # Issue #XXX: Team collaboration - workspace membership + context privacy check
        from uuid import UUID

        from services.context_service import ContextService
        from services.permission_service import PermissionService
        from utils.exceptions import AuthorizationError

        # Check if context is shared
        context_service = ContextService(self.db)
        is_shared_context = await context_service.is_context_shared(UUID(context_id))

        # For shared contexts, verify workspace membership
        if is_shared_context:
            perm_service = PermissionService(self.db)
            is_member = await perm_service.is_workspace_member(user_id, UUID(workspace_id))
            if not is_member:
                raise AuthorizationError(f"Access denied: not a member of workspace {workspace_id}")

        logger.debug(
            "context_access_check",
            context_id=context_id,
            is_shared=is_shared_context,
            workspace_verified=is_shared_context,
        )

        # Issue #163: Normalize query for consistent search
        # NFKC: "ｶﾀｶﾅ" (half-width) → "カタカナ" (full-width)
        # NFC: か + ゛ → が (composed form)
        from utils.text import detect_symbol_density, normalize_for_search

        normalized_query = normalize_for_search(query) or query

        # Log symbol-heavy queries for potential weight tuning
        if detect_symbol_density(normalized_query):
            logger.debug(
                "high_symbol_density_detected",
                query=query[:50],
                normalized=normalized_query[:50],
            )

        logger.debug(
            "hybrid_search_config",
            context_id=context_id,
            semantic_weight=float(config.semantic_weight),
            bm25_weight=float(config.bm25_weight),
            fetch_factor=config.fetch_factor,
            fetch_size=fetch_size,
            query_normalized=query != normalized_query,
        )

        # Determine collection and embedding model for this context
        collection = get_collection_name(
            getattr(config, "embedding_model", "text-embedding-3-small"),
            getattr(config, "embedding_dimensions", 512),
        )
        embed_svc = EmbeddingService(
            self.db,
            model=getattr(config, "embedding_model", None),
            dimensions=getattr(config, "embedding_dimensions", None),
        )

        # 1. Semantic Search (Vector search)
        logger.debug("semantic_search_starting", query=normalized_query[:50], fetch_size=fetch_size)

        query_vector = await embed_svc.embed(
            normalized_query, user_id, context_id=context_id, workspace_id=workspace_id
        )
        semantic_results = await search_memories_qdrant(
            user_id=user_id,
            query_vector=query_vector,
            workspace_id=workspace_id,
            context_id=context_id,
            limit=fetch_size,
            filters=filters,
            is_shared_context=is_shared_context,
            collection_name=collection,
        )

        # 2. Full-text Search (MatchText via scroll)
        logger.debug("fulltext_search_starting", query=normalized_query[:50], fetch_size=fetch_size)

        fulltext_results = await search_memories_fulltext(
            user_id=user_id,
            query=normalized_query,
            workspace_id=workspace_id,
            context_id=context_id,
            limit=fetch_size,
            filters=filters,
            is_shared_context=is_shared_context,
            collection_name=collection,
        )

        # 3. Hybrid Merge (Dynamic weights from config - Issue #130)
        logger.debug("hybrid_merge_starting")

        merged_results = self._merge_results(
            semantic_results,
            fulltext_results,
            semantic_weight=float(config.semantic_weight),
            keyword_weight=float(config.bm25_weight),
        )

        # 4. Reranking (optional) - Issue #105: Multi-provider support (Voyage AI, Cohere)
        # Issue #130: Check both use_rerank parameter and config setting
        # Issue #149: Check plan tier feature access
        if use_rerank and config.use_rerank:
            # Check if workspace's plan tier allows reranking
            if workspace_id:
                from services.quota_service import QuotaService

                quota_service = QuotaService(self.db)
                can_rerank, error = await quota_service.check_feature_access(
                    workspace_id, "reranking", raise_on_denied=False
                )

                if not can_rerank:
                    logger.info(
                        "reranking_disabled_by_plan_tier",
                        workspace_id=str(workspace_id),
                        reason=error,
                    )
                    # Graceful degradation: Continue without reranking
                else:
                    # Plan tier allows reranking, proceed
                    try:
                        merged_results = await self.reranker_service.rerank(
                            query=query,
                            candidates=merged_results[
                                :fetch_size
                            ],  # Dynamic fetch size (Issue #130)
                            user_id=user_id,
                            k=k,
                            context_id=context_id,
                            workspace_id=workspace_id,  # NEW: Issue #146
                        )
                        logger.debug("reranking_completed", results=len(merged_results))
                    except Exception as e:
                        logger.warning("reranking_failed", error=str(e))
                        # Continue without reranking
            else:
                # No workspace_id (dev mode?), allow reranking
                try:
                    merged_results = await self.reranker_service.rerank(
                        query=query,
                        candidates=merged_results[:fetch_size],
                        user_id=user_id,
                        k=k,
                        context_id=context_id,
                        workspace_id=workspace_id,
                    )
                    logger.debug("reranking_completed", results=len(merged_results))
                except Exception as e:
                    logger.warning("reranking_failed", error=str(e))
        elif not config.use_rerank:
            logger.debug("reranking_disabled_by_config", context_id=context_id)

        # Return top k results
        return merged_results[:k]

    def _merge_results(
        self,
        semantic_results: list[dict],
        keyword_results: list[dict],
        semantic_weight: float = 0.6,
        keyword_weight: float = 0.4,
    ) -> list[dict]:
        """Merge semantic and keyword search results.

        Args:
            semantic_results: Semantic search results
            keyword_results: Keyword search results
            semantic_weight: Weight for semantic scores (default: 0.6)
            keyword_weight: Weight for keyword scores (default: 0.4)

        Returns:
            Merged and sorted results
        """
        # ====================================================================
        # BUG FIX #83-3: Zero division protection
        # ====================================================================
        # Problem: max(r["score"] for r in results) raises ValueError if results
        #          is empty list (no __bool__ check before max()).
        #
        # Solution: Add explicit empty check before calling max().
        #
        # Impact: Prevents crash when one search mode returns no results.
        # ====================================================================

        # Normalize scores to 0-1 range
        def normalize(results):
            # Early return for empty results
            if not results:
                return []

            max_score = max(r["score"] for r in results)
            if max_score == 0:
                return results

            return [{**r, "score": r["score"] / max_score} for r in results]

        semantic_norm = normalize(semantic_results)
        keyword_norm = normalize(keyword_results)

        # Merge by memory_id
        merged = {}

        for result in semantic_norm:
            memory_id = result["id"]
            merged[memory_id] = {
                **result,
                "semantic_score": result["score"],
                "keyword_score": 0.0,
                "hybrid_score": result["score"] * semantic_weight,
            }

        for result in keyword_norm:
            memory_id = result["id"]
            if memory_id in merged:
                merged[memory_id]["keyword_score"] = result["score"]
                merged[memory_id]["hybrid_score"] += result["score"] * keyword_weight
            else:
                merged[memory_id] = {
                    **result,
                    "semantic_score": 0.0,
                    "keyword_score": result["score"],
                    "hybrid_score": result["score"] * keyword_weight,
                }

        # Sort by hybrid_score
        sorted_results = sorted(merged.values(), key=lambda x: x["hybrid_score"], reverse=True)

        return sorted_results

    async def _get_search_config(self, context_id: str | None) -> Any:
        """Load search configuration from database.

        Issue #130: Context-scoped search configuration.

        Args:
            context_id: Context ID (string format)

        Returns:
            ProjectSearchConfig object (from DB or defaults)
        """
        if not context_id:
            # Return default config if no project
            logger.debug("search_config_using_defaults", reason="no_context_id")
            return type(
                "DefaultConfig",
                (),
                {
                    "semantic_weight": 0.6,
                    "bm25_weight": 0.4,
                    "fetch_factor": 3,
                    "use_rerank": False,
                },
            )()

        # Load config from database
        try:
            repo = ContextSearchConfigRepository(self.db)
            config = await repo.create_or_get(UUID(context_id))
            logger.debug(
                "search_config_loaded",
                context_id=context_id,
                semantic_weight=float(config.semantic_weight),
                bm25_weight=float(config.bm25_weight),
                fetch_factor=config.fetch_factor,
                use_rerank=config.use_rerank,
            )
            return config
        except Exception as e:
            logger.warning(
                "search_config_load_failed",
                context_id=context_id,
                error=str(e),
                fallback="defaults",
            )
            # Return defaults on error
            return type(
                "DefaultConfig",
                (),
                {
                    "semantic_weight": 0.6,
                    "bm25_weight": 0.4,
                    "fetch_factor": 3,
                    "use_rerank": False,
                },
            )()
