"""Cost aggregation API routes (Issue #472).

Two endpoints share one ``CostAggregationService`` instance:

- ``GET /api/v1/admin/cost-aggregation`` — admin-only, sees every
  workspace.
- ``GET /api/v1/workspaces/{workspace_id}/cost-aggregation`` —
  workspace owner / admin scoped via
  ``PermissionService.check_workspace_admin``. The path-bound
  ``workspace_id`` is the only allowed scope.

Both endpoints accept the same period / from / to / user_id / source /
paid_by filters; the workspace-scoped one omits ``workspace_id`` from
the query string because it comes from the path.

The split (instead of a single auth-scoped route) keeps:

- ``/admin/`` prefix semantics honest — only system admins can hit it.
- Workspace path consistency with the future B2B billing endpoint
  (``/api/v1/workspaces/{id}/invoices`` and friends).
- Distinct OpenAPI tags (admin vs workspaces) so the docs render the
  two endpoints in separate, semantically-named groups rather than a
  shared "cost-aggregation" bucket.
"""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser, get_current_user
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.sleep import SLEEP_REPORT_PAID_BY_VALUES, SLEEP_REPORT_SOURCES
from services.cost_aggregation_service import (
    MAX_LOOKBACK_DAYS,
    VALID_PERIODS,
    CostAggregationRow,
    CostAggregationService,
    window_exceeds_cap,
)
from services.permission_service import PermissionService

# No router-level tags so each route picks its own group ("admin" vs
# "workspaces") in the OpenAPI docs without duplicating "cost-aggregation".
# Logging happens inside CostAggregationService, not here — the route
# layer just shapes inputs/outputs.
router = APIRouter()


# ============================================================================
# Schemas
# ============================================================================


class CostBreakdownByModelResponse(TZAwareBaseModel):
    """Per-model cost split inside a CostAggregationRowResponse.

    ``cost_usd`` / ``cost_usd_byok`` are nullable: ``null`` means
    "cost unknown" (some contributing usage row had no resolved
    pricing — e.g. a model with no row in ``llm_pricing`` at the
    run's ``started_at``). Distinguishes "no pricing snapshot" from
    "genuinely $0", same semantics as ``LLMPricingService.lookup()``
    returning ``None`` on a miss.
    """

    model: str | None
    calls: int
    cost_usd: float | None
    cost_usd_byok: float | None


class CostBreakdownBySourceResponse(TZAwareBaseModel):
    """Per-source cost split inside a CostAggregationRowResponse.

    ``cost_usd`` / ``cost_usd_byok`` are nullable for the same
    reason documented on ``CostBreakdownByModelResponse``.
    """

    source: str
    calls: int
    cost_usd: float | None
    cost_usd_byok: float | None


class CostAggregationRowResponse(TZAwareBaseModel):
    """One (period × workspace × user) row in the aggregation response.

    ``cost_usd`` is the platform-billed total (used for B2B invoicing);
    ``cost_usd_byok`` is the workspace's BYOK observability total
    (informational only — Kagura does not bill it). The split is
    enforced at the SQL level so it cannot accidentally re-merge.

    Both cost fields are nullable: ``null`` means "cost unknown" (some
    contributing usage row had no resolved pricing). The UI should
    render NULL as "—" rather than "$0.00" to make the distinction
    visible to operators.
    """

    period_start: date
    workspace_id: UUID | None
    user_id: str
    calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached_in: int
    tokens_cache_write: int
    embedding_tokens: int
    cost_usd: float | None
    cost_usd_byok: float | None
    cost_breakdown_by_model: list[CostBreakdownByModelResponse]
    cost_breakdown_by_source: list[CostBreakdownBySourceResponse]


class CostAggregationResponse(TZAwareBaseModel):
    """Wrapper around the row list — keeps room for a future cursor /
    summary block without breaking the response shape."""

    rows: list[CostAggregationRowResponse]


