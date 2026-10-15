"""Startup reconciliation of the Qdrant dense vector layout (Issue #1990).

Every API worker runs the lifespan, and ``update_collection`` is not
idempotent in cost: N workers that all read the unconverted state send N
updates and restart the optimizer's segment rebuild N times. So the pass
runs under one Postgres advisory lock, transaction-scoped and blocking —
the first worker converts, the ones queued behind it then read the new
state and send nothing. It runs as a background task so a slow or down
Qdrant delays nothing at boot (the readiness probe reports that).
"""

from __future__ import annotations

from sqlalchemy import text

from db.base import get_db
from db.qdrant import apply_dense_vector_config_to_all
from utils.logger import get_logger

logger = get_logger(__name__)

DENSE_VECTOR_CONFIG_LOCK_KEY = "qdrant_dense_config"
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


async def run_dense_vector_config_reconcile() -> dict[str, bool]:
    """Reconcile every ``kagura_memories*`` collection, one worker at a time.

    Returns ``{collection: changed}`` from
    :func:`db.qdrant.apply_dense_vector_config_to_all`, or an empty dict
    when the lock could not be taken or the pass failed; both are logged
    and never raised — the next boot retries.
    """
    try:
        async for db in get_db():
            await db.execute(_LOCK_SQL, {"key": DENSE_VECTOR_CONFIG_LOCK_KEY})
            try:
                results = await apply_dense_vector_config_to_all()
            finally:
                # Releases the transaction-scoped lock for the next worker.
                await db.rollback()
            logger.info(
                "dense_vector_config_reconciled",
                collections=len(results),
                changed=sorted(name for name, changed in results.items() if changed),
            )
            return results
    except Exception as e:  # noqa: BLE001 — best-effort startup pass
        logger.warning("dense_vector_config_apply_failed", error=str(e))
    return {}
