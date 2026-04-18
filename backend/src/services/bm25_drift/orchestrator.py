"""Per-context orchestration for BM25 IDF drift measurement.

Issue #343: composes idf_snapshot + psi_calculator + persistence. Mirrors
the SleepOrchestrator pattern (services/sleep/orchestrator.py): one
instance per (db, qdrant) pair, .run() exec per context.
"""

from __future__ import annotations

from uuid import UUID

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import get_qdrant_client
from models.bm25_drift import Bm25IdfDriftLog
from services.bm25_drift.idf_snapshot import build_idf_snapshot
from services.bm25_drift.psi_calculator import compute_psi
from services.context_routing import resolve_collection_name
from utils.logger import get_logger

logger = get_logger(__name__)


class Bm25DriftOrchestrator:
    """Run a single PSI measurement for one context and persist the result."""

    def __init__(
        self,
        db: AsyncSession,
        qdrant: AsyncQdrantClient | None = None,
    ) -> None:
        self.db = db
        # Reuse the global Qdrant singleton unless the caller injects one
        # (test seam). Constructor takes an explicit param so tests can
        # patch without monkey-patching get_qdrant_client.
        self._qdrant = qdrant

    def _get_qdrant(self) -> AsyncQdrantClient:
        if self._qdrant is None:
            self._qdrant = get_qdrant_client()
        return self._qdrant

    async def run(self, context_id: UUID) -> Bm25IdfDriftLog:
        """Compute drift for `context_id` and INSERT one row.

        The caller owns transaction lifecycle (commit/rollback). On error
        the row is not flushed; orchestrator does NOT swallow exceptions.
        """
        collection_name = await resolve_collection_name(self.db, context_id)
        qdrant = self._get_qdrant()

        snapshot = await build_idf_snapshot(qdrant, collection_name, context_id)

        result = compute_psi(
            df_memory=snapshot.df_memory,
            df_global=snapshot.df_global,
            m_memory=snapshot.m_memory,
            n_global=snapshot.n_global,
        )

        row = Bm25IdfDriftLog(
            context_id=context_id,
            psi=result.psi,
            psi_status=result.status,
            m_memory_points=snapshot.m_memory,
            r_resource_points=snapshot.r_resource,
            num_terms=result.num_terms,
            top_divergent_terms=result.top_divergent_terms or None,
        )
        self.db.add(row)
        await self.db.flush()

        # Structured log event — operator-side observability for v0.12.1.
        # Prometheus gauge wiring is deferred to the v0.14.0 enable issue.
        # Term content is intentionally NOT in this event (CSO/CLO).
        logger.info(
            "bm25_idf_drift_computed",
            context_id=str(context_id),
            collection=collection_name,
            psi=float(result.psi) if result.psi is not None else None,
            status=result.status,
            m_memory=snapshot.m_memory,
            r_resource=snapshot.r_resource,
            num_terms=result.num_terms,
        )

        return row
