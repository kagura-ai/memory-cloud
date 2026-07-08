"""Memory Analysis access gates (Issue #496).

Composes the 4 access gates that every analysis-mutating endpoint
(REST + MCP) must pass through, in this order:

    1. JWT / API-key auth          (middleware — handled upstream)
    2. require_workspace_owner     (PermissionService — 403)
    3. require_pro_tier            (QuotaService.check_feature_access — 403)
    4. check_memory_analysis_quota (read-only count — 429 fail-fast,
                                   does NOT increment)
    5. check_workspace_in_allowlist (kill switch — 403)
    6. byok_resolver.preflight      (route body — 422, raised inside
                                     orchestrator.start so the gate
                                     module does not duplicate it)

GET endpoints (list / single / active) skip gates 3 + 4 — a Pro user
who has run analyses today should always be able to read their past
runs, even if quota=0 today (#496 issue spec).

Two FastAPI dep helpers + two non-dep helpers are exposed:

- ``require_memory_analysis_access`` — full 4-gate chain (POST/DELETE)
- ``require_memory_analysis_read``   — gate 1+2 only (GET endpoints)
- ``check_memory_analysis_quota``    — read-only count, raises 429
- ``check_workspace_in_allowlist``   — kill switch primitive
- ``check_memory_analysis_access_mcp`` — MCP-side composition (no Depends)

The MCP variant exists because MCP tools cannot use FastAPI ``Depends``;
they call the same underlying primitives in the same order so MCP/REST
parity is structural, not coincidental (#332 教訓).

Quota count semantics:

- The day window uses the **caller's** ``User.timezone`` (default UTC).
  Two owners in different timezones in the same workspace each see
  their own day boundary; daily=3 makes the edge case rare in practice.
- ``cancelled`` rows count toward the quota, by design — once the
  pipeline has been kicked off, BYOK / partial LLM cost may already
  be incurred. Conservative counting prevents kick-cancel spam.
- Increment is **NOT** done here. Quota check is read-only; the row
  is INSERTed by ``AnalysisOrchestrator.start()`` after BYOK pre-flight,
  so a 422 BYOK failure never debits the day window.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Re-exported from ``auth.analysis_allowlist`` so existing callers
# (gate functions below + tests) keep working unchanged. The pure
# helper lives in its own module to avoid pulling the rest of this
# module's quota/tier/BYOK dependency tree into callers that only
# need the membership check (PR #538 / #497 review).
from auth.analysis_allowlist import check_workspace_in_allowlist  # noqa: F401
from auth.dependencies import get_user_from_api_key_or_session
from db.base import get_db
from models.auth import User
from services.analysis.query_service import day_window_utc
from utils.exceptions import (
    AuthorizationError,
    ConfigurationError,
    FeatureNotAvailableError,
    QuotaExceededError,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# Quota check — read-only, day window in caller's timezone
# ============================================================================


async def _get_user_timezone(db: AsyncSession, user_id: str) -> str:
    """Fetch the caller's timezone. Defaults to UTC when missing.

    Used for both the quota count window AND the response's
    ``resets_at`` ISO string so the user's "midnight" is consistent
    across the API contract.
    """
    result = await db.execute(select(User.timezone).where(User.user_id == user_id))
    tz = result.scalar_one_or_none()
    return tz or "UTC"


async def check_memory_analysis_quota(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    user_timezone: str,
) -> None:
    """Read-only check: raise 429 if today's analysis quota is exhausted.

    "Today" = the calendar date in ``user_timezone``. Counts ALL
    ``memory_analyses`` rows for the workspace whose ``started_at``
    falls in the current day window — regardless of status. See module
    docstring for why ``cancelled`` rows count.

    Single workspace fetch shared with ``EffectiveQuotaService``: we
    pre-fetch the row here (for ``addon_analysis_bonus`` + the same
    workspace identity used downstream) so the gate does not double-
    SELECT ``Workspace`` on every call. The COUNT remains its own
    statement because the index path differs (``memory_analyses``
    composite index on ``workspace_id, started_at`` vs. the workspace
    PK lookup).

    Raises:
        QuotaExceededError: with ``QUOTA-001`` and structured details
            (``used_today`` / ``limit_today`` / ``addon_bonus`` /
            ``remaining_today`` / ``resets_at``) so the API layer's
            global handler can render the 429 body without re-querying.
    """
    from models.analysis import MemoryAnalysis
    from models.auth import Workspace
    from services.effective_quota_service import EffectiveQuotaService

    day_start_utc, day_end_utc = day_window_utc(user_timezone)

    # Single Workspace fetch: pulls the row that ``EffectiveQuotaService``
    # would otherwise re-SELECT plus the ``addon_analysis_bonus`` column
    # we surface in the 429 detail. The service still validates and
    # triggers addon recalculation if needed; passing the pre-fetched
    # row avoids the redundant round-trip on the happy path.
    workspace_row = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if workspace_row is None:
        # Defensive — owner gate already proved membership; this would
        # only trigger on a deleted workspace mid-request. Surface as
        # 500 ``ConfigurationError`` (CFG-001) rather than 429
        # QuotaExceededError, since "no workspace" is a server-side
        # anomaly, not a quota-exhausted client signal.
        raise ConfigurationError(
            f"Workspace {workspace_id} disappeared mid-request — gate cannot evaluate quota"
        )

    count_stmt = select(func.count(MemoryAnalysis.id)).where(
        and_(
            MemoryAnalysis.workspace_id == workspace_id,
            MemoryAnalysis.started_at >= day_start_utc,
            MemoryAnalysis.started_at < day_end_utc,
        )
    )
    used_today = int((await db.execute(count_stmt)).scalar() or 0)

    effective = await EffectiveQuotaService(db).get_effective_quotas(workspace_id)
    limit_today = int(effective["analysis_runs_per_day"])
    # Refresh the workspace row AFTER ``get_effective_quotas`` runs —
    # the service may recalculate addon bonus columns (when all are 0)
    # and re-fetch the workspace internally. Without this refresh the
    # 429 detail would carry the pre-recalc addon_bonus (often 0) while
    # ``limit_today`` reflects the recalculated effective quota,
    # producing an inconsistent body. Issue #496 Copilot review.
    await db.refresh(workspace_row)
    addon_bonus = int(workspace_row.addon_analysis_bonus or 0)

    if used_today >= limit_today:
        # day_end_utc is naive UTC; reformat with caller tz for the
        # client-visible reset timestamp (matches ``resets_at`` in the
        # AC's example body). Defensive ``try/except`` mirrors
        # ``day_window_utc()``: ``User.timezone`` is varchar(50)
        # without a CHECK, so an exotic value should not 500 the gate.
        try:
            client_tz = ZoneInfo(user_timezone) if user_timezone else ZoneInfo("UTC")
        except Exception:
            client_tz = ZoneInfo("UTC")
        resets_at = day_end_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(client_tz).isoformat()
        logger.info(
            "memory_analysis_quota_exceeded",
            workspace_id=str(workspace_id),
            used_today=used_today,
            limit_today=limit_today,
            user_timezone=user_timezone,
        )
        raise QuotaExceededError(
            (
                f"Analysis daily quota exceeded: {used_today}/{limit_today} runs today "
                f"(addon bonus {addon_bonus}). Resets at {resets_at}."
            ),
            quota_type="memory_analysis",
            used_today=used_today,
            limit_today=limit_today,
            addon_bonus=addon_bonus,
            remaining_today=0,
            resets_at=resets_at,
        )


# ============================================================================
# Composed gate — REST FastAPI Depends entry points
# ============================================================================


async def require_memory_analysis_access(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
) -> tuple[str, UUID, str]:
    """Full 4-gate chain for POST/DELETE endpoints.

    Order matches the issue spec's fail-fast precedence — the FIRST
    failing gate determines the error returned to the client:

        - Gate 2 (owner)       → 403 (PermissionService.check_workspace_owner)
        - Gate 3 (Pro tier)    → 403 with FEAT-001 detail
        - Gate 4 (quota)       → 429 with QUOTA-001 detail
        - Gate 5 (allowlist)   → 403 (FEAT-001 with allowlist reason)

    Returns ``(user_id, workspace_id, user_timezone)`` so the route
    handler can pass tz into the orchestrator/response without an
    extra User round-trip.
    """
    user_id = user.get("user_id")
    if not user_id:
        # Defensive — APIKeyOrSessionUser shouldn't return this state.
        raise HTTPException(status_code=401, detail="Not authenticated")

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="No workspace selected. Please select a workspace first.",
        )

    # Gate 2: owner role
    from services.permission_service import PermissionService

    perm = PermissionService(db)
    try:
        await perm.check_workspace_owner(user_id, workspace_id)
    except AuthorizationError as exc:
        logger.warning(
            "memory_analysis_owner_denied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            status_code=exc.status_code,
        )
        raise

    # Gate 3: Pro-tier feature flag
    from services.quota_service import QuotaService

    quota = QuotaService(db)
    has_feature, err = await quota.check_feature_access(workspace_id, "memory_analysis")
    if not has_feature:
        raise FeatureNotAvailableError(
            err or "memory_analysis not available", feature="memory_analysis"
        )

    # Gate 4: daily quota — read-only, raises 429
    user_timezone = await _get_user_timezone(db, user_id)
    await check_memory_analysis_quota(
        db,
        workspace_id=workspace_id,
        user_timezone=user_timezone,
    )

    # Gate 5: allowlist kill switch
    if not check_workspace_in_allowlist(workspace_id):
        raise FeatureNotAvailableError(
            "Memory analysis is not yet enabled for this workspace.",
            feature="memory_analysis",
        )

    logger.info(
        "memory_analysis_access_granted",
        user_id=user_id,
        workspace_id=str(workspace_id),
    )
    return user_id, workspace_id, user_timezone


async def require_memory_analysis_read(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
) -> tuple[str, UUID, str]:
    """Read-only gate (GET list / single / active).

    Skips Pro-tier and quota gates so a Pro-cancelled or quota=0 user
    can still poll past runs they own. Allowlist still applies — a
    workspace removed from the allowlist should not be reading runs
    either.
    """
    user_id = user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="No workspace selected. Please select a workspace first.",
        )

    from services.permission_service import PermissionService

    perm = PermissionService(db)
    try:
        await perm.check_workspace_owner(user_id, workspace_id)
    except AuthorizationError as exc:
        logger.warning(
            "memory_analysis_read_denied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            status_code=exc.status_code,
        )
        raise

    if not check_workspace_in_allowlist(workspace_id):
        raise FeatureNotAvailableError(
            "Memory analysis is not yet enabled for this workspace.",
            feature="memory_analysis",
        )

    user_timezone = await _get_user_timezone(db, user_id)
    return user_id, workspace_id, user_timezone


# ============================================================================
# MCP composition (non-Depends, called from tools/analysis.py)
# ============================================================================


async def check_memory_analysis_access_mcp(
    db: AsyncSession,
    *,
    user_id: str,
    workspace_id: UUID,
    require_quota: bool = True,
) -> str:
    """MCP-side equivalent of ``require_memory_analysis_access``.

    Called from ``mcp_server/tools/analysis.py`` since MCP tools
    cannot use FastAPI ``Depends``. Mirrors the REST gate ordering
    so MCP and REST behave identically (#332 lesson).

    ``require_quota=False`` mode is used by GET-equivalent MCP tools
    (``get_analysis``, ``list_analyses``, ``get_active_analysis``,
    ``get_cluster``) to skip gates 3 + 4 — same parity as
    ``require_memory_analysis_read``.

    Returns the caller's ``user_timezone`` for downstream formatting.

    Raises ``AuthorizationError`` / ``FeatureNotAvailableError`` /
    ``QuotaExceededError`` so the caller can map to the MCP error envelope
    at one place.
    """
    from services.permission_service import PermissionService

    perm = PermissionService(db)
    await perm.check_workspace_owner(user_id, workspace_id)

    if require_quota:
        from services.quota_service import QuotaService

        quota = QuotaService(db)
        has_feature, err = await quota.check_feature_access(workspace_id, "memory_analysis")
        if not has_feature:
            raise FeatureNotAvailableError(
                err or "memory_analysis not available", feature="memory_analysis"
            )

        user_timezone = await _get_user_timezone(db, user_id)
        await check_memory_analysis_quota(
            db,
            workspace_id=workspace_id,
            user_timezone=user_timezone,
        )
    else:
        user_timezone = await _get_user_timezone(db, user_id)

    if not check_workspace_in_allowlist(workspace_id):
        raise FeatureNotAvailableError(
            "Memory analysis is not yet enabled for this workspace.",
            feature="memory_analysis",
        )

    return user_timezone


# ============================================================================
# Type aliases — REST routes import these for cleaner signatures
# ============================================================================

AnalysisWriteAccess = Annotated[
    tuple[str, UUID, str],
    Depends(require_memory_analysis_access),
]
"""Returned tuple is ``(user_id, workspace_id, user_timezone)``."""

AnalysisReadAccess = Annotated[
    tuple[str, UUID, str],
    Depends(require_memory_analysis_read),
]
"""Returned tuple is ``(user_id, workspace_id, user_timezone)``."""
