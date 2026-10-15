"""Startup reconciliation of the Qdrant dense vector layout (Issue #1990).

Every API worker runs the lifespan, and ``update_collection`` is not
idempotent in cost: N workers that all read the unconverted state send N
updates and restart the optimizer's segment rebuild N times. So the pass
runs under the cross-process :func:`tasks.single_flight.single_flight`
guard (session-level Postgres advisory lock on its own connection, released
explicitly): the first worker converts every collection, the others skip.
A worker that starts later — a rolling restart, a crashed leader — finds
the lock free and runs a pass that sends nothing. It runs as a background
task so a slow or down Qdrant delays nothing at boot (the readiness probe
reports that).
"""

from __future__ import annotations

from db.qdrant import apply_dense_vector_config_to_all
from tasks.single_flight import single_flight
from utils.logger import get_logger

logger = get_logger(__name__)

DENSE_VECTOR_CONFIG_LOCK_KEY = "qdrant_dense_config"


async def run_dense_vector_config_reconcile() -> dict[str, bool]:
    """Reconcile every ``kagura_memories*`` collection, in one worker only.

    Returns ``{collection: changed}`` from
    :func:`db.qdrant.apply_dense_vector_config_to_all`, or an empty dict
    when another worker holds the guard or the pass failed; both are logged
    and never raised — the next boot retries.
    """
    try:
        async with single_flight(DENSE_VECTOR_CONFIG_LOCK_KEY) as acquired:
            if not acquired:
                logger.info(
                    "dense_vector_config_skipped_busy", lock_key=DENSE_VECTOR_CONFIG_LOCK_KEY
                )
                return {}
            results = await apply_dense_vector_config_to_all()
        logger.info(
            "dense_vector_config_reconciled",
            collections=len(results),
            changed=sorted(name for name, changed in results.items() if changed),
        )
        return results
    except Exception as e:  # noqa: BLE001 — best-effort startup pass
        logger.warning("dense_vector_config_apply_failed", error=str(e))
        return {}
