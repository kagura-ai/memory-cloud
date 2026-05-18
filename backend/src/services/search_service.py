"""Search service for Hybrid Search implementation.

Issue #1 specification:
- Semantic Search (OpenAI Embedding) 60%
- BM25/Full-text (Qdrant Multilingual) 40%
- Reranking (optional) - Issue #105: Multi-provider support (Voyage AI, Cohere)
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import search_memories_fulltext, search_memories_qdrant
from models.llm_call_log import (
    LLM_CALL_LOG_CALL_TYPES,
    LLM_CALL_LOG_CALLERS,
)
from repositories.config_repository import ContextSearchConfigRepository
from services.context_routing import resolve_routing_from_config
from services.embedding_service import EmbeddingService
from services.llm_call_log_writer import LLMCallLogWriter
from services.reranker_service import RerankerService
from utils.logger import get_logger

logger = get_logger(__name__)

SearchMode = Literal["hybrid", "semantic", "keyword"]

# #475 PR-3: pinned literal values for the llm_call_log row this module emits.
# Module-level singletons with import-time tuple-membership assertions match
# the pattern in ``services/analysis/orchestrator.py:121-129`` — a future
# rename in the model's enum tuples turns into an ImportError here instead of
# a runtime ``ValueError`` on the first recall after deploy. Cheaper than
# indexing into the tuple (fragile to reorder) or repeating the literal at
# every future call site (lets the looser convention ossify).
_RECALL_CALLER = "recall"
_EMBEDDING_CALL_TYPE = "embedding"
assert _RECALL_CALLER in LLM_CALL_LOG_CALLERS
assert _EMBEDDING_CALL_TYPE in LLM_CALL_LOG_CALL_TYPES


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
        context_id: str | list[str],
        k: int = 10,
        use_rerank: bool = False,
        filters: dict[str, Any] | None = None,
        search_mode: SearchMode = "hybrid",
        include_vectors: bool = False,
        is_shared_context_read: bool = False,
    ) -> list[dict]:
        """Search with configurable mode: hybrid, semantic, or keyword.

        Issue #17: search_mode parameter allows choosing the search strategy.
        Issue #81: context_id can be a single string or list of strings for cross-context recall.

        Modes:
        - hybrid (default): Semantic + BM25 blend with configurable weights
        - semantic: Vector search only (embedding similarity)
        - keyword: BM25 only (Sudachi tokenized, good for hiragana queries)

        Args:
            query: Search query
            user_id: User ID (required)
            workspace_id: Workspace ID (required)
            context_id: Context ID or list of context IDs (Issue #81)
            k: Number of results
            use_rerank: Use reranking if available
            filters: Optional filters
            search_mode: Search strategy (hybrid/semantic/keyword)
            include_vectors: Return document embeddings from Qdrant (increases payload size)
            is_shared_context_read: Issue #708 Option A. Set by ``MemoryService.recall``
                when the caller's workspace differs from the context owner's
                workspace AND handler-layer access has already been verified
                (``_resolve_context_for_read``). When True, two things happen:
                (1) the redundant ``is_workspace_member(workspace_id)`` check
                is skipped — under Option A ``workspace_id`` is the SOURCE
                workspace, of which the caller is not necessarily a member,
                but their access via ``ContextMember`` / system_admin / etc.
                was already confirmed upstream; and (2) the Qdrant filter
                drops ``user_id == caller`` so memories authored by source
                workspace members are visible to the cross-workspace reader.
                Single-context and cross-context (``context_ids``) paths both
                honor this flag.

        Returns:
            List of search results with scores

        Raises:
            ValueError: If search_mode is not hybrid/semantic/keyword
        """
        if search_mode not in ("hybrid", "semantic", "keyword"):
            raise ValueError(f"Invalid search_mode: {search_mode}")

        # Issue #81: Normalize context_id to determine primary config context
        primary_context_id = context_id[0] if isinstance(context_id, list) else context_id

        # Load context search configuration (Issue #130)
        config = await self._get_search_config(primary_context_id)
        fetch_factor = config.fetch_factor
        # Issue #67: Double fetch size when reranker is active to compensate for
        # content-based BM25 length bias (reranker will re-score and trim)
        if use_rerank and getattr(config, "use_rerank", False):
            fetch_factor = fetch_factor * 2
        fetch_size = min(k * fetch_factor, 200)  # Cap to prevent excessive Qdrant/reranker load

        # Issue #XXX: Team collaboration - workspace membership + context privacy check
        from uuid import UUID

        from services.context_service import ContextService
        from services.permission_service import PermissionService
        from utils.exceptions import AuthorizationError

        # Issue #708 Option A: caller's cross-workspace access has been
        # verified at the handler layer; trust it and treat as shared so
        # source memories are visible. Otherwise fall back to the legacy
        # path that probes per-context ``is_context_shared`` (single-
        # context only — list paths fall through with is_shared_context
        # left False, matching pre-#708 behavior).
        is_shared_context = is_shared_context_read
        if not is_shared_context_read and not isinstance(context_id, list):
            context_service = ContextService(self.db)
            is_shared_context = await context_service.is_context_shared(UUID(context_id))

            # For shared contexts, verify workspace membership
            if is_shared_context:
                perm_service = PermissionService(self.db)
                is_member = await perm_service.is_workspace_member(user_id, UUID(workspace_id))
                if not is_member:
                    raise AuthorizationError(
                        f"Access denied: not a member of workspace {workspace_id}"
                    )

        logger.debug(
            "context_access_check",
            context_id=str(context_id),
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

        # Resolve per-context routing (#341: shared helper).
        # Reuse the config already loaded by _get_search_config to avoid a
        # second SELECT on the same ContextSearchConfig row.
        routing_config = config if hasattr(config, "context_id") else None
        collection, embed_svc = resolve_routing_from_config(
            self.db, routing_config, default_service=self.embedding_service
        )

        semantic_results: list[dict] = []
        fulltext_results: list[dict] = []

        if search_mode in ("hybrid", "semantic"):
            logger.debug(
                "semantic_search_starting", query=normalized_query[:50], fetch_size=fetch_size
            )
            # #475 PR-3: capture token usage so we can attribute embedding
            # cost via llm_call_log. ``embed_svc`` may be a context-specific
            # service returned by ``resolve_routing_from_config`` above
            # (different model than ``self.embedding_service``) — read
            # provider/model from it directly so multi-tenant pricing
            # routes correctly.
            query_vector, embedding_tokens = await embed_svc.embed_with_usage(
                normalized_query, user_id, context_id=primary_context_id, workspace_id=workspace_id
            )
            if embedding_tokens > 0:
                # Cache hits return 0 tokens and intentionally produce no
                # llm_call_log row (B1 pin) — the table is "API was
                # called" event log, not cache analytics. fail_on_error
                # is False so a writer flake never breaks recall.
                #
                # Issue #709: ``paid_by`` is resolved from the actual key
                # source rather than the legacy hardcoded ``"platform"``.
                writer = LLMCallLogWriter(self.db)
                await writer.record(
                    caller=_RECALL_CALLER,
                    call_type=_EMBEDDING_CALL_TYPE,
                    provider=embed_svc.provider,
                    model=embed_svc.model,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    context_id=primary_context_id,
                    embedding_tokens=embedding_tokens,
                    # #708 loop 4: thread context_id so paid_by reflects the
                    # actual key ``_get_user_api_key`` selected for THIS context.
                    # Without it, env-fallback calls on a context whose workspace
                    # has BYOK scoped to a DIFFERENT context would be falsely
                    # logged as "byok" — corrupts cost-grade attribution (#524).
                    paid_by=await embed_svc.resolve_paid_by(
                        workspace_id, context_id=primary_context_id
                    ),
                    fail_on_error=False,
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
                include_vectors=include_vectors,
            )

        if search_mode in ("hybrid", "keyword"):
            logger.debug(
                "fulltext_search_starting", query=normalized_query[:50], fetch_size=fetch_size
            )
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

        # Merge results based on mode
        if search_mode == "semantic":
            merged_results = semantic_results
        elif search_mode == "keyword":
            merged_results = fulltext_results
        else:
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
                            context_id=primary_context_id,
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
                        context_id=primary_context_id,
                        workspace_id=workspace_id,
                    )
                    logger.debug("reranking_completed", results=len(merged_results))
                except Exception as e:
                    logger.warning("reranking_failed", error=str(e))
        elif not config.use_rerank:
            logger.debug("reranking_disabled_by_config", context_id=primary_context_id)

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
                    "fetch_factor": 5,
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
                    "fetch_factor": 5,
                    "use_rerank": False,
                },
            )()
