"""Admin endpoint for manually triggering kNN-seed calibration (#406 Phase B).

POST /api/v1/admin/neural/recalibrate?model=<name>&dimensions=<N>

Enqueues a model-global calibration job for the given ``(model_name,
dimensions)`` pair. This is the third of three calibration triggers
(bootstrap / admin-manual / lazy-TTL) defined in #240 C2 — the admin
path exists for operational recovery: e.g. after swapping an embedding
provider, after a corpus-wide re-embed, or when the lazy-TTL signal
hasn't fired yet and the operator wants a fresh threshold immediately.

The actual compute runs in an ``asyncio.create_task`` spawned by
:func:`~tasks.neural_calibration.enqueue_recalibration_dedup`; this
handler always returns 202, with ``accepted=true`` when a new job was
enqueued and ``accepted=false`` when the dedup lock rejects a duplicate
(idempotent — callers can safely retry without hammering the compute).

GET /api/v1/admin/neural/calibrations (#2845)

Reads what the job wrote: the model-global calibration rows with their
percentiles, sample size, age, expiry and the threshold each gives.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.neural import (
    CALIBRATION_KIND_EDGE_GATE,
    CALIBRATION_KIND_KNN_SEED,
    EmbeddingCalibration,
)
from neural.calibration import edge_gate_threshold_of, knn_seed_threshold_of
from neural.config import NeuralMemoryConfig
from tasks.neural_calibration import enqueue_recalibration_dedup
from utils import get_user_id
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/neural", tags=["admin-neural"])


# Allow-list of supported embedding model names. Open-list by design —
# adding a new provider means shipping the embedding service, not
# touching this router — but we reject obviously invalid input (empty
# string, very long string, path-looking values) at the edge so the
# downstream dedup-key construction and logs stay clean. The actual
# model-dimension validity is enforced by the calibration task itself
# (it aborts when no context uses the pair).
_MODEL_NAME_MAX_LEN = 100


class RecalibrateResponse(BaseModel):
    """202 response body for the admin recalibrate endpoint.

    Attributes:
        accepted: ``True`` when the dedup lock was acquired and a
            calibration task was spawned. ``False`` when a prior job is
            still in-flight for the same ``(model, dimensions)`` —
            idempotent; no error raised.
        model_name: Echo of the requested model.
        dimensions: Echo of the requested dimensionality.
    """

    accepted: bool
    model_name: str
    dimensions: int


@router.post(
    "/recalibrate",
    response_model=RecalibrateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def recalibrate(
    current_user: AdminUser,
    model: str = Query(..., min_length=1, max_length=_MODEL_NAME_MAX_LEN),
    dimensions: int = Query(..., ge=1, le=65536),
) -> RecalibrateResponse:
    """Enqueue a deduped model-global calibration job.

    The ``AdminUser`` dependency rejects non-admins with 403 before this
    handler runs. ``model`` is length-bounded and ``dimensions`` is
    range-bounded by FastAPI validation. No rate limiting is applied at
    this layer — the Redis dedup lock is the single source of truth for
    "one job per (model, dims) in flight at a time", and admins can
    self-serve retries without hammering the compute.
    """
    # Defensive: reject obviously-malformed model names (path separators,
    # whitespace) even if they pass length validation. FastAPI's Query
    # validator doesn't enforce a regex because we want to stay open to
    # new providers without churning this file.
    if "/" in model or "\\" in model or any(c.isspace() for c in model):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_model_name",
        )

    accepted = await enqueue_recalibration_dedup(
        model_name=model,
        dimensions=dimensions,
        context_id=None,
    )
    logger.info(
        "admin_recalibrate_requested",
        admin_email=current_user.get("email"),
        model=model,
        dimensions=dimensions,
        accepted=accepted,
    )
    return RecalibrateResponse(
        accepted=accepted,
        model_name=model,
        dimensions=dimensions,
    )


# ============================================================================
# Calibration read (#2845)
# ============================================================================

# The two distributions a model-global row can summarize; the threshold each
# gives is computed by the function the runtime resolver uses.
_THRESHOLD_OF = {
    CALIBRATION_KIND_KNN_SEED: knn_seed_threshold_of,
    CALIBRATION_KIND_EDGE_GATE: edge_gate_threshold_of,
}


class EmbeddingCalibrationItem(TZAwareBaseModel):
    """One model-global calibration row with what it gives at runtime.

    Attributes:
        model_name: Embedding model the row was measured for.
        dimensions: Vector dimensionality of that model.
        kind: ``knn_seed`` (top-k neighbor distribution, seed edges on
            ingest) or ``edge_gate`` (random-pair distribution, the
            co-activation edge gate).
        p25-p99: The stored percentiles of the cosine distribution.
        sample_size: Observations the percentiles were computed from.
        sampled_at: When the calibration was computed.
        valid_until: When the row expires. An expired row is still served.
        expired: Whether ``valid_until`` has passed.
        threshold: The similarity threshold the row gives with the config of
            the process that answers: ``max(percentile, floor)`` with the
            percentile and floor settings of the row's ``kind``.
    """

    model_config = ConfigDict(extra="forbid")

    model_name: str
    dimensions: int
    kind: Literal["knn_seed", "edge_gate"]
    p25: float
    p50: float
    p75: float
    p90: float
    p95: float
    p99: float
    sample_size: int
    sampled_at: datetime
    valid_until: datetime
    expired: bool
    threshold: float


class EmbeddingCalibrationListResponse(TZAwareBaseModel):
    """Response body of the calibration read.

    Attributes:
        knn_seed_override: The operator setting ``knn_seed_min_similarity``,
            or null. When it is set it is the seeding threshold and the
            ``knn_seed`` rows are not used.
        calibrations: The model-global rows, ordered by ``model_name``,
            ``dimensions``, ``kind``. A pair that was never calibrated has
            no row.
    """

    model_config = ConfigDict(extra="forbid")

    knn_seed_override: float | None
    calibrations: list[EmbeddingCalibrationItem]


@router.get("/calibrations", response_model=EmbeddingCalibrationListResponse)
async def list_calibrations(
    current_user: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> EmbeddingCalibrationListResponse:
    """Read the model-global embedding calibration rows (#2845).

    Read only, no parameters, no paging: at most two rows (``knn_seed`` and
    ``edge_gate``) per model and dimensions. Per-context rows are never
    answered, and the answer holds no row id and no context, workspace,
    memory or user identifier.

    ``threshold`` is computed here from the row and the config of the process
    that answers, with the functions the runtime resolvers use. The route
    does not call the resolvers: a read starts no recalibration, also for an
    expired row.
    """
    config = await NeuralMemoryConfig.from_db(db)
    rows = (
        await db.execute(
            select(
                EmbeddingCalibration.model_name,
                EmbeddingCalibration.dimensions,
                EmbeddingCalibration.kind,
                EmbeddingCalibration.p25,
                EmbeddingCalibration.p50,
                EmbeddingCalibration.p75,
                EmbeddingCalibration.p90,
                EmbeddingCalibration.p95,
                EmbeddingCalibration.p99,
                EmbeddingCalibration.sample_size,
                EmbeddingCalibration.sampled_at,
                EmbeddingCalibration.valid_until,
            )
            .where(
                EmbeddingCalibration.context_id.is_(None),
                EmbeddingCalibration.kind.in_(tuple(_THRESHOLD_OF)),
            )
            .order_by(
                EmbeddingCalibration.model_name,
                EmbeddingCalibration.dimensions,
                EmbeddingCalibration.kind,
            )
        )
    ).all()

    calibrations: list[EmbeddingCalibrationItem] = []
    for row in rows:
        # A detached copy of the selected columns, never added to the session:
        # it carries the model's own percentile() and is_expired().
        calibration = EmbeddingCalibration(
            model_name=row.model_name,
            dimensions=row.dimensions,
            kind=row.kind,
            p25=row.p25,
            p50=row.p50,
            p75=row.p75,
            p90=row.p90,
            p95=row.p95,
            p99=row.p99,
            sample_size=row.sample_size,
            sampled_at=row.sampled_at,
            valid_until=row.valid_until,
        )
        calibrations.append(
            EmbeddingCalibrationItem(
                model_name=row.model_name,
                dimensions=row.dimensions,
                kind=row.kind,
                p25=row.p25,
                p50=row.p50,
                p75=row.p75,
                p90=row.p90,
                p95=row.p95,
                p99=row.p99,
                sample_size=row.sample_size,
                sampled_at=row.sampled_at,
                valid_until=row.valid_until,
                expired=calibration.is_expired(),
                threshold=_THRESHOLD_OF[row.kind](calibration, config),
            )
        )

    logger.info(
        "neural_calibrations_listed",
        count=len(calibrations),
        admin=get_user_id(current_user),
    )
    return EmbeddingCalibrationListResponse(
        knn_seed_override=config.knn_seed_min_similarity,
        calibrations=calibrations,
    )
