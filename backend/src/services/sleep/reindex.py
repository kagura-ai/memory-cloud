"""Sleep Maintenance Phase 5: Re-index.

Issue #101: Re-embed and upsert changed memories to Qdrant.
Processes memory IDs collected from earlier phases (dedup merge,
importance re-eval, etc.) to keep Qdrant in sync with PostgreSQL.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import add_memory_to_qdrant
from models.memory import Memory
from services.content_chunk_writer import (
    content_chunks_enabled,
    requeue_embedding,
    sync_content_chunk_payload,
    write_content_chunks,
)
from services.embedding_service import EmbeddingService
from services.resource_indexer import ResourceIndexer, owns_resource_point
from services.sleep.reporter import PhaseResult
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
        *,
        content_changed_memory_ids: set[UUID] | None = None,
    ) -> PhaseResult:
        """Re-index all changed memories.

        For each memory ID:
        1. Fetch current state from PostgreSQL
        2. Re-generate embedding via EmbeddingService
        3. Upsert to Qdrant with updated payload
        4. Content chunk points (#2015): rewritten (re-embedded) when the
           memory's content changed; otherwise only their payload is synced
           from the new summary payload, without an embedding (D5).

        Skips individual failures to allow partial success.

        Args:
            changed_memory_ids: Set of memory IDs modified by earlier phases
            user_id: User ID for Qdrant isolation
            workspace_id: Workspace ID for Qdrant isolation
            context_id: Context ID for Qdrant isolation
            content_changed_memory_ids: The subset whose content changed.
                None = unknown: every changed memory's chunks are rewritten.

        Returns:
            PhaseResult with reindex statistics
        """
        # Call-time import: memory_service is a heavy module and this phase
        # only needs its point builder.
        from services.memory_service import build_memory_point

        result = PhaseResult(phase_name="reindex")

        if not changed_memory_ids:
            result.details = {"message": "no_memories_to_reindex"}
            return result

        reindexed = 0
        failed = 0
        failed_ids: list[str] = []
        # #2015 D5: unknown means changed — an extra embed, never a stale chunk.
        content_changed = (
            changed_memory_ids if content_changed_memory_ids is None else content_changed_memory_ids
        )
        chunk_rewrites = 0
        chunk_requeues = 0

        # Batch-fetch all changed memories in one query (avoid N+1)
        stmt = select(Memory).where(
            Memory.id.in_(changed_memory_ids),
            Memory.deleted_at.is_(None),
        )
        row = await self.db.execute(stmt)
        memory_map = {m.id: m for m in row.scalars().all()}

        # #2015: the context's content-chunk flag, read once for the batch
        # (None = unknown; the writer then looks it up per memory).
        chunks_enabled: bool | None = None
        if context_id:
            try:
                chunks_enabled = await content_chunks_enabled(self.db, UUID(str(context_id)))
            except Exception:  # noqa: BLE001 — the writer retries the lookup
                chunks_enabled = None

        # #2053: built on the first resource-indexer row, then reused.
        resource_indexer: ResourceIndexer | None = None

        for memory_id in changed_memory_ids:
            memory = memory_map.get(memory_id)
            if not memory:
                logger.debug("reindex_skip_deleted", memory_id=str(memory_id))
                continue

            try:
                if owns_resource_point(memory):
                    # #2053: a row the resource indexer wrote owns the
                    # indexer's point (``Memory.point_id``, embedded from the
                    # document). The path below would embed its label summary
                    # and write a stray point under the row id; the indexer
                    # rebuilds its own point instead, as
                    # process_pending_embedding does (#1870). It has no
                    # content chunks to rewrite.
                    if resource_indexer is None:
                        resource_indexer = ResourceIndexer(self.db)
                    usage: dict[str, int] = {}
                    try:
                        await resource_indexer.rebuild_point(
                            memory,
                            collection_name=self.collection_name,
                            embedding_service=self.embedding_service,
                            usage=usage,
                        )
                    finally:
                        # Set once the embedding answered: a failed upsert
                        # still spent it.
                        if "tokens" in usage:
                            self._account_embedding(result, usage["tokens"])
                    reindexed += 1
                    continue

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
                self._account_embedding(result, tokens)
                # #2053: the upsert replaces the WHOLE point, so it must be
                # the point the normal write path builds — BM25 sparse
                # vector, tokenized/normalized payload and the geo field
                # (#1332) included. Only the dense vector is built here.
                payload, sparse_indices, sparse_values = build_memory_point(memory)

                await add_memory_to_qdrant(
                    user_id=user_id,
                    memory_id=memory.id,
                    vector=vector,
                    payload=payload,
                    workspace_id=workspace_id,
                    context_id=context_id,
                    sparse_indices=sparse_indices,
                    sparse_values=sparse_values,
                    collection_name=self.collection_name,
                )
                # #2015: a content change rewrites the chunk points
                # (re-embedded, in place). A metadata-only change keeps
                # their vectors and copies the new payload onto them — a
                # promotion's scope reaches them only this way; chunks that
                # could not be updated are rewritten instead.
                if memory_id in content_changed or not await sync_content_chunk_payload(
                    self.db,
                    memory,
                    base_payload=payload,
                    collection_name=self.collection_name,
                    chunks_enabled=chunks_enabled,
                ):
                    outcome = await self._write_chunks(result, memory, payload, chunks_enabled)
                    if outcome == "written":
                        chunk_rewrites += 1
                    elif outcome == "requeued":
                        chunk_requeues += 1

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
            # #2015: memories whose content chunk points were rewritten.
            "content_chunk_rewrites": chunk_rewrites,
            # #2061: memories whose chunk write failed, re-queued for the
            # embedding sweep (still counted as reindexed).
            "content_chunk_requeued": chunk_requeues,
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

    async def _write_chunks(
        self,
        result: PhaseResult,
        memory: Memory,
        payload: dict,
        chunks_enabled: bool | None,
    ) -> Literal["written", "requeued", "none"]:
        """Rewrite the memory's chunk points.

        Returns ``"written"`` when chunk points were written, ``"none"`` when
        there was nothing to write (flag off, short content, superseded).
        A failed write (#2061) re-queues the memory instead: the Sleep run has
        no retry of its own, and the memory is at least a week old, so
        nothing else would rebuild its chunks — the embedding sweep does
        (``"requeued"``). ``"none"`` when the row was not ``success``: a
        ``pending`` or ``processing`` row is being embedded, a ``failed`` one
        waits for its retry (or, with its budget spent, the admin retry). A
        failed re-queue is raised: the caller counts the memory as failed.

        A provider request for the chunks counts as one embedding call with
        its tokens (#2015), also when the write then failed — the provider
        billed it. A batch served from the cache alone sends no request and
        counts none.
        """
        usage: dict[str, int] = {}
        try:
            written = await write_content_chunks(
                self.db,
                memory,
                base_payload=payload,
                embed_svc=self.embedding_service,
                collection_name=self.collection_name,
                chunks_enabled=chunks_enabled,
                fail_open=False,
                usage=usage,
            )
        except Exception:
            # Logged by the writer (content_chunks_failed, error class only).
            requeued = await requeue_embedding(self.db, memory.id, reason="chunk_write_failed")
            return "requeued" if requeued else "none"
        finally:
            if usage.get("requests"):
                self._account_embedding(result, usage.get("tokens", 0))
        return "written" if written else "none"

    def _account_embedding(self, result: PhaseResult, tokens: int) -> None:
        """Count one embedding call and its tokens on ``result``."""
        result.embedding_calls_used += 1
        result.embedding_tokens += tokens
        # Embedding model is instance-global per the ``EmbeddingService``
        # config; capture the (provider, model) identity from the service on
        # the first call so the reporter can populate the scalar columns on
        # ``sleep_reports``.
        if result.embedding_provider is None:
            result.embedding_provider = self.embedding_service.provider
            result.embedding_model = self.embedding_service.model
