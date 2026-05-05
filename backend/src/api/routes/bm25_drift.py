"""BM25 IDF drift admin API.

Issue #343: read-only list/detail of bm25_idf_drift_log rows + manual
trigger for dev/staging + plaintext term reveal endpoint (#377).
All endpoints are admin-only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from pydantic import Field as PydanticField
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser, require_admin
from config.settings import get_settings
from db.base import get_db
from db.qdrant import QDRANT_TOKEN_PAYLOAD_FIELDS, scroll_context_points
from db.redis import increment_counter
from models.api_base import TZAwareBaseModel
from models.auth import AuditLog, Context
from models.bm25_drift import Bm25IdfDriftLog
from services.bm25_drift.orchestrator import Bm25DriftOrchestrator
from services.bm25_drift.psi_calculator import PsiStatus
from utils.exceptions import RedisError
from utils.logger import get_logger
from utils.sparse_vector import hash_token
from utils.tokenizer import tokenize_for_search

logger = get_logger(__name__)

router = APIRouter(
    prefix="/admin/bm25-drift",
    tags=["bm25-drift"],
)


# ============================================================================
# Schemas
# ============================================================================


class Bm25DriftSummary(TZAwareBaseModel):
    """Drift log row, list-view shape (no top_divergent_terms)."""

    id: int
    context_id: UUID
    context_name: str | None = None
    measured_at: datetime
    psi: float | None
    psi_status: str
    m_memory_points: int
    r_resource_points: int
    num_terms: int


class Bm25DriftDetail(Bm25DriftSummary):
    """Drift log row, detail-view shape (includes top_divergent_terms)."""

    context_deleted: bool = False
    top_divergent_terms: list[dict[str, Any]] | None = None


class Bm25DriftListResponse(BaseModel):
    rows: list[Bm25DriftSummary]
    total: int
    limit: int
    offset: int


class Bm25DriftDetailResponse(BaseModel):
    row: Bm25DriftDetail


class Bm25DriftRunRequest(BaseModel):
    """Body for POST /admin/bm25-drift/run.

    Attributes:
        context_id: Target context. When null, runs against every active
            context (matches the cron behaviour). For dev/staging
            verification — production cron remains disabled until v0.14.0.
    """

    context_id: UUID | None = None


class Bm25DriftRunResponse(BaseModel):
    scheduled_context_count: int


class Bm25DriftRevealRequest(BaseModel):
    """Body for POST /admin/bm25-drift/{row_id}/reveal-terms."""

    reason: str = PydanticField(
        ...,
        min_length=10,
        description="Justification for viewing plaintext terms (audit-logged)",
    )


class ResolvedTermEntry(BaseModel):
    """Single entry from top_divergent_terms with resolved token."""

    index: int
    df_memory: float
    df_global: float
    idf_memory: float
    idf_global: float
    delta: float
    token: str | None = None


class Bm25DriftRevealResponse(Bm25DriftDetail):
    """Drift detail + resolved plaintext terms."""

    resolved_terms: list[ResolvedTermEntry]


# Single source of truth for the status enum: derive from the Literal in
# psi_calculator. The DB CHECK constraint and ORM model already pin the
# same set; if a status is added there, it shows up here automatically.
_VALID_STATUSES = set(PsiStatus.__args__)


# ============================================================================
# Endpoints
# ============================================================================


@router.get("", response_model=Bm25DriftListResponse)
async def list_drift_logs(
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - access guard only
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    context_id: UUID | None = Query(None),
) -> Bm25DriftListResponse:
    """List BM25 IDF drift log rows. Admin-only.

    Sorted by measured_at DESC. Filterable by psi_status and context_id.
    """
    if status_filter is not None and status_filter not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {sorted(_VALID_STATUSES)}",
        )

    conditions = []
    if status_filter is not None:
        conditions.append(Bm25IdfDriftLog.psi_status == status_filter)
    if context_id is not None:
        conditions.append(Bm25IdfDriftLog.context_id == context_id)

    count_stmt = select(func.count()).select_from(Bm25IdfDriftLog)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = select(Bm25IdfDriftLog)
    if conditions:
        stmt = stmt.where(*conditions)
    rows_result = await db.execute(
        stmt.order_by(Bm25IdfDriftLog.measured_at.desc()).limit(limit).offset(offset)
    )
    rows = list(rows_result.scalars().all())

    # Batch-resolve context display names to avoid N+1.
    ctx_ids = {r.context_id for r in rows}
    ctx_map: dict[UUID, str | None] = {}
    if ctx_ids:
        ctx_result = await db.execute(
            select(Context.id, Context.name, Context.display_name, Context.deleted_at).where(
                Context.id.in_(ctx_ids)
            )
        )
        for cid, cname, cdisplay, cdeleted in ctx_result.all():
            ctx_map[cid] = None if cdeleted is not None else (cdisplay or cname)

    for r in rows:
        r.context_name = ctx_map.get(r.context_id)

    return Bm25DriftListResponse(
        rows=[Bm25DriftSummary.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{row_id}", response_model=Bm25DriftDetailResponse)
async def get_drift_log_detail(
    row_id: int,
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - access guard only
    db: AsyncSession = Depends(get_db),
) -> Bm25DriftDetailResponse:
    """Detail view including top_divergent_terms. Admin-only.

    Emits an audit log event on every read so historical access is
    reconstructable from logs alone.
    """
    row_result = await db.execute(select(Bm25IdfDriftLog).where(Bm25IdfDriftLog.id == row_id))
    row = row_result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Drift log {row_id} not found.",
        )

    await _apply_context_name(db, row)

    # Audit log — terms count only, never term content.
    logger.info(
        "drift_detail_accessed",
        row_id=row_id,
        context_id=str(row.context_id),
        psi=float(row.psi) if row.psi is not None else None,
        status=row.psi_status,
        num_terms=row.num_terms,
    )

    return Bm25DriftDetailResponse(
        row=Bm25DriftDetail.model_validate(row, from_attributes=True),
    )


@router.post(
    "/run",
    response_model=Bm25DriftRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_drift_now(
    body: Bm25DriftRunRequest,
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - access guard only
    db: AsyncSession = Depends(get_db),
) -> Bm25DriftRunResponse:
    """Manually trigger a drift measurement. Admin-only.

    For dev/staging verification while the production cron is disabled
    (v0.12.1 ships infrastructure-only; production enablement = v0.14.0).
    Each requested context is enqueued as an `asyncio.create_task` and
    runs against an independent DB session.
    """
    if body.context_id is not None:
        target_context_ids: list[UUID] = [body.context_id]
    else:
        from models.memory import Memory

        result = await db.execute(
            select(Memory.context_id)
            .distinct()
            .where(Memory.deleted_at.is_(None))
            .where(Memory.context_id.isnot(None))
        )
        target_context_ids = [r[0] for r in result.all() if r[0] is not None]

    # Keep strong references to the spawned tasks until the response
    # is built. asyncio.create_task returns a Task that is GC-eligible
    # if the only reference is dropped before the loop schedules it
    # (cpython#90887). The list survives until function return, which
    # gives the event loop time to pick the tasks up.
    _spawned: list[asyncio.Task] = [
        asyncio.create_task(_run_drift_job(cid)) for cid in target_context_ids
    ]

    logger.info(
        "drift_manual_trigger_scheduled",
        scheduled=len(_spawned),
    )
    return Bm25DriftRunResponse(scheduled_context_count=len(_spawned))


async def _run_drift_job(context_id: UUID) -> None:
    """Background task: open an independent session and run one drift cycle.

    Mirrors admin_sleep._run_sleep_job — own session per job avoids
    request-session lifetime entanglement. The rollback itself can fail
    (e.g. connection loss); we guard it so the structured error log still
    reaches the operator.
    """
    from db.base import get_db as _get_db

    async for db in _get_db():
        try:
            orchestrator = Bm25DriftOrchestrator(db)
            await orchestrator.run(context_id)
            await db.commit()
        except Exception as e:
            try:
                await db.rollback()
            except Exception as rollback_exc:
                logger.error(
                    "drift_manual_trigger_rollback_failed",
                    context_id=str(context_id),
                    error=str(rollback_exc),
                )
            logger.error(
                "drift_manual_trigger_failed",
                context_id=str(context_id),
                error=str(e),
                exc_info=True,
            )
        return


async def _apply_context_name(db: AsyncSession, row: Bm25IdfDriftLog) -> None:
    """Set synthetic row.context_name + row.context_deleted from the Context table.

    These attributes are not mapped columns on Bm25IdfDriftLog — assigning
    to them populates response shape without dirtying the SQLAlchemy session.
    """
    ctx_result = await db.execute(
        select(Context.name, Context.display_name, Context.deleted_at).where(
            Context.id == row.context_id
        )
    )
    ctx_row = ctx_result.first()
    if ctx_row is None:
        row.context_name = None
        row.context_deleted = True
        return
    cname, cdisplay, cdeleted = ctx_row
    if cdeleted is not None:
        row.context_name = None
        row.context_deleted = True
    else:
        row.context_name = cdisplay or cname
        row.context_deleted = False


def _resolve_payload_tokens(
    points: list,
    needed: set[int],
    found: dict[int, str] | None = None,
) -> dict[int, str]:
    """Accumulate {hash: token} for indices in `needed`, scanning a single page with early exit.

    Reads pre-tokenized fields populated by memory_service. Falls back to
    re-tokenizing summary text only for legacy points written before the
    token payload fields existed. Stops as soon as every requested index
    has been resolved. The optional `found` argument lets the caller share
    one accumulator across multiple scroll pages (the streaming-page path
    in reveal_drift_terms relies on this so memory stays bounded).
    """
    if found is None:
        found = {}

    def _scan(tokens_str: str) -> bool:
        for tok in tokens_str.split():
            h = hash_token(tok)
            if h in needed and h not in found:
                found[h] = tok
                if len(found) == len(needed):
                    return True
        return False

    for point in points:
        if len(found) == len(needed):
            return found
        payload = point.payload or {}
        token_strs = [s for s in (payload.get(f) for f in QDRANT_TOKEN_PAYLOAD_FIELDS) if s]
        if token_strs:
            for s in token_strs:
                if _scan(s):
                    return found
            continue
        for text_field in ("summary", "context_summary"):
            text = payload.get(text_field)
            if text and _scan(tokenize_for_search(text)):
                return found
    return found


async def _write_reveal_audit(
    db: AsyncSession,
    admin: dict,
    user_sub: str,
    row_id: int,
    *,
    outcome: str,
    reason: str,
    context_id: str | None,
    num_terms_resolved: int,
    num_terms_total: int,
) -> None:
    """Persist an AuditLog row for any reveal-terms attempt (success or denied).

    Called from every terminal branch that has an authenticated identity so
    security teams have a reconstructable access trail (success / row_not_found
    / rate_limited). Term content is never logged.
    """
    db.add(
        AuditLog(
            user_email=admin.get("email", "unknown"),
            user_id=user_sub,
            action="bm25_drift_reveal_terms",
            resource=f"bm25_drift_log:{row_id}",
            user_metadata={
                "outcome": outcome,
                "context_id": context_id,
                "reason": reason,
                "num_terms_resolved": num_terms_resolved,
                "num_terms_total": num_terms_total,
            },
        )
    )
    await db.commit()


@router.post("/{row_id}/reveal-terms", response_model=Bm25DriftRevealResponse)
async def reveal_drift_terms(
    row_id: int,
    body: Bm25DriftRevealRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> Bm25DriftRevealResponse:
    """Resolve mmh3-hashed term indices in top_divergent_terms to plaintext.

    Reads pre-tokenized fields from the Qdrant payload (see
    QDRANT_TOKEN_PAYLOAD_FIELDS) populated by memory_service, recomputes
    mmh3 hashes via utils.sparse_vector.hash_token, and matches against
    the stored indices. Falls back to re-tokenizing summary text only
    for legacy points written before the token payload fields existed.
    Streams Qdrant pages and breaks as soon as every requested hash is
    resolved, so memory stays bounded for million-point contexts.
    Rate-limited per user. Every terminal branch (success / 404 /
    rate-limited) writes an AuditLog row; term content is never logged.
    """
    settings = get_settings()

    user_sub = admin.get("sub") or admin.get("user_id")
    if not user_sub:
        # require_admin guarantees an authenticated dict; missing both keys
        # is an upstream bug, not a routable identity.
        raise HTTPException(status_code=500, detail="Admin identity missing sub/user_id.")

    row_result = await db.execute(select(Bm25IdfDriftLog).where(Bm25IdfDriftLog.id == row_id))
    row = row_result.scalar_one_or_none()
    if row is None:
        await _write_reveal_audit(
            db,
            admin,
            user_sub,
            row_id,
            outcome="row_not_found",
            reason=body.reason,
            context_id=None,
            num_terms_resolved=0,
            num_terms_total=0,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Drift log {row_id} not found.",
        )

    try:
        count = await increment_counter(f"rate:bm25_reveal:{user_sub}", ttl=3600)
    except RedisError:
        # Fail-closed: rate limit is part of the threat model for this admin
        # endpoint, so treat Redis unavailability as a deny. Audit the
        # attempt with outcome=rate_limit_unavailable so security teams can
        # distinguish "user was rate-limited" from "rate-limit infra broke".
        await _write_reveal_audit(
            db,
            admin,
            user_sub,
            row_id,
            outcome="rate_limit_unavailable",
            reason=body.reason,
            context_id=str(row.context_id),
            num_terms_resolved=0,
            num_terms_total=0,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Rate limit service unavailable.",
        ) from None
    if count > settings.bm25_reveal_rate_limit_per_hour:
        await _write_reveal_audit(
            db,
            admin,
            user_sub,
            row_id,
            outcome="rate_limited",
            reason=body.reason,
            context_id=str(row.context_id),
            num_terms_resolved=0,
            num_terms_total=0,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"BM25 reveal rate limit exceeded "
                f"({settings.bm25_reveal_rate_limit_per_hour}/hour). "
                f"Try again later."
            ),
        )

    top_terms: list[dict[str, Any]] = row.top_divergent_terms or []
    needed: set[int] = {entry["index"] for entry in top_terms}
    hash_to_token: dict[int, str] = {}
    if needed:
        async for page in scroll_context_points(
            str(row.context_id),
            with_payload=[*QDRANT_TOKEN_PAYLOAD_FIELDS, "summary", "context_summary"],
        ):
            _resolve_payload_tokens(page, needed, hash_to_token)
            if len(hash_to_token) == len(needed):
                break

    resolved_terms = [
        ResolvedTermEntry(**entry, token=hash_to_token.get(entry["index"])) for entry in top_terms
    ]
    resolved_count = sum(1 for t in resolved_terms if t.token is not None)

    await _apply_context_name(db, row)

    await _write_reveal_audit(
        db,
        admin,
        user_sub,
        row_id,
        outcome="success",
        reason=body.reason,
        context_id=str(row.context_id),
        num_terms_resolved=resolved_count,
        num_terms_total=len(top_terms),
    )

    logger.info(
        "bm25_drift_terms_revealed",
        row_id=row_id,
        context_id=str(row.context_id),
        reason=body.reason,
        num_terms_resolved=resolved_count,
        num_terms_total=len(top_terms),
    )

    detail = Bm25DriftDetail.model_validate(row, from_attributes=True)
    return Bm25DriftRevealResponse(
        **detail.model_dump(),
        resolved_terms=resolved_terms,
    )
