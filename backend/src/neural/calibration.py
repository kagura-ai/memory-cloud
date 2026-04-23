"""Runtime resolution of the kNN-seed similarity threshold (#406 Phase B).

Implements the D4 fallback chain from issue #240:

    Step 1  Operator-set ``knn_seed_min_similarity`` non-null → raw override,
            skip calibration entirely (D6 preserves operator intent).
    Step 2  Model-global calibration row (``context_id IS NULL``) for the
            current ``(model, dimensions)`` → ``max(percentile(p), floor)``
            where p = ``knn_seed_min_percentile`` (default 90.0) and floor =
            ``knn_seed_min_similarity_floor`` (default 0.3).
    Step 3  No calibration row → return ``None`` so the caller disables kNN
            seeding for this call. The calibration job will bootstrap later
            (triggered by remember() when the context crosses the D3 gate).

Per-context calibration (D5 v2 / C1) is **schema-allowed but runtime-dead**
in v1: the lookup below hard-codes ``context_id=None``. The v2 follow-up
flips Step 2 to try per-context first, then fall through to model-global.

Lazy TTL trigger (C2, third trigger): if Step 2's row is past ``valid_until``,
enqueue a background recalibration task (deduped by ``(model, dims,
context_id)``) and serve the stale value anyway — fail-open on stale
calibration, because a fresh threshold matters less than not stalling
every remember() call while we wait for Qdrant to re-measure 10k points.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from models.neural import EmbeddingCalibration
from utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from neural.config import NeuralMemoryConfig

logger = get_logger(__name__)


async def resolve_knn_threshold(
    db: AsyncSession,
    config: NeuralMemoryConfig,
    model_name: str,
    dimensions: int,
    context_id: UUID | None = None,  # noqa: ARG001 — TODO(v2): per-context lookup
) -> float | None:
    """Resolve the runtime kNN similarity threshold for ``(model, dimensions)``.

    Returns the similarity cosine at or above which a Qdrant neighbor
    should be accepted as a ``semantic_similarity`` seed edge. Returns
    ``None`` iff step 3 of the D4 fallback chain fires — kNN seeding
    should be disabled for this call (the calibration path has not been
    bootstrapped yet).

    Args:
        db: AsyncSession for the calibration table lookup.
        config: Resolved ``NeuralMemoryConfig`` (already loaded from DB).
        model_name: Embedding model name (e.g. ``text-embedding-3-small``).
        dimensions: Vector dimensionality (e.g. 512, 4096).
        context_id: Reserved for D5 v2 per-context calibration. Currently
            ignored (see module docstring).

    Returns:
        Similarity threshold in ``[0.0, 1.0]``, or ``None`` to disable.
    """
    # Step 1: operator override (D6). None means "use calibration".
    if config.knn_seed_min_similarity is not None:
        return config.knn_seed_min_similarity

    # Step 2: model-global calibration (context_id IS NULL).
    # TODO(v2): try context_id first, fall through to NULL on miss.
    stmt = (
        select(EmbeddingCalibration)
        .where(
            EmbeddingCalibration.model_name == model_name,
            EmbeddingCalibration.dimensions == dimensions,
            EmbeddingCalibration.context_id.is_(None),
        )
        .limit(1)
    )
    calibration = (await db.execute(stmt)).scalar_one_or_none()

    if calibration is not None:
        if calibration.is_expired():
            # Lazy TTL trigger — see tasks/neural_calibration.py for dedup.
            # Serve the stale value while the refresh runs in the background.
            await _enqueue_lazy_recalibration(model_name, dimensions, context_id=None)
        percentile_value = calibration.percentile(config.knn_seed_min_percentile)
        return max(percentile_value, config.knn_seed_min_similarity_floor)

    # Step 3: no calibration yet → disable seeding. Bootstrap trigger in the
    # remember() path will populate the row once the context crosses D3.
    logger.warning(
        "knn_seed_disabled_no_calibration",
        model=model_name,
        dimensions=dimensions,
        hint=(
            "Calibration row missing for this (model, dimensions). "
            "Bootstrap fires when the context has >=200 memories or "
            ">=10k top-k observations. Admin-manual recalibrate is "
            "available via /api/admin/neural/recalibrate."
        ),
    )
    return None


async def _enqueue_lazy_recalibration(
    model_name: str,
    dimensions: int,
    context_id: UUID | None,
) -> None:
    """Lazy-TTL recalibration enqueue with dedup.

    Deliberately imported lazily so ``neural/calibration.py`` can be used in
    unit tests that don't need the task/Redis machinery. When the trigger
    module isn't available (tests, thin environments), log and continue —
    the next scheduled recalibration will catch up.
    """
    try:
        # Imported lazily (a) to avoid a circular dependency between
        # ``neural`` and ``tasks`` packages and (b) so ``resolve_knn_threshold``
        # remains unit-testable without the full tasks/Redis machinery.
        from tasks.neural_calibration import enqueue_recalibration_dedup  # noqa: PLC0415
    except ImportError:
        logger.debug(
            "knn_seed_lazy_ttl_enqueue_unavailable",
            model=model_name,
            dimensions=dimensions,
        )
        return
    await enqueue_recalibration_dedup(model_name, dimensions, context_id)