# ============================================================================
# Shared parameter parsing
# ============================================================================


def _parse_window(
    period: str,
    from_: date,
    to: date,
) -> tuple[str, datetime, datetime]:
    """Validate period + materialize the half-open [start, end) window.

    The query string accepts ISO dates (``2026-04-01``); the SQL needs
    naive UTC datetimes so the comparison column-type matches
    ``sleep_reports.started_at`` (TIMESTAMP WITHOUT TIME ZONE, UTC by
    convention). ``end`` is the day AFTER the caller's ``to`` to keep the
    range half-open and avoid the "midnight is included or not?" trap.
    """
    if period not in VALID_PERIODS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid period {period!r}; must be one of {list(VALID_PERIODS)}",
        )
    if from_ > to:
        raise HTTPException(
            status_code=400,
            detail=f"'from' ({from_.isoformat()}) must be <= 'to' ({to.isoformat()})",
        )

    start_dt = datetime.combine(from_, time.min)
    # Half-open: end-of-window is the start of the day AFTER ``to``.
    end_date = date.fromordinal(to.toordinal() + 1)
    end_dt = datetime.combine(end_date, time.min)
    # Defense-in-depth window cap (#528): reject before the service so a
    # non-UI caller (curl / SDK / MCP) can't scan years of sleep_reports.
    # ``end_dt - start_dt`` is the inclusive day count (half-open end = to+1),
    # so ``window_exceeds_cap`` mirrors the frontend's ``days > MAX_LOOKBACK_DAYS``
    # exactly — 365 inclusive days pass, 366 reject. The service re-checks as a
    # backstop using the same shared predicate.
    if window_exceeds_cap(start_dt, end_dt):
        raise HTTPException(
            status_code=400,
            detail=(
                f"date range exceeds {MAX_LOOKBACK_DAYS}-day maximum window "
                f"(got {(end_dt - start_dt).days} days)"
            ),
        )
    return period, start_dt, end_dt


def _validate_enums(source: str | None, paid_by: str | None) -> None:
    """Reject unknown source/paid_by values with a 400 (not a 500)."""
    if source is not None and source not in SLEEP_REPORT_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source {source!r}; must be one of {list(SLEEP_REPORT_SOURCES)}",
        )
    if paid_by is not None and paid_by not in SLEEP_REPORT_PAID_BY_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid paid_by {paid_by!r}; must be one of {list(SLEEP_REPORT_PAID_BY_VALUES)}"
            ),
        )


# Cost values are rounded to 6 decimals at the API boundary — the SQL
# carries float8 for headroom, but JSON consumers (dashboards / billing
# exports) only need micro-USD precision. Single rounding site so we
# never have to chase down "why does the row total differ from the
# breakdown sum by 1e-12 ?" floating-point drift.
_COST_DECIMALS = 6


def _round(v: float | None) -> float | None:
    """Round to display precision; NULL stays NULL ("cost unknown")."""
    return None if v is None else round(v, _COST_DECIMALS)


def _to_response_row(row: CostAggregationRow) -> CostAggregationRowResponse:
    """Convert the service's plain container into the response model."""
    return CostAggregationRowResponse(
        period_start=row.period_start,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        calls=row.calls,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        tokens_cached_in=row.tokens_cached_in,
        tokens_cache_write=row.tokens_cache_write,
        embedding_tokens=row.embedding_tokens,
        cost_usd=_round(row.cost_usd),
        cost_usd_byok=_round(row.cost_usd_byok),
        cost_breakdown_by_model=[
            CostBreakdownByModelResponse(
                model=b.model,
                calls=b.calls,
                cost_usd=_round(b.cost_usd),
                cost_usd_byok=_round(b.cost_usd_byok),
            )
            for b in row.cost_breakdown_by_model
        ],
        cost_breakdown_by_source=[
            CostBreakdownBySourceResponse(
                source=b.source,
                calls=b.calls,
                cost_usd=_round(b.cost_usd),
                cost_usd_byok=_round(b.cost_usd_byok),
            )
            for b in row.cost_breakdown_by_source
        ],
    )


