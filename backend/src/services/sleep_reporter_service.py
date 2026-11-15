"""Sleep Reporter Service.

Issue #526: Extracted from ``api.routes.sleep_reports`` so both the admin
(cross-workspace) and workspace-scoped endpoints can share the same listing
and detail logic.

Same two-endpoint pattern as ``CostAggregationService`` (#472):
- Admin route passes ``workspace_id=None`` to see every workspace.
- Workspace route passes a fixed ``workspace_id`` to scope to one workspace.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context
from models.sleep import (
    SLEEP_REPORT_SOURCE_ALL,
    SLEEP_REPORT_SOURCE_FILTERS,
    SleepAction,
    SleepReport,
)
from services.label_resolver import resolve_user_labels, resolve_workspace_names
from utils.logger import get_logger

logger = get_logger(__name__)

# One selected ``sleep_actions`` row of ``list_actions`` (#2842): id, phase,
# action_type, memory_id, target_id, details, created_at.
SleepActionRow = Row[int, str, str, UUID | None, UUID | None, dict[str, Any] | None, datetime]


def sleep_report_source_condition(source: str) -> ColumnElement[bool] | None:
    """The WHERE condition for a ``source`` list filter (#2219).

    Shared by the Sleep Reports list endpoints and the MCP
    ``get_sleep_history`` tool, so the filter has one implementation.

    Args:
        source: ``"sleep"``, ``"analysis"`` or ``"all"``
            (``SLEEP_REPORT_SOURCE_FILTERS``).

    Returns:
        ``SleepReport.source == source``, or ``None`` for ``"all"``.

    Raises:
        ValueError: ``source`` is not one of ``SLEEP_REPORT_SOURCE_FILTERS``.
    """
    if source not in SLEEP_REPORT_SOURCE_FILTERS:
        raise ValueError(f"Invalid source. Must be one of: {list(SLEEP_REPORT_SOURCE_FILTERS)}")
    if source == SLEEP_REPORT_SOURCE_ALL:
        return None
    return SleepReport.source == source


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
        source: str = SLEEP_REPORT_SOURCE_ALL,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SleepReport], int]:
        """List sleep reports with filters and pagination.

        ``source`` narrows to Sleep runs or analysis rows (#2219); the
        default ``"all"`` lists both.

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
        source_condition = sleep_report_source_condition(source)
        if source_condition is not None:
            conditions.append(source_condition)

        count_stmt = select(func.count()).select_from(SleepReport)
        if conditions:
            count_stmt = count_stmt.where(*conditions)

        stmt = select(SleepReport)
        if conditions:
            stmt = stmt.where(*conditions)
        # ``id`` breaks ties of ``started_at`` so ``offset`` pages never repeat
        # or skip a row (#2858).
        stmt = (
            stmt.order_by(SleepReport.started_at.desc(), SleepReport.id).limit(limit).offset(offset)
        )

        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar() or 0

        result = await self.db.execute(stmt)
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
        report = await self.get_report(report_id, workspace_id=workspace_id)
        if report is None:
            return None

        actions_stmt = (
            select(SleepAction).where(SleepAction.report_id == report_id).order_by(SleepAction.id)
        )
        actions_result = await self.db.execute(actions_stmt)
        actions = list(actions_result.scalars().all())
        return report, actions

    async def get_report(
        self,
        report_id: UUID,
        *,
        workspace_id: UUID | None = None,
    ) -> SleepReport | None:
        """Get a single sleep report's own row, without its actions (#2842).

        If ``workspace_id`` is provided, returns ``None`` when the report
        belongs to a different workspace (caller should surface 404).
        """
        report_result = await self.db.execute(
            select(SleepReport).where(SleepReport.id == report_id)
        )
        report = report_result.scalar_one_or_none()
        if not report:
            return None

        if workspace_id is not None and report.workspace_id != workspace_id:  # type: ignore[operator]
            return None
        return report

    # ------------------------------------------------------------------
    # Actions (#2842)
    # ------------------------------------------------------------------

    async def report_exists(self, report_id: UUID) -> bool:
        """Whether a sleep report with this id exists (reads the id only)."""
        result = await self.db.execute(select(SleepReport.id).where(SleepReport.id == report_id))
        return result.scalar_one_or_none() is not None

    async def count_actions(self, report_id: UUID) -> int:
        """The number of actions a run recorded, without reading an action row."""
        result = await self.db.execute(
            select(func.count()).select_from(SleepAction).where(SleepAction.report_id == report_id)
        )
        return result.scalar() or 0

    async def list_actions(
        self, report_id: UUID, *, limit: int, offset: int
    ) -> Sequence[SleepActionRow]:
        """One page of a run's actions in ``id`` order (the order they were recorded).

        ``sleep_actions.id`` is unique and grows with every insert, so a
        running report's new actions land on later pages and a walk by
        ``offset`` meets each action once.

        Returns:
            Rows of ``(id, phase, action_type, memory_id, target_id, details,
            created_at)``: the fields of an action the routes answer.
        """
        result = await self.db.execute(
            select(
                SleepAction.id,
                SleepAction.phase,
                SleepAction.action_type,
                SleepAction.memory_id,
                SleepAction.target_id,
                SleepAction.details,
                SleepAction.created_at,
            )
            .where(SleepAction.report_id == report_id)
            .order_by(SleepAction.id)
            .limit(limit)
            .offset(offset)
        )
        return result.all()

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

    async def resolve_context_name(self, context_id: UUID | None) -> tuple[str | None, bool]:
        """Resolve a single context name and deleted status.

        Returns:
            (display_name | name | None, deleted).
        """
        if context_id is None:
            return None, False

        result = await self.db.execute(
            select(Context.name, Context.display_name, Context.deleted_at).where(
                Context.id == context_id
            )
        )
        row = result.one_or_none()
        if not row:
            return None, True

        ctx_name, ctx_display_name, ctx_deleted_at = row
        if ctx_deleted_at is not None:
            return None, True
        return ctx_display_name or ctx_name, False

    # ------------------------------------------------------------------
    # Workspace name resolution helper (#2844)
    # ------------------------------------------------------------------

    async def resolve_workspace_names(self, workspace_ids: set[UUID]) -> dict[UUID, str]:
        """Batch-resolve ``workspace_id → name`` for Sleep report rows.

        Delegates to ``services.label_resolver.resolve_workspace_names``
        (#1861), which the cost-aggregation routes share. One query, none for
        an empty set; a soft-deleted or missing workspace is left out, so its
        reports answer ``workspace_name: null``.
        """
        return await resolve_workspace_names(self.db, workspace_ids)

    # ------------------------------------------------------------------
    # User identity resolution helper (#1201)
    # ------------------------------------------------------------------

    async def resolve_user_labels(
        self, user_ids: set[str], *, member_of: UUID | None = None
    ) -> dict[str, str]:
        """Batch-resolve ``user_id → email`` for Sleep report rows.

        Issue #1201: Sleep runs per ``(user_id, workspace_id, context_id)``
        partition, so a workspace-scoped list can show the same context name on
        multiple rows (one per member / connector identity). Resolving the
        owning user's email lets the UI tell those rows apart.

        Delegates to ``services.label_resolver.resolve_user_labels`` (#1861),
        which the cost-aggregation routes share. One query, no N+1; ids with
        no ``users`` row are omitted so the UI falls back to a shortened id.

        ``member_of`` (#1882) narrows the answer to current members of that
        workspace. The workspace-scoped routes pass their path workspace: a
        report outlives the membership of the account it ran for, and a
        workspace owner may not learn the email of an account that has left.
        The system-admin routes pass nothing.
        """
        return await resolve_user_labels(self.db, user_ids, member_of=member_of)
