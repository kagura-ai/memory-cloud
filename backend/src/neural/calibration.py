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

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from models.neural import (
    CALIBRATION_KIND_EDGE_GATE,
    CALIBRATION_KIND_KNN_SEED,
    EmbeddingCalibration,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from neural.config import NeuralMemoryConfig

logger = get_logger(__name__)

# Strong references to the lazy-TTL enqueue coroutines so Python does not GC
# them mid-flight. ``asyncio.create_task`` returns a Task that the event
# loop holds only a weak reference to — without an explicit strong ref the
# enqueue coroutine can vanish before it reaches Redis. Mirrors the pattern
# in ``tasks.neural_calibration._IN_FLIGHT_TASKS``. (Copilot review PR #420
# loop 5, mirror of loop 1's fix for the task-side set.)
_LAZY_TTL_TASKS: set[asyncio.Task] = set()


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
    # Filter kind=knn_seed (#982): once edge_gate rows share the
    # (model, dims, NULL) space, an unfiltered limit(1) could return the
    # wrong distribution non-deterministically.
    calibration = await _fetch_global_calibration(
        db, model_name, dimensions, CALIBRATION_KIND_KNN_SEED
    )

    if calibration is not None:
        if calibration.is_expired():
            # Lazy TTL trigger — see tasks/neural_calibration.py for dedup.
            # Fire-and-forget so ``remember()`` does NOT wait on the Redis
            # round-trip (or its timeout) while we serve the stale value.
            # The background task path ``tasks.neural_calibration`` owns
            # its own error handling and strong-ref retention; here we
            # only need to spawn it. (Copilot review PR #420 loop 3.)
            try:
                task = asyncio.create_task(
                    _enqueue_lazy_recalibration(model_name, dimensions, context_id=None)
                )
                _LAZY_TTL_TASKS.add(task)
                task.add_done_callback(_LAZY_TTL_TASKS.discard)
            except RuntimeError:
                # ``asyncio.create_task`` raises if no event loop is running
                # (e.g. the caller is driving via ``asyncio.run`` in a test
                # teardown). In that narrow case the expired value is still
                # served; the next scheduled recalibration will catch up.
                logger.debug(
                    "knn_seed_lazy_ttl_enqueue_no_loop",
                    model=model_name,
                    dimensions=dimensions,
                )
        percentile_value = calibration.percentile(config.knn_seed_min_percentile)
        return max(percentile_value, config.knn_seed_min_similarity_floor)

    # Step 3: no calibration yet → disable seeding. Bootstrap trigger in the
    # remember() path will populate the row once the context crosses D3.
    # Logged at debug level so new contexts (or freshly migrated deployments)
    # don't emit a WARNING on every ``remember()`` call until the bootstrap
    # gate is reached — that would create alert fatigue at no operational
    # benefit. Operators can observe the disabled state via this debug
    # event and the absence of an ``embedding_calibrations`` row for the
    # (model, dimensions) pair.
    # (Copilot review PR #420 loop 2 & 6.)
    logger.debug(
        "knn_seed_disabled_no_calibration",
        model=model_name,
        dimensions=dimensions,
    )
    return None


async def _fetch_global_calibration(
    db: AsyncSession,
    model_name: str,
    dimensions: int,
    kind: str,
) -> EmbeddingCalibration | None:
    """Fetch the model-global (``context_id IS NULL``) calibration row.

    Shared by ``resolve_knn_threshold`` and ``resolve_edge_threshold`` so the
    select boilerplate and the ``kind`` filter live in one place (the two
    callers differ only in which ``kind`` they request and how they treat a
    miss). TODO(v2): try a per-context row first, fall through to NULL.
    """
    stmt = (
        select(EmbeddingCalibration)
        .where(
            EmbeddingCalibration.model_name == model_name,
            EmbeddingCalibration.dimensions == dimensions,
            EmbeddingCalibration.context_id.is_(None),
            EmbeddingCalibration.kind == kind,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def resolve_edge_threshold(
    db: AsyncSession,
    config: NeuralMemoryConfig,
    model_name: str,
    dimensions: int,
    context_id: UUID | None = None,  # noqa: ARG001 — TODO(v2): per-context lookup
) -> float:
    """Resolve the runtime semantic-gate threshold for edge formation (#982).

    The co-activation tracker and Hebbian learner gate pair edges on cosine
    similarity (Issue #118, anti-noise). The #969 compounding experiment found
    the absolute 0.5 default rejects genuine cross-topic pairs (cosines
    0.37-0.40 under text-embedding-3-small), so this resolves a per-(model,
    dimensions) threshold from the RANDOM-PAIR cosine distribution instead.

    Unlike ``resolve_knn_threshold`` (which returns ``None`` to DISABLE seeding
    when uncalibrated), the edge gate must ALWAYS stay active — so the fallback
    is the absolute ``config.min_similarity_for_edge``, never ``None``.

    Step 1  ``edge_gate`` calibration row for ``(model, dimensions)``
            (``context_id IS NULL``) → ``max(percentile(p), floor)`` where
            ``p = config.min_similarity_for_edge_percentile`` and
            ``floor = config.min_similarity_for_edge_floor``. An expired row is
            still served (fail-open) — a stale threshold beats stalling recall.
            NOTE: unlike the knn-seed path, there is no lazy-TTL enqueue here
            yet because the edge_gate measurement + recalibration job is not
            wired (the random-pair distribution writer lands in #982 Increment
            5). Until then an expired edge_gate row is served until manually
            recalibrated. TODO(#982): add the edge_gate recalibration trigger.
    Step 2  no row → ``config.min_similarity_for_edge`` (pre-#982 behavior).

    Args:
        db: AsyncSession for the calibration table lookup.
        config: Resolved ``NeuralMemoryConfig`` (already loaded from DB).
        model_name: Embedding model name (e.g. ``text-embedding-3-small``).
        dimensions: Vector dimensionality (e.g. 512, 4096).
        context_id: Reserved for D5 v2 per-context calibration. Currently
            ignored (model-global lookup only).

    Returns:
        Similarity threshold in ``[0.0, 1.0]``. Always a float (the gate is
        never disabled).
    """
    calibration = await _fetch_global_calibration(
        db, model_name, dimensions, CALIBRATION_KIND_EDGE_GATE
    )

    if calibration is not None:
        percentile_value = calibration.percentile(config.min_similarity_for_edge_percentile)
        return max(percentile_value, config.min_similarity_for_edge_floor)

    # Step 2: uncalibrated → absolute fallback keeps the anti-noise gate active.
    return config.min_similarity_for_edge


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
        # Deferred import so ``resolve_knn_threshold`` remains unit-testable
        # without the full tasks / Redis machinery pulled in transitively
        # (e.g. ``tests/neural/test_calibration.py`` can exercise this
        # module against a stubbed AsyncSession). The dependency graph is
        # acyclic — ``tasks.neural_calibration`` imports from
        # ``neural.config`` but not from this module — so the lazy import
        # is about test ergonomics, not breaking a cycle.
        from tasks.neural_calibration import enqueue_recalibration_dedup  # noqa: PLC0415
    except ImportError:
        logger.debug(
            "knn_seed_lazy_ttl_enqueue_unavailable",
            model=model_name,
            dimensions=dimensions,
        )
        return
    await enqueue_recalibration_dedup(model_name, dimensions, context_id)
