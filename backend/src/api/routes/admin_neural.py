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
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from auth.dependencies import AdminUser
from tasks.neural_calibration import enqueue_recalibration_dedup
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
