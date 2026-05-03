"""Sleep Reporter Service.

Issue #526: Extracted from ``api.routes.sleep_reports`` so both the admin
(cross-workspace) and workspace-scoped endpoints can share the same listing
and detail logic.

Same two-endpoint pattern as ``CostAggregationService`` (#472):
- Admin route passes ``workspace_id=None`` to see every workspace.
- Workspace route passes a fixed ``workspace_id`` to scope to one workspace.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context
from models.sleep import SleepAction, SleepReport
from utils.logger import get_logger

logger = get_logger(__name__)


class SleepReporterService:
    """Shared service for sleep report listing and detail queries.

        Both admin and workspace routes delegate to this service; the only
    difference is the ``workspace_id`` filter (``None`` = all workspaces).
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list_reports(
        self,
        *,
        workspace_id: UUID | None = None,
        status_filter: str | None = None,
        context_id: UUID | None = None,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SleepReport], int]:
        """List sleep reports with filters and pagination.

        Returns:
            (list of ORM instances, total count).  Callers must
            batch-resolve ``context_name`` via ``resolve_context_names``.
        """
        conditions = []
        if status_filter is not None:
            conditions.append(SleepReport.status == status_filter)
        if workspace_id is not None:
            conditions.append(SleepReport.workspace_id == workspace_id)
        if context_id is not None:
            conditions.append(SleepReport.context_id == context_id)
        if user_id is not None:
            conditions.append(SleepReport.user_id == user_id)

        count_stmt = select(func.count()).select_from(SleepReport)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar() or 0

        stmt = select(SleepReport)
        if conditions:
            stmt = stmt.where(*conditions)
        result = await self.db.execute(
            stmt.order_by(SleepReport.started_at.desc()).limit(limit).offset(offset)
        )
        reports = list(result.scalars().all())
        return reports, total

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------

    async def get_report_detail(
        self,
        report_id: UUID,
        *,
        workspace_id: UUID | None = None,
    ) -> tuple[SleepReport, list[SleepAction]] | None:
        """Get a single sleep report with its action audit log.

        If ``workspace_id`` is provided, returns ``None`` when the report
        belongs to a different workspace (caller should surface 404).

        Returns:
            (report ORM instance, actions) or ``None`` if not found /
            wrong workspace.  Callers must resolve ``context_name`` and
            ``context_deleted`` themselves.
        """
        report_result = await self.db.execute(
            select(SleepReport).where(SleepReport.id == report_id)
        )
        report = report_result.scalar_one_or_none()
        if not report:
            return None

        if workspace_id is not None and report.workspace_id != workspace_id:  # type: ignore[operator]
            return None

        actions_result = await self.db.execute(
            select(SleepAction).where(SleepAction.report_id == report_id).order_by(SleepAction.id)
        )
        actions = list(actions_result.scalars().all())
        return report, actions

    # ------------------------------------------------------------------
    # Context resolution helper
    # ------------------------------------------------------------------

    async def resolve_context_names(self, context_ids: set[UUID]) -> dict[UUID, str | None]:
        """Batch-resolve context display names.

        Returns a map of ``context_id → display_name | name | None``.
        ``None`` means the context is deleted or missing.
        """
        ctx_map: dict[UUID, str | None] = {}
        if not context_ids:
            return ctx_map

        result = await self.db.execute(
            select(Context.id, Context.name, Context.display_name, Context.deleted_at).where(
                Context.id.in_(context_ids)
            )
        )
        for ctx_id, ctx_name, ctx_display_name, ctx_deleted_at in result.all():
            if ctx_deleted_at is not None:
                ctx_map[ctx_id] = None
            else:
                ctx_map[ctx_id] = ctx_display_name or ctx_name
        return ctx_map