# ============================================================================
# Admin (cross-workspace) endpoint
# ============================================================================


@router.get(
    "/admin/cost-aggregation",
    response_model=CostAggregationResponse,
    summary="Cost aggregation across all workspaces (admin)",
    tags=["admin"],
)
async def admin_cost_aggregation(
    _admin: AdminUser,  # noqa: ARG001 — FastAPI dep, access guard only
    db: AsyncSession = Depends(get_db),
    period: str = Query("day", description="Aggregation period: day | week | month"),
    from_: date = Query(..., alias="from", description="Inclusive lower-bound date"),
    to: date = Query(..., description="Inclusive upper-bound date"),
    workspace_id: UUID | None = Query(None, description="Filter to a single workspace"),
    user_id: str | None = Query(None, description="Filter to a single user"),
    source: str | None = Query(None, description=f"Filter by source: {list(SLEEP_REPORT_SOURCES)}"),
    paid_by: str | None = Query(
        None, description=f"Filter by billing classification: {list(SLEEP_REPORT_PAID_BY_VALUES)}"
    ),
) -> CostAggregationResponse:
    """Aggregate LLM + embedding cost across every workspace.

    Admin-only (``require_admin``). ``workspace_id`` here is a
    convenience filter — admins can scope to one workspace without
    using the workspace-scoped route — but absent it, every workspace
    is included.
    """
    _validate_enums(source, paid_by)
    period_v, start_dt, end_dt = _parse_window(period, from_, to)

    service = CostAggregationService(db)
    rows = await service.aggregate(
        period=period_v,
        start=start_dt,
        end=end_dt,
        workspace_id=workspace_id,
        user_id=user_id,
        source=source,
        paid_by=paid_by,
    )
    return CostAggregationResponse(rows=[_to_response_row(r) for r in rows])


# ============================================================================
# Workspace-scoped endpoint
# ============================================================================


@router.get(
    "/workspaces/{workspace_id}/cost-aggregation",
    response_model=CostAggregationResponse,
    summary="Cost aggregation scoped to one workspace (owner/admin)",
    tags=["workspaces"],
)
async def workspace_cost_aggregation(
    workspace_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
    period: str = Query("day", description="Aggregation period: day | week | month"),
    from_: date = Query(..., alias="from", description="Inclusive lower-bound date"),
    to: date = Query(..., description="Inclusive upper-bound date"),
    user_id: str | None = Query(None, description="Filter to a single user"),
    source: str | None = Query(None, description=f"Filter by source: {list(SLEEP_REPORT_SOURCES)}"),
    paid_by: str | None = Query(
        None, description=f"Filter by billing classification: {list(SLEEP_REPORT_PAID_BY_VALUES)}"
    ),
) -> CostAggregationResponse:
    """Aggregate cost for a single workspace.

    Requires workspace **owner** or **admin** role — viewer / member
    are rejected because cost aggregates leak per-user activity volume
    across private contexts that those roles should not see. Owners
    review billing; admins manage workspace settings.

    Cross-workspace probing is impossible here: the path-bound
    ``workspace_id`` is the only scope passed to the service, and
    ``check_workspace_admin`` rejects callers without admin/owner
    membership in *that specific* workspace before the query runs.
    """
    _validate_enums(source, paid_by)
    period_v, start_dt, end_dt = _parse_window(period, from_, to)

    perm_service = PermissionService(db)
    await perm_service.check_workspace_admin(user["user_id"], workspace_id)

    service = CostAggregationService(db)
    rows = await service.aggregate(
        period=period_v,
        start=start_dt,
        end=end_dt,
        workspace_id=workspace_id,
        user_id=user_id,
        source=source,
        paid_by=paid_by,
    )
    return CostAggregationResponse(rows=[_to_response_row(r) for r in rows])
