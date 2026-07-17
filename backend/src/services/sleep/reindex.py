"""Sleep Maintenance Phase 5: Re-index.

Issue #101: Re-embed and upsert changed memories to Qdrant.
Processes memory IDs collected from earlier phases (dedup merge,
importance re-eval, etc.) to keep Qdrant in sync with PostgreSQL.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import add_memory_to_qdrant
from models.memory import Memory
from services.embedding_service import EmbeddingService
from services.sleep.reporter import PhaseResult
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)


class ReindexPhase:
    """Re-embed and upsert changed memories to Qdrant."""

    def __init__(
        self,
        db: AsyncSession,
        embedding_model: str | None = None,
        collection_name: str | None = None,
    ):
        self.db = db
        self.embedding_service = EmbeddingService(db, model=embedding_model)
        self.collection_name = collection_name or "kagura_memories"

    async def execute(
        self,
        changed_memory_ids: set[UUID],
        user_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> PhaseResult:
        """Re-index all changed memories.

        For each memory ID:
        1. Fetch current state from PostgreSQL
        2. Re-generate embedding via EmbeddingService
        3. Upsert to Qdrant with updated payload

        Skips individual failures to allow partial success.

        Args:
            changed_memory_ids: Set of memory IDs modified by earlier phases
            user_id: User ID for Qdrant isolation
            workspace_id: Workspace ID for Qdrant isolation
            context_id: Context ID for Qdrant isolation

        Returns:
            PhaseResult with reindex statistics
        """
        result = PhaseResult(phase_name="reindex")

        if not changed_memory_ids:
            result.details = {"message": "no_memories_to_reindex"}
            return result

        reindexed = 0
        failed = 0
        failed_ids: list[str] = []

        # Batch-fetch all changed memories in one query (avoid N+1)
        stmt = select(Memory).where(
            Memory.id.in_(changed_memory_ids),
            Memory.deleted_at.is_(None),
        )
        row = await self.db.execute(stmt)
        memory_map = {m.id: m for m in row.scalars().all()}

        for memory_id in changed_memory_ids:
            memory = memory_map.get(memory_id)
            if not memory:
                logger.debug("reindex_skip_deleted", memory_id=str(memory_id))
                continue

            try:
                # #471: use embed_with_usage() to capture token count for
                # cost-grade reporting. Cache hits return (vector, 0) which
                # is the correct cost attribution — cached responses don't
                # bill API tokens.
                vector, tokens = await self.embedding_service.embed_with_usage(
                    memory.summary,
                    user_id=user_id,
                    context_id=context_id,
                    workspace_id=workspace_id,
                )
                result.embedding_calls_used += 1
                result.embedding_tokens += tokens
                # Embedding model is instance-global per the
                # ``EmbeddingService`` config; capture the (provider, model)
                # identity from the service on the first call so the
                # reporter can populate the scalar columns on
                # ``sleep_reports``.
                if result.embedding_provider is None:
                    result.embedding_provider = self.embedding_service.provider
                    result.embedding_model = self.embedding_service.model

                # Payload aligned with memory_service.py canonical format
                payload = {
                    "user_id": user_id,
                    "summary": memory.summary,
                    "context_summary": memory.context_summary or "",
                    "type": memory.type,
                    "importance": memory.importance,
                    "scope": memory.scope,
                    "tags": memory.tags or [],
                    "created_at": to_utc_iso(memory.created_at),
                    "updated_at": to_utc_iso(memory.updated_at),
                }
                # WHERE axis (#1332): the upsert replaces the WHOLE payload —
                # without carrying the geo field a reindexed located memory
                # would vanish from filters.near (must semantics). The e69
                # generated columns are the single validated source.
                if memory.location_lat is not None and memory.location_lon is not None:
                    payload["location"] = {
                        "lat": memory.location_lat,
                        "lon": memory.location_lon,
                    }

                await add_memory_to_qdrant(
                    user_id=user_id,
                    memory_id=memory.id,
                    vector=vector,
                    payload=payload,
                    workspace_id=workspace_id,
                    context_id=context_id,
                    collection_name=self.collection_name,
                )

                reindexed += 1

            except Exception as e:
                failed += 1
                failed_ids.append(str(memory_id))
                logger.warning(
                    "reindex_memory_failed",
                    memory_id=str(memory_id),
                    error=str(e),
                )

        result.memories_processed = reindexed + failed
        result.changed_memory_ids = changed_memory_ids
        result.details = {
            "reindexed": reindexed,
            "failed": failed,
            "failed_ids": failed_ids[:20],  # Cap for report size
        }

        if failed > 0 and reindexed == 0:
            result.success = False
            result.error = f"All {failed} reindex operations failed"

        logger.info(
            "reindex_phase_completed",
            reindexed=reindexed,
            failed=failed,
            total=len(changed_memory_ids),
        )

        return result
