"""Memory service for business logic.

Orchestrates memory operations across PostgreSQL, Qdrant, and Redis.
Implements remember(), recall(), forget(), reference() APIs.
Issue #82: Context-based multi-collection support.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.retention import should_promote_to_persistent
from db.qdrant import (
    add_memory_to_qdrant,
    delete_memory_from_qdrant,
    get_collection_name,
)
from models.auth import Context
from models.memory import Memory
from models.schemas import (
    ExploreRequest,
    ExploreResponse,
    ForgetRequest,
    ForgetResponse,
    MemoryResponse,
    MemoryStatsResponse,
    RecallRequest,
    RecallResponse,
    ReferenceResponse,
    RelatedMemoryResponse,
    RelatedTagItem,
    RememberRequest,
    RememberResponse,
)
from repositories.memory import MemoryRepository
from services.context_service import ContextService
from services.embedding_service import EmbeddingService
from services.search_service import SearchService
from utils.datetime import utcnow
from utils.exceptions import NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


class MemoryService:
    """Memory service for core memory operations.

    Issue #82: Supports context-based multi-collection.
    """

    def __init__(self, db: AsyncSession):
        """Initialize memory service.

        Args:
            db: Database session
        """
        self.db = db
        self.memory_repo = MemoryRepository(db)
        self.embedding_service = EmbeddingService(db)
        self.search_service = SearchService(db)
        self.context_service = ContextService(db)

    async def _get_context_search_config(self, context_id: UUID):
        """Get ContextSearchConfig for a context."""
        from models.config import ContextSearchConfig

        result = await self.db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
        )
        return result.scalar_one_or_none()

    async def _get_context_collection_name(self, context_id: UUID) -> str:
        """Get Qdrant collection name for a context from its search config."""
        config = await self._get_context_search_config(context_id)
        if config:
            return get_collection_name(config.embedding_model, config.embedding_dimensions)
        return get_collection_name("text-embedding-3-small", 512)

    def _get_embedding_service_for_config(self, config) -> EmbeddingService:
        """Create EmbeddingService configured for a specific context's model."""
        if config:
            return EmbeddingService(
                self.db, model=config.embedding_model, dimensions=config.embedding_dimensions
            )
        return self.embedding_service

    async def _get_context_isolation_params(
        self, user_id: str, context_id: UUID | None
    ) -> tuple[Context | None, str | None, str | None]:
        """Extract workspace_id and context_id for 3-level isolation (performance optimization).

        Single Collection Migration: Helper to avoid duplicate context fetches.

        Args:
            user_id: User ID
            context_id: Context ID

        Returns:
            Tuple of (context_object, workspace_id_str, context_id_str)

        Raises:
            NotFoundException: If context not found
        """
        if not context_id:
            return None, None, None

        context = await self.context_service.get_context(user_id, context_id)
        return context, str(context.workspace_id), str(context_id)

    async def remember(
        self,
        request: RememberRequest,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> RememberResponse:
        """Store new memory.

        Issue #1 specification:
        - Layer 1: summary (Embedding化)
        Issue #146: Workspace-scoped API keys support
        - Layer 2: context_summary
        - Layer 3: content + details
        - Qdrant + PostgreSQL dual storage

        Issue #82: Context-based multi-collection support.

        Args:
            request: Remember request
            user_id: User ID
            client: Client name
            current_context_id: Current context UUID (Issue #82)

        Returns:
            RememberResponse with memory_id and scope

        Example:
            >>> result = await service.remember(request, "kiyota", "claude-sonnet-4")
            >>> result.memory_id
            UUID('...')
            >>> result.scope
            'working'
        """
        # Issue #149: Check quota before creating memory
        # Single Collection Migration: Memory count only (storage size removed)
        if current_workspace_id:
            from services.quota_service import QuotaService

            quota_service = QuotaService(self.db)

            # Check memory quota (count-based only)
            can_create, error = await quota_service.check_memory_quota(
                current_workspace_id, raise_on_exceeded=True
            )

        # Single Collection Migration: Extract isolation params (optimized)
        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id, current_context_id
        )

        # Validate required parameters
        if not workspace_id_str or not context_id_str:
            raise ValueError("remember() requires current_context_id")

        # Issue #273: Validate content size to prevent DoS attacks
        # Without storage quota, we enforce per-memory size limit
        from config.constants import MAX_CONTENT_SIZE

        content_size = (
            len(request.summary or "")
            + len(request.context_summary or "")
            + len(request.content or "")
            + len(str(request.details or ""))
        )
        if content_size > MAX_CONTENT_SIZE:
            from utils.exceptions import QuotaExceededError

            raise QuotaExceededError(
                f"Memory size {content_size:,} bytes exceeds limit {MAX_CONTENT_SIZE:,} bytes (1MB). "
                f"Please reduce content size or split into multiple memories."
            )

        # Create memory ID first
        memory_id = uuid4()

        # ============================================================================
        # BUG FIX #122-3: Transaction integrity for PostgreSQL/Qdrant operations
        # ============================================================================
        # Problem: Previous implementation was:
        #   1. Generate embedding
        #   2. Save to Qdrant
        #   3. Save to PostgreSQL
        #   4. Commit
        # If step 2 failed silently, memory existed in PostgreSQL but not in Qdrant,
        # making it unreachable via recall() but accessible via reference().
        #
        # Solution: Use embedding_status to track state and ensure atomicity:
        #   1. Save to PostgreSQL with embedding_status='pending'
        #   2. Generate embedding
        #   3. Save to Qdrant
        #   4. Update embedding_status='success'
        #   5. Commit
        # On any failure, rollback and set embedding_status='failed' with error.
        # ============================================================================

        # Issue #163: Normalize searchable text fields for consistent search
        # NFKC: "ｶﾀｶﾅ" (half-width) → "カタカナ" (full-width)
        # NFC: か + ゛ → が (composed form)
        # Only summary and context_summary are normalized (used in fulltext search)
        # content and details are kept as-is since they store full original data
        from utils.text import normalize_for_search

        normalized_summary = normalize_for_search(request.summary)
        normalized_context_summary = normalize_for_search(request.context_summary)

        # Create memory entity first with pending status
        memory = Memory(
            id=memory_id,
            user_id=user_id,
            workspace_id=UUID(workspace_id_str)
            if workspace_id_str
            else None,  # Migration 063: 3-level isolation
            context_id=UUID(context_id_str)
            if context_id_str
            else None,  # Migration 063: 3-level isolation
            summary=normalized_summary,  # Issue #163: Normalized for search
            context_summary=normalized_context_summary,  # Issue #163: Normalized for search
            content=request.content,  # Keep original
            details=request.details,  # Keep original
            type=request.type,
            importance=request.importance,
            tags=request.tags,
            context=request.context,
            scope="working",
            client=client,
            summary_embedding_id=memory_id,  # Same as memory_id
            embedding_status="pending",  # Issue #122: Track embedding state
        )

        try:
            # Save to PostgreSQL first (with pending status)
            await self.memory_repo.create(memory)
            await self.db.flush()  # Ensure memory exists before Qdrant operation

            # Issue #67: Join tags once for both embedding and BM25 payload
            config = await self._get_context_search_config(context.id)
            embed_svc = self._get_embedding_service_for_config(config)
            # Pre-lowercase for consistent BM25 scoring (avoids .lower() in search loop)
            tags_str = " ".join(t.lower() for t in request.tags) if request.tags else ""
            embed_text = f"{request.summary} {tags_str}" if tags_str else request.summary
            vector = await embed_svc.embed(
                embed_text,
                user_id,
                context_id=current_context_id,
                workspace_id=current_workspace_id,
            )

            # Prepare Qdrant payload (use normalized values for consistent search)
            from utils.tokenizer import tokenize_for_search

            payload = {
                "user_id": user_id,
                "summary": normalized_summary,
                "context_summary": normalized_context_summary,
                "summary_tokens": tokenize_for_search(normalized_summary),
                "context_summary_tokens": tokenize_for_search(normalized_context_summary or ""),
                "type": request.type,
                "importance": request.importance,
                "tags": request.tags,
                "tags_text": tags_str,
                "scope": "working",
                "client": client,
                "created_at": utcnow().isoformat(),
            }

            if request.context:
                payload["context"] = request.context

            # Add to Qdrant with 3-level isolation (per-model collection)
            # Reuse config already fetched above (avoid double DB query)
            if config:
                collection = get_collection_name(
                    config.embedding_model, config.embedding_dimensions
                )
            else:
                collection = get_collection_name("text-embedding-3-small", 512)
            await add_memory_to_qdrant(
                user_id=user_id,
                memory_id=memory_id,
                vector=vector,
                payload=payload,
                workspace_id=workspace_id_str,
                context_id=context_id_str,
                collection_name=collection,
            )

            # Update embedding status to success
            memory.embedding_status = "success"
            await self.db.commit()

            logger.info(
                "memory_created",
                memory_id=str(memory_id),
                user_id=user_id,
                type=request.type,
                scope="working",
            )

            return RememberResponse(memory_id=memory_id, scope="working")

        except Exception as e:
            # Rollback PostgreSQL transaction
            await self.db.rollback()

            # Log the failure (don't try to save failed status as it complicates recovery)
            # The memory simply won't exist, which is the correct behavior
            logger.error(
                "memory_creation_failed",
                memory_id=str(memory_id),
                user_id=user_id,
                error=str(e),
            )

            raise

    async def reference(self, memory_id: UUID, user_id: str) -> ReferenceResponse:
        """Get full memory details (Layer 3).

        Args:
            memory_id: Memory UUID
            user_id: User ID (for access control)

        Returns:
            ReferenceResponse with full details

        Raises:
            NotFoundException: If memory not found
        """
        memory = await self.memory_repo.get(memory_id)

        if not memory:
            raise NotFoundException("Memory", str(memory_id))

        # Issue #XXX: Team collaboration - verify access permission
        from services.permission_service import PermissionService

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=user_id,
            memory_user_id=memory.user_id,
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
        )

        if not can_access:
            raise NotFoundException("Memory", str(memory_id))

        # Update access stats
        await self.memory_repo.update_access_stats(memory_id, client="api")
        await self.db.commit()

        logger.info("memory_referenced", memory_id=str(memory_id), user_id=user_id)

        return ReferenceResponse(
            memory_id=memory.id,
            summary=memory.summary,
            context_summary=memory.context_summary,
            content=memory.content,
            details=memory.details,
            type=memory.type,
            importance=memory.importance,
            tags=memory.tags or [],
            context=memory.context,
            created_at=memory.created_at,
            client=memory.client,
        )

    async def recall(
        self,
        request: RecallRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> RecallResponse:
        """Search memories with Hybrid Search + Neural Memory.

        Issue #1 + #20 specification:
        Issue #146: Workspace-scoped API keys support
        - Phase 2: Hybrid Search (Semantic 60% + BM25 40%)
        - Phase 3: Neural Memory integration
          - Activation Spreading (graph association)
          - Unified Scoring (semantic + graph + temporal + trust)
          - Co-activation Tracking
          - Hebbian Update (async)

        Issue #82: Context-based multi-collection support.

        Args:
            request: Recall request
            user_id: User ID
            current_context_id: Current context UUID (Issue #82)

        Returns:
            RecallResponse with search results
        """
        import os

        from neural.activation import ActivationSpreader
        from neural.co_activation import CoActivationTracker
        from neural.config import NeuralMemoryConfig
        from neural.hebbian import HebbianLearner
        from neural.models import ActivationState, NeuralMemoryNode
        from neural.scoring import UnifiedScorer
        from repositories.graph import GraphRepository
        from services.graph_service import GraphService

        logger.info(
            "recall_request",
            user_id=user_id,
            query=request.query,
            k=request.k,
            use_rerank=request.use_rerank,
        )

        # Single Collection Migration: Validate required parameters
        if not current_workspace_id or not current_context_id:
            raise ValueError("recall() requires current_workspace_id and current_context_id")

        # Check if Neural Memory is enabled
        neural_enabled = os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() == "true"

        # 1. Primary Retrieval: Hybrid Search (Semantic + BM25)
        candidates_k = request.k * 4 if neural_enabled else request.k
        search_results = await self.search_service.hybrid_search(
            query=request.query,
            user_id=user_id,
            workspace_id=str(current_workspace_id),
            context_id=str(current_context_id),
            k=candidates_k,
            use_rerank=request.use_rerank,
            filters=request.filters,
        )

        # Get full memory data from PostgreSQL
        memory_ids = [r["id"] for r in search_results]

        if not memory_ids:
            return RecallResponse(results=[])

        # Fetch memories from PostgreSQL (exclude soft-deleted)
        result = await self.db.execute(
            select(Memory).where(
                Memory.id.in_(memory_ids),
                Memory.deleted_at.is_(None),  # Exclude deleted memories
            )
        )
        memories_list = list(result.scalars().all())
        memories = {str(m.id): m for m in memories_list}

        # If Neural Memory disabled, use classic Hybrid Search
        if not neural_enabled:
            responses = []
            for search_result in search_results[: request.k]:
                memory_id = search_result["id"]
                memory = memories.get(memory_id)

                if not memory:
                    continue

                # Update access stats
                await self.memory_repo.update_access_stats(
                    memory.id,
                    client=request.filters.get("client", "api") if request.filters else "api",
                )

                # Check for auto-promotion
                await self._check_and_promote(memory)

                responses.append(
                    MemoryResponse(
                        memory_id=memory.id,
                        summary=memory.summary,
                        context_summary=memory.context_summary,
                        type=memory.type,
                        importance=memory.importance,
                        scope=memory.scope,
                        created_at=memory.created_at,
                        client=memory.client,
                        tags=memory.tags or [],
                        context=memory.context,
                        score=search_result.get("hybrid_score", search_result["score"]),
                    )
                )

            await self.db.commit()

            # Issue #104: Aggregate related tags from results
            related_tags = self._aggregate_related_tags(responses, limit=10)

            logger.info("recall_completed", user_id=user_id, results=len(responses), neural=False)
            return RecallResponse(results=responses, related_tags=related_tags)

        # === Neural Memory Integration ===

        # ============================================================================
        # BUG FIX #83-2: Generate query embedding independently
        # ============================================================================
        # Problem: Query embedding was extracted from search_results[0]["embedding"],
        #          which fails if:
        #          1. search_results is empty (no RAG hits)
        #          2. search_results doesn't include embedding field
        #          This defeats the purpose of Neural Memory (graph exploration
        #          should work even without RAG hits).
        #
        # Solution: Generate query embedding FIRST, before RAG search.
        #           This ensures Neural Memory can always function.
        #
        # Benefits:
        #          - Graph exploration works even with zero RAG results
        #          - Query embedding is authentic (not borrowed from result)
        #          - MMR and redundancy calculations use correct query vector
        # ============================================================================

        # 2. Generate query embedding (independent of search results)
        query_embedding = await self.embedding_service.embed(
            request.query,
            user_id,
            context_id=current_context_id,
            workspace_id=current_workspace_id,  # NEW: Issue #146
        )

        # 3. Load user's graph
        graph_repo = GraphRepository(self.db)
        await graph_repo.get_or_create(user_id)

        # Single Collection Migration: Pass workspace_id and context_id to GraphService
        graph_service = GraphService(
            user_id=user_id,
            db=self.db,
            workspace_id=str(current_workspace_id) if current_workspace_id else None,
            context_id=str(current_context_id) if current_context_id else None,
        )

        # 4. Neural Memory components (Issue #107: DB-driven config)
        config = await NeuralMemoryConfig.from_db(self.db)
        activation_spreader = ActivationSpreader(graph_service, config)
        hebbian_learner = HebbianLearner(graph_service, config)
        co_activation_tracker = CoActivationTracker(config)

        # Issue #84 Phase 2C: Load co-activations from Redis (warm start)
        await co_activation_tracker.load_from_redis(user_id)

        unified_scorer = UnifiedScorer(config, activation_spreader)

        # 5. Convert candidates to NeuralMemoryNode format
        neural_candidates = []
        for memory in memories_list:
            # Get embedding from search result
            search_result = next((r for r in search_results if r["id"] == str(memory.id)), None)
            if not search_result:
                continue

            embedding = search_result.get("embedding", [])

            neural_node = NeuralMemoryNode(
                id=str(memory.id),
                user_id=user_id,
                kind=memory.type,
                text=memory.summary,
                embedding=embedding,
                created_at=memory.created_at,
                last_used_at=memory.last_used_at,
                use_count=memory.access_count or 0,
                importance=memory.importance,
                confidence=memory.confidence,
                long_term=(memory.scope == "persistent"),
            )

            neural_candidates.append(
                (neural_node, search_result.get("hybrid_score", search_result["score"]))
            )

        # ============================================================================
        # BUG FIX #83-9: Use search_results order for seed nodes
        # ============================================================================
        # Problem: seed_node_ids was built from memories_list which comes from
        #          SQL WHERE id IN (...) - order is NOT guaranteed.
        #          This meant random 10 memories were used as seeds instead of
        #          the top 10 by relevance score.
        #
        # Solution: Use search_results (ordered by hybrid_score) to select seeds.
        #
        # Impact: Activation spreading now starts from the most relevant memories,
        #         improving Neural Memory quality.
        # ============================================================================

        # 6. Get seed nodes from top search results (ordered by relevance)
        top_ids = [r["id"] for r in search_results[:10]]
        seed_node_ids = [mid for mid in top_ids if mid in memories]

        # 7. Unified Scoring (Semantic + Graph + Temporal + Trust)

        scored_results = await unified_scorer.score_candidates(
            query_embedding=query_embedding,
            candidates=neural_candidates,
            seed_nodes=seed_node_ids,
            selected_nodes=None,
        )

        # 8. Sort and limit
        scored_results.sort(key=lambda r: r.score, reverse=True)
        scored_results = scored_results[: request.k]

        # 9. Co-activation Tracking
        activated_nodes = [
            ActivationState(node_id=result.node.id, activation=result.score)
            for result in scored_results
        ]
        co_activation_tracker.record_activation(user_id, activated_nodes)

        # Issue #84 Phase 2C: Persist to Redis (7-day TTL, survives restarts)
        await co_activation_tracker.save_to_redis(user_id)

        # 10. Add nodes to graph (if not already present)
        nodes_dict = {result.node.id: result.node for result in scored_results}
        nodes_added = 0
        for node_id, node in nodes_dict.items():
            if not await graph_service.has_node(node_id):
                await graph_service.add_node(
                    node_id=node_id,
                    node_type="memory",
                    data={
                        "user_id": user_id,
                        "kind": node.kind,
                        "text": node.text,
                        "created_at": node.created_at,
                        "importance": node.importance,
                        "confidence": node.confidence,
                        "long_term": node.long_term,
                    },
                )
                nodes_added += 1

        # 11. Queue and apply Hebbian updates (Issue #84: async)
        # Single Collection Migration: collection_name removed (always "kagura_memories")
        await hebbian_learner.queue_update(user_id, activated_nodes, nodes_dict)
        edges_updated = await hebbian_learner.apply_updates(user_id)

        # 12. Save graph - deprecated in SQL backend (Issue #84)
        # Graph data now persisted directly in neural_memory_edges table
        # No need to save JSON to graph_memory table
        logger.info(
            "graph_updated",
            user_id=user_id,
            nodes_added=nodes_added,
            edges_updated=edges_updated,
        )

        # 12. Build response
        responses = []
        for recall_result in scored_results:
            memory = memories.get(recall_result.node.id)
            if not memory:
                continue

            # Update access stats
            await self.memory_repo.update_access_stats(
                memory.id,
                client=request.filters.get("client", "api") if request.filters else "api",
            )

            # Issue #84 Phase 2B: Sync graph node (no-op in SQL backend, but ensures consistency)
            await graph_service.sync_node_from_memory(memory.id)

            # Check for auto-promotion
            await self._check_and_promote(memory)

            responses.append(
                MemoryResponse(
                    memory_id=memory.id,
                    summary=memory.summary,
                    context_summary=memory.context_summary,
                    type=memory.type,
                    importance=memory.importance,
                    scope=memory.scope,
                    created_at=memory.created_at,
                    client=memory.client,
                    tags=memory.tags or [],
                    context=memory.context,
                    score=recall_result.score,
                )
            )

        await self.db.commit()

        # Issue #104: Aggregate related tags from results
        related_tags = self._aggregate_related_tags(responses, limit=10)

        logger.info("recall_completed", user_id=user_id, results=len(responses), neural=True)

        return RecallResponse(results=responses, related_tags=related_tags)

    async def forget(
        self,
        request: ForgetRequest,
        user_id: str,
        current_context_id: UUID | None = None,
    ) -> ForgetResponse:
        """Delete memory (single or multiple via query).

        Issue #82: Context-based multi-collection support.

        Args:
            request: Forget request (memory_id or query)
            user_id: User ID
            current_context_id: Current context UUID (Issue #82)

        Returns:
            ForgetResponse with deleted count and IDs
        """
        # Single Collection Migration: Extract isolation params (optimized)
        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id, current_context_id
        )

        deleted_ids = []

        # Case 1: Delete by memory_id
        if request.memory_id:
            memory = await self.memory_repo.get(request.memory_id)

            if memory:
                # Issue #XXX: Team collaboration - verify delete permission
                from services.permission_service import PermissionService

                perm_service = PermissionService(self.db)
                can_access = await perm_service.can_access_memory(
                    user_id=user_id,
                    memory_user_id=memory.user_id,
                    workspace_id=memory.workspace_id,
                    context_id=memory.context_id,
                )

                if not can_access:
                    logger.warning(
                        "forget_access_denied",
                        memory_id=str(request.memory_id),
                        user_id=user_id,
                    )
                    # Return empty response instead of error for security
                    return ForgetResponse(deleted_count=0, memory_ids=[])
                # Migration 063: Get workspace_id/context_id from memory directly
                # CRITICAL: Validate workspace_id/context_id are not NULL (data integrity)
                if not memory.workspace_id or not memory.context_id:
                    raise ValueError(
                        f"Memory {memory.id} has NULL workspace_id/context_id. "
                        "This indicates data migration issue. Run Migration 063."
                    )

                memory_workspace_id = str(memory.workspace_id)
                memory_context_id = str(memory.context_id)

                # Soft delete in PostgreSQL (set deleted_at, deleted_by)

                memory.deleted_at = utcnow()
                memory.deleted_by = user_id
                await self.memory_repo.update(memory.id, memory)

                # Hard delete from Qdrant (remove from search index)
                del_collection = await self._get_context_collection_name(memory.context_id)
                await delete_memory_from_qdrant(
                    user_id, request.memory_id, collection_name=del_collection
                )

                # Clean up neural memory edges with 3-level isolation
                from repositories.neural_edge import NeuralEdgeRepository

                edge_repo = NeuralEdgeRepository(self.db)
                edges_deleted = await edge_repo.delete_node_edges(
                    user_id=user_id,
                    node_id=request.memory_id,
                    workspace_id=memory_workspace_id,
                    context_id=memory_context_id,
                )
                if edges_deleted > 0:
                    logger.info(
                        "neural_edges_cleaned",
                        memory_id=str(request.memory_id),
                        edges_deleted=edges_deleted,
                        user_id=user_id,
                    )

                deleted_ids.append(request.memory_id)

                logger.info(
                    "memory_soft_deleted", memory_id=str(request.memory_id), user_id=user_id
                )

        # Case 2: Delete by query (search and delete)
        elif request.query:
            # Search for matching memories
            recall_request = RecallRequest(
                query=request.query,
                k=request.k,
                use_rerank=False,  # No reranking for delete
                filters=None,
            )

            # Issue #82: Pass project ID to recall
            search_response = await self.recall(recall_request, user_id, current_context_id)

            # Soft delete each found memory
            for memory_response in search_response.results:
                memory = await self.memory_repo.get(memory_response.memory_id)
                if memory:
                    memory.deleted_at = utcnow()
                    memory.deleted_by = user_id
                    await self.memory_repo.update(memory.id, memory)

                    # Hard delete from Qdrant
                    del_collection = await self._get_context_collection_name(memory.context_id)
                    await delete_memory_from_qdrant(
                        user_id, memory_response.memory_id, collection_name=del_collection
                    )

                    # Clean up neural memory edges
                    from repositories.neural_edge import NeuralEdgeRepository

                    edge_repo = NeuralEdgeRepository(self.db)
                    edges_deleted = await edge_repo.delete_node_edges(
                        user_id, memory_response.memory_id
                    )
                    if edges_deleted > 0:
                        logger.info(
                            "neural_edges_cleaned",
                            memory_id=str(memory_response.memory_id),
                            edges_deleted=edges_deleted,
                            user_id=user_id,
                        )

                    deleted_ids.append(memory_response.memory_id)

            logger.info(
                "memories_soft_deleted_by_query",
                query=request.query,
                count=len(deleted_ids),
                user_id=user_id,
            )

        await self.db.commit()

        return ForgetResponse(deleted_count=len(deleted_ids), memory_ids=deleted_ids)

    async def _check_and_promote(self, memory: Memory) -> None:
        """Check and promote working memory to persistent.

        Args:
            memory: Memory entity
        """
        if memory.scope == "persistent":
            return

        age_days = (utcnow() - memory.created_at).days

        if should_promote_to_persistent(
            access_count=memory.access_count or 0,
            age_days=age_days,
            importance=memory.importance,
            accessed_by_clients=memory.accessed_by_clients or [],
        ):
            await self.memory_repo.promote_to_persistent(memory.id)
            await self.db.commit()

            logger.info(
                "memory_promoted",
                memory_id=str(memory.id),
                access_count=memory.access_count,
                age_days=age_days,
                importance=memory.importance,
            )

    async def cleanup_old_working_memories(self, user_id: str, age_days: int = 30) -> int:
        """Cleanup old working memories (30+ days, not promoted).

        Args:
            user_id: User ID
            age_days: Minimum age in days (default: 30)

        Returns:
            Number of deleted memories
        """
        from config.retention import WORKING_MEMORY_RETENTION_DAYS

        age_days = age_days or WORKING_MEMORY_RETENTION_DAYS

        # Get old working memories
        old_memories = await self.memory_repo.get_old_working_memories(user_id, age_days)

        deleted_count = 0

        for memory in old_memories:
            # Skip if already promoted or high importance
            if memory.scope == "persistent" or memory.importance >= 0.7:
                continue

            # Delete from PostgreSQL
            await self.memory_repo.delete(memory.id)

            # Delete from Qdrant
            del_collection = await self._get_context_collection_name(memory.context_id)
            await delete_memory_from_qdrant(user_id, memory.id, collection_name=del_collection)

            deleted_count += 1

        await self.db.commit()

        logger.info(
            "old_memories_cleaned_up",
            user_id=user_id,
            deleted=deleted_count,
            age_days=age_days,
        )

        return deleted_count

    async def explore(
        self,
        request: ExploreRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
    ) -> ExploreResponse:
        """Explore related memories via graph traversal with 3-level isolation.

        Neural Memory graph exploration using Activation Spreading.
        Single Collection Migration: Added workspace_id and context_id for isolation.

        Args:
            request: Explore request
            user_id: User ID
            current_context_id: Current context UUID
            current_workspace_id: Workspace ID

        Returns:
            ExploreResponse with related memories

        Example:
            >>> result = await service.explore(request, "kiyota")
            >>> result.related_memories
            [RelatedMemoryResponse(...), ...]
        """
        from sqlalchemy import select

        from neural.activation import ActivationSpreader
        from neural.config import NeuralMemoryConfig
        from repositories.graph import GraphRepository
        from services.graph_service import GraphService

        logger.info(
            "explore_request",
            user_id=user_id,
            memory_id=str(request.memory_id),
            depth=request.depth,
        )

        # 1. Get seed memory
        seed_memory = await self.memory_repo.get(request.memory_id)
        if not seed_memory:
            raise NotFoundException(f"Memory {request.memory_id} not found")

        # Issue #XXX: Team collaboration - verify access permission
        from services.permission_service import PermissionService

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=user_id,
            memory_user_id=seed_memory.user_id,
            workspace_id=seed_memory.workspace_id,
            context_id=seed_memory.context_id,
        )

        if not can_access:
            raise NotFoundException(f"Memory {request.memory_id} not found")

        # Migration 063: Get workspace_id and context_id directly from seed memory
        # CRITICAL: Validate seed memory has workspace_id/context_id (data integrity)
        if not seed_memory.workspace_id or not seed_memory.context_id:
            raise ValueError(
                f"Seed memory {seed_memory.id} has NULL workspace_id/context_id. "
                "This indicates data migration issue. Run Migration 063."
            )

        if not current_context_id:
            current_context_id = seed_memory.context_id
        if not current_workspace_id:
            current_workspace_id = seed_memory.workspace_id

        # 2. Load user's graph
        graph_repo = GraphRepository(self.db)
        await graph_repo.get_or_create(user_id)

        # 3. Convert to GraphService with 3-level isolation
        graph_service = GraphService(
            user_id=user_id,
            db=self.db,
            workspace_id=str(current_workspace_id) if current_workspace_id else None,
            context_id=str(current_context_id) if current_context_id else None,
        )

        # 4. Check if seed node exists in graph
        if not await graph_service.has_node(str(request.memory_id)):
            # Return empty result if seed not in graph
            seed_response = MemoryResponse(
                memory_id=seed_memory.id,
                summary=seed_memory.summary,
                context_summary=seed_memory.context_summary,
                type=seed_memory.type,
                importance=seed_memory.importance,
                scope=seed_memory.scope,
                created_at=seed_memory.created_at,
                client=seed_memory.client,
                tags=seed_memory.tags or [],
                context=seed_memory.context,
                score=None,
            )

            return ExploreResponse(
                seed_memory=seed_response,
                related_memories=[],
                metadata={
                    "total_activated": 0,
                    "returned": 0,
                    "reason": "seed_not_in_graph",
                },
            )

        # 5. Activation spreading (Issue #107: DB-driven config)
        config = await NeuralMemoryConfig.from_db(self.db)
        spreader = ActivationSpreader(graph_service, config)

        seed_activations = {str(request.memory_id): 1.0}
        activated = await spreader.spread(
            seed_activations=seed_activations,
            max_hops=request.depth,
            user_id=user_id,
        )

        # 6. Filter by weight and exclude seed
        filtered_activations = [
            act
            for act in activated
            if act.node_id != str(request.memory_id)  # Exclude seed
            and act.hop > 0  # Only non-seed nodes
        ]

        # Filter by min_weight
        if request.min_weight > 0:
            filtered_activations = [
                act for act in filtered_activations if act.activation >= request.min_weight
            ]

        # 7. Get memory details from PostgreSQL
        memory_ids = [UUID(act.node_id) for act in filtered_activations[:50]]  # Limit to 50

        if not memory_ids:
            seed_response = MemoryResponse(
                memory_id=seed_memory.id,
                summary=seed_memory.summary,
                context_summary=seed_memory.context_summary,
                type=seed_memory.type,
                importance=seed_memory.importance,
                scope=seed_memory.scope,
                created_at=seed_memory.created_at,
                client=seed_memory.client,
                tags=seed_memory.tags or [],
                context=seed_memory.context,
                score=None,
            )

            return ExploreResponse(
                seed_memory=seed_response,
                related_memories=[],
                metadata={
                    "total_activated": len(activated),
                    "returned": 0,
                    "filtered_out": len(activated)
                    - len(
                        [a for a in activated if a.hop > 0 and a.activation >= request.min_weight]
                    ),
                    "suggestion": f"All {len(activated)} activated nodes filtered out by min_weight={request.min_weight}. Try lowering to 0.0-0.05 for results (typical edge weights: 0.02-0.05).",
                },
            )

        result = await self.db.execute(
            select(Memory).where(
                Memory.id.in_(memory_ids),
                Memory.deleted_at.is_(None),  # Exclude deleted memories
            )
        )
        memories = {str(m.id): m for m in result.scalars().all()}

        # 8. Build response
        related_memories = []
        activation_map = {act.node_id: act for act in filtered_activations}

        for memory_id, memory in memories.items():
            activation = activation_map.get(memory_id)
            if not activation:
                continue

            # Get edge weight
            edge_data = await graph_service.get_edge(str(request.memory_id), memory_id)
            weight = edge_data["weight"] if edge_data else 0.0

            # Reconstruct path (simple: seed -> current)
            path = [request.memory_id, UUID(memory_id)]

            related_memories.append(
                RelatedMemoryResponse(
                    memory_id=memory.id,
                    summary=memory.summary,
                    context_summary=memory.context_summary,
                    type=memory.type,
                    activation=activation.activation,
                    hop=activation.hop,
                    weight=weight,
                    path=path,
                )
            )

        # Sort by activation (descending)
        related_memories.sort(key=lambda x: x.activation, reverse=True)

        # Limit to top 10
        related_memories = related_memories[:10]

        seed_response = MemoryResponse(
            memory_id=seed_memory.id,
            summary=seed_memory.summary,
            context_summary=seed_memory.context_summary,
            type=seed_memory.type,
            importance=seed_memory.importance,
            scope=seed_memory.scope,
            created_at=seed_memory.created_at,
            client=seed_memory.client,
            tags=seed_memory.tags or [],
            context=seed_memory.context,
            score=None,
        )

        logger.info(
            "explore_completed",
            user_id=user_id,
            total_activated=len(activated),
            returned=len(related_memories),
        )

        return ExploreResponse(
            seed_memory=seed_response,
            related_memories=related_memories,
            metadata={
                "total_activated": len(activated),
                "returned": len(related_memories),
                "filtered_out": len(activated) - 1 - len(filtered_activations),  # -1 for seed
                "max_activation": max(
                    [act.activation for act in filtered_activations], default=0.0
                ),
                "min_activation": min([act.activation for act in filtered_activations], default=0.0)
                if filtered_activations
                else 0.0,
            },
        )

    async def get_stats(
        self,
        user_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
        include_details: bool = True,
        time_window_hours: int = 168,
        is_shared_context: bool = False,
    ) -> MemoryStatsResponse:
        """Get memory usage statistics with 3-level isolation.

        Single Collection Migration: Uses workspace_id/context_id instead of collection_name.

        Args:
            user_id: User ID
            workspace_id: Workspace ID (for context-scoped stats)
            context_id: Context ID (for context-scoped stats)
            include_details: Include type/importance breakdown
            time_window_hours: Recent activity window in hours (default: 168 = 7 days)
            is_shared_context: If True, count all members' memories (not just user_id)

        Returns:
            MemoryStatsResponse with counts and breakdowns
        """
        from datetime import timedelta

        from sqlalchemy import and_, case, func, literal_column, select

        from models.memory import Memory
        from models.schemas import MemoryStatsResponse

        # Build base filter conditions
        # Issue #204: For shared contexts, count all members' memories
        # - Private contexts: Filter by user_id (creator's memories only)
        # - Shared contexts: No user_id filter (all members' memories)
        base_conditions = [Memory.deleted_at.is_(None)]
        if not is_shared_context:
            base_conditions.append(Memory.user_id == user_id)  # Private: creator only

        # Single Collection Migration: Filter by workspace_id and context_id
        if workspace_id:
            base_conditions.append(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            base_conditions.append(Memory.context_id == UUID(context_id))

        # Total count (exclude soft-deleted)
        total_result = await self.db.execute(
            select(func.count(Memory.id)).where(and_(*base_conditions))
        )
        total_count = total_result.scalar() or 0

        # Working vs Persistent (exclude soft-deleted)
        working_result = await self.db.execute(
            select(func.count(Memory.id)).where(and_(*base_conditions, Memory.scope == "working"))
        )
        working_count = working_result.scalar() or 0
        persistent_count = total_count - working_count

        by_type = {}
        by_importance = {}
        recent_activity = 0

        # Add details if requested
        if include_details:
            # By type
            type_result = await self.db.execute(
                select(Memory.type, func.count(Memory.id))
                .where(and_(*base_conditions))
                .group_by(Memory.type)
            )
            by_type = {row[0]: row[1] for row in type_result.all()}

            # By importance level
            importance_case = case(
                (Memory.importance >= 0.7, literal_column("'high'")),
                (Memory.importance >= 0.4, literal_column("'medium'")),
                else_=literal_column("'low'"),
            )

            importance_result = await self.db.execute(
                select(importance_case, func.count(Memory.id))
                .where(and_(*base_conditions))
                .group_by(importance_case)
            )
            by_importance = {row[0]: row[1] for row in importance_result.all()}

            # Recent activity (exclude soft-deleted)
            recent_result = await self.db.execute(
                select(func.count(Memory.id)).where(
                    and_(
                        *base_conditions,
                        Memory.created_at >= utcnow() - timedelta(hours=time_window_hours),
                    )
                )
            )
            recent_activity = recent_result.scalar() or 0

        return MemoryStatsResponse(
            total_count=total_count,
            working_count=working_count,
            persistent_count=persistent_count,
            by_type=by_type,
            by_importance=by_importance,
            recent_activity=recent_activity,
        )

    def _aggregate_related_tags(
        self, responses: list[MemoryResponse], limit: int = 10
    ) -> list[RelatedTagItem]:
        """Aggregate tags from recall results.

        Issue #104: Extract related tags from search results to help LLMs
        understand tag context without overwhelming them with all tags.

        Args:
            responses: List of memory responses
            limit: Maximum number of tags to return (default: 10)

        Returns:
            List of RelatedTagItem with tag, count, and sample_summary
        """
        from collections import Counter

        # Count tag occurrences
        tag_counter: Counter[str] = Counter()
        tag_samples: dict[str, str] = {}  # tag -> first summary encountered

        for response in responses:
            if response.tags:
                for tag in response.tags:
                    tag_counter[tag] += 1
                    # Store first summary as sample
                    if tag not in tag_samples:
                        tag_samples[tag] = response.summary

        # Sort by count (descending) and take top N
        top_tags = tag_counter.most_common(limit)

        # Build RelatedTagItem list
        related_tags = [
            RelatedTagItem(
                tag=tag,
                count=count,
                sample_summary=tag_samples.get(tag),
            )
            for tag, count in top_tags
        ]

        return related_tags
