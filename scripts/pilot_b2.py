"""Pilot script: broadlistening pipeline end-to-end run (#533).

Usage (inside kagura-api container):
    PYTHONPATH=src PYTHONUNBUFFERED=1 python -u /tmp/pilot_b2.py

Env vars:
    NUMBA_DISABLE_JIT=1   — disable numba JIT to test hypothesis 3
"""

import asyncio
import os
import sys
import time
from uuid import UUID

# Diagnostics
print(f"Python: {sys.version}", flush=True)
print(
    f"NUMBA_DISABLE_JIT: {os.environ.get('NUMBA_DISABLE_JIT', 'not set')}", flush=True
)

WORKSPACE_ID = UUID("e4b22965-77b0-4a5d-a23e-10c15b6590b8")
CONTEXT_ID = UUID("700a6873-ade0-44ba-beca-95e8cda3ad82")
USER_ID = "local:admin"


async def main() -> None:
    from db.base import get_db
    from services.analysis.orchestrator import AnalysisOrchestrator, AnalysisParams

    from sqlalchemy import text

    # Avoid importlib deadlock when to_thread workers race on lazy imports.
    print("Pre-warming umap + sklearn...", flush=True)
    t_prewarm = time.monotonic()
    import umap  # noqa: F401
    from sklearn.cluster import KMeans  # noqa: F401
    from sklearn.metrics import silhouette_score  # noqa: F401

    print(f"Pre-warm done in {time.monotonic() - t_prewarm:.2f}s", flush=True)

    t0 = time.monotonic()
    analysis = None

    print("Connecting to DB...", flush=True)
    async for db in get_db():
        orchestrator = AnalysisOrchestrator(db)

        # Phase 1: start() — BYOK check, idempotency, row create
        print("Phase 1: start()...", flush=True)
        analysis = await orchestrator.start(
            workspace_id=WORKSPACE_ID,
            context_id=CONTEXT_ID,
            user_id=USER_ID,
            params=AnalysisParams(),
        )
        await db.commit()
        print(
            f"Phase 1 done in {time.monotonic() - t0:.2f}s — "
            f"run_id={analysis.id} status={analysis.status}",
            flush=True,
        )

    if analysis is None:
        print("ERROR: Phase 1 did not complete", flush=True)
        return

    # Phase 2: run() — fresh session (mirrors production task pattern)
    print("Phase 2: run()...", flush=True)
    t1 = time.monotonic()
    async for db2 in get_db():
        orchestrator2 = AnalysisOrchestrator(db2)
        await orchestrator2.run(analysis_id=analysis.id)
        print(f"Phase 2 done in {time.monotonic() - t1:.2f}s", flush=True)

    # Result check
    print("Checking DB result...", flush=True)
    async for db3 in get_db():
        from models.analysis import MemoryAnalysis

        row = await db3.get(MemoryAnalysis, analysis.id)
        if row is None:
            print("ERROR: analysis row not found", flush=True)
            return

        print(f"status: {row.status}", flush=True)
        print(f"cost_actual_cents: {row.cost_actual_cents}", flush=True)
        print(f"error: {row.error}", flush=True)

        clusters = await db3.execute(
            text(
                "SELECT count(*) FROM memory_analysis_clusters WHERE analysis_id = :id"
            ),
            {"id": str(analysis.id)},
        )
        assignments = await db3.execute(
            text(
                "SELECT count(*) FROM memory_analysis_assignments WHERE analysis_id = :id"
            ),
            {"id": str(analysis.id)},
        )
        print(f"clusters: {clusters.scalar()}", flush=True)
        print(f"assignments: {assignments.scalar()}", flush=True)

        sr = await db3.execute(
            text(
                "SELECT count(*) FROM sleep_reports WHERE source = 'analysis' AND paid_by = 'byok'"
            ),
        )
        print(f"sleep_reports(analysis/byok): {sr.scalar()}", flush=True)

    print(f"\nTotal: {time.monotonic() - t0:.2f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
