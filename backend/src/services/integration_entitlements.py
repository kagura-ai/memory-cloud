"""Ordered billing snapshots and live paid-integration commit authorization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.auth import Workspace
from models.integration_entitlement import (
    WorkspaceIntegrationEvent,
    WorkspaceIntegrationLicense,
    WorkspaceIntegrationState,
)
from models.integration_entitlement_schemas import (
    EntitlementWindow,
    IntegrationEntitlementPush,
    IntegrationEntitlementResult,
    IntegrationEntitlementView,
    ServiceEntitlement,
)
from utils.datetime import utcnow
from utils.exceptions import AuthorizationError, ConflictError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


async def _workspace(db: AsyncSession, workspace_id: UUID, *, lock: bool) -> Workspace | None:
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def _state(db: AsyncSession, workspace_id: UUID) -> WorkspaceIntegrationState | None:
    return (
        await db.execute(
            select(WorkspaceIntegrationState)
            .where(WorkspaceIntegrationState.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _max_window(state: WorkspaceIntegrationState) -> EntitlementWindow | None:
    if (
        state.max_valid_from is None
        or state.max_valid_until is None
        or state.max_evidence_ref is None
    ):
        return None
    return EntitlementWindow(
        valid_from=state.max_valid_from.replace(tzinfo=UTC),
        valid_until=state.max_valid_until.replace(tzinfo=UTC),
        evidence_ref=state.max_evidence_ref,
    )


async def read_integration_entitlements(
    db: AsyncSession, workspace_id: UUID
) -> IntegrationEntitlementView:
    """Read one coherent snapshot under the publisher's workspace lock.

    Keep the transaction open until the view has been assembled. Separate
    SELECTs under READ COMMITTED otherwise mix revisions across a publish.
    """
    workspace = await _workspace(db, workspace_id, lock=True)
    if workspace is None or workspace.deleted_at is not None:
        raise NotFoundException("Workspace")
    state = await _state(db, workspace_id)
    licenses = (
        (
            await db.execute(
                select(WorkspaceIntegrationLicense)
                .where(WorkspaceIntegrationLicense.workspace_id == workspace_id)
                .order_by(WorkspaceIntegrationLicense.service_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return IntegrationEntitlementView(
        workspace_id=workspace_id,
        plan_name=workspace.plan_name,
        revision=state.revision if state else 0,
        event_id=state.event_id if state else None,
        source="external_billing" if state else None,
        source_ref=state.source_ref if state else None,
        max_access=_max_window(state) if state else None,
        services=[
            ServiceEntitlement(
                service_id=item.service_id,
                valid_from=item.valid_from.replace(tzinfo=UTC),
                valid_until=item.valid_until.replace(tzinfo=UTC),
                evidence_ref=item.evidence_ref,
            )
            for item in licenses
        ],
        updated_at=state.updated_at if state else None,
    )


def _digest(workspace_id: UUID, body: IntegrationEntitlementPush) -> str:
    payload = body.model_dump(mode="json")
    payload["services"] = sorted(payload["services"], key=lambda item: item["service_id"])
    wire = json.dumps({"workspace_id": str(workspace_id), "snapshot": payload}, sort_keys=True)
    return hashlib.sha256(wire.encode()).hexdigest()


async def _replace_licenses(
    db: AsyncSession, workspace_id: UUID, services: list[ServiceEntitlement]
) -> None:
    rows = (
        (
            await db.execute(
                select(WorkspaceIntegrationLicense)
                .where(WorkspaceIntegrationLicense.workspace_id == workspace_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    existing = {row.service_id: row for row in rows}
    for item in services:
        row = existing.pop(item.service_id, None)
        if row is None:
            row = WorkspaceIntegrationLicense(workspace_id=workspace_id, service_id=item.service_id)
            db.add(row)
        row.valid_from = item.valid_from.replace(tzinfo=None)
        row.valid_until = item.valid_until.replace(tzinfo=None)
        row.evidence_ref = item.evidence_ref
    for row in existing.values():
        await db.delete(row)


async def apply_integration_entitlements(
    db: AsyncSession, workspace_id: UUID, body: IntegrationEntitlementPush
) -> IntegrationEntitlementResult:
    """Apply a complete newer snapshot atomically; the caller owns the commit.

    Workspace locking precedes all state/receipt/license access and is shared
    by consumers and existing plan mutations. No provider SDK/status is trusted.
    """
    workspace = await _workspace(db, workspace_id, lock=True)
    if workspace is None or workspace.deleted_at is not None:
        raise NotFoundException("Workspace")
    state = await _state(db, workspace_id)
    digest = _digest(workspace_id, body)
    receipt = await db.get(WorkspaceIntegrationEvent, (workspace_id, body.event_id))
    outcome: Literal["applied", "duplicate", "stale"] = "applied"
    if receipt is not None:
        if receipt.revision != body.revision or receipt.payload_digest != digest:
            raise ConflictError("Entitlement event was reused with different content")
        outcome = "duplicate"
    else:
        prior = (
            await db.execute(
                select(WorkspaceIntegrationEvent.event_id).where(
                    WorkspaceIntegrationEvent.workspace_id == workspace_id,
                    WorkspaceIntegrationEvent.revision == body.revision,
                )
            )
        ).scalar_one_or_none()
        if prior is not None or (state is not None and body.revision == state.revision):
            raise ConflictError("Entitlement revision was already used")
        if state is not None and body.revision < state.revision:
            outcome = "stale"
    if outcome == "applied":
        if state is None:
            state = WorkspaceIntegrationState(workspace_id=workspace_id)
            db.add(state)
        state.revision, state.event_id = body.revision, body.event_id
        state.source, state.source_ref = "external_billing", body.source_ref
        window = body.max_access
        state.max_valid_from = window.valid_from.replace(tzinfo=None) if window else None
        state.max_valid_until = window.valid_until.replace(tzinfo=None) if window else None
        state.max_evidence_ref = window.evidence_ref if window else None
        state.updated_at = utcnow()
        await db.flush()
        await _replace_licenses(db, workspace_id, body.services)
        db.add(
            WorkspaceIntegrationEvent(
                workspace_id=workspace_id,
                event_id=body.event_id,
                revision=body.revision,
                payload_digest=digest,
            )
        )
        await db.flush()
        logger.info(
            "integration_entitlements_applied",
            workspace_id=str(workspace_id),
            revision=body.revision,
            service_count=len(body.services),
        )
    view = await read_integration_entitlements(db, workspace_id)
    return IntegrationEntitlementResult(
        **view.model_dump(), applied=outcome == "applied", outcome=outcome
    )


@dataclass(frozen=True)
class PaidIntegrationAccess:
    """Point-in-time inspection, never a reusable authorization credential."""

    allowed: bool
    reason: str
    revision: int = 0
    valid_until: datetime | None = None


async def _access(
    db: AsyncSession, workspace_id: UUID, service_id: str, *, lock: bool
) -> PaidIntegrationAccess:
    workspace = await _workspace(db, workspace_id, lock=lock)
    if workspace is None or workspace.deleted_at is not None:
        return PaidIntegrationAccess(False, "unavailable")
    state = await _state(db, workspace_id)
    if state is None:
        return PaidIntegrationAccess(False, "not_entitled")
    if (
        workspace.plan_name != "promax"
        or state.max_valid_from is None
        or state.max_valid_until is None
    ):
        return PaidIntegrationAccess(False, "max_required", state.revision)
    license = (
        await db.execute(
            select(WorkspaceIntegrationLicense)
            .where(
                WorkspaceIntegrationLicense.workspace_id == workspace_id,
                WorkspaceIntegrationLicense.service_id == service_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if license is None:
        return PaidIntegrationAccess(False, "not_entitled", state.revision)
    # Deliberately after lock acquisition and all DB awaits, not transaction
    # start time (which would remain stale throughout a long-running write).
    now = utcnow()
    if now < max(state.max_valid_from, license.valid_from):
        return PaidIntegrationAccess(False, "not_started", state.revision)
    end = min(state.max_valid_until, license.valid_until)
    if now >= end:
        return PaidIntegrationAccess(False, "expired", state.revision)
    return PaidIntegrationAccess(True, "active", state.revision, end)


async def inspect_paid_integration(
    db: AsyncSession, workspace_id: UUID, service_id: str
) -> PaidIntegrationAccess:
    """Inspect current eligibility for display; writes must use the transaction guard."""
    return await _access(db, workspace_id, service_id, lock=False)


async def _ensure_transaction_has_no_writes_or_row_locks(db: AsyncSession) -> None:
    """Reject callers that already made a write or acquired a row lock."""
    if await db.scalar(text("SELECT pg_current_xact_id_if_assigned()")) is not None:
        raise RuntimeError(
            "Paid integration authority must be checked before transaction writes or row locks"
        )


async def _require_paid_integration(
    db: AsyncSession,
    workspace_id: UUID,
    service_id: str,
    *,
    check_transaction: bool,
) -> PaidIntegrationAccess:
    if check_transaction:
        await _ensure_transaction_has_no_writes_or_row_locks(db)
    result = await _access(db, workspace_id, service_id, lock=True)
    if not result.allowed:
        raise AuthorizationError(
            "Paid integration is not available", reason="paid_integration_unavailable"
        )
    return result


async def require_paid_integration(
    db: AsyncSession, workspace_id: UUID, service_id: str
) -> PaidIntegrationAccess:
    """Require live rights under the shared authority lock, held until transaction end."""
    return await _require_paid_integration(db, workspace_id, service_id, check_transaction=True)


@asynccontextmanager
async def paid_integration_write(
    db: AsyncSession, workspace_id: UUID, service_id: str
) -> AsyncIterator[PaidIntegrationAccess]:
    """Own a write transaction and recheck paid rights after flush, before commit.

    Authenticate first, enter before mutations or other row locks, then verify
    mutable role/installation/target authority under the held lock. The caller
    must hold any further authority-row locks through this helper's commit.
    No caller-controlled commit, rollback or nested transaction is allowed.
    Cancellation, expiry and errors roll back the batch.
    """
    if db.new or db.dirty or db.deleted or db.info.get("paid_integration_write"):
        raise RuntimeError("Paid integration guard requires a clean, unguarded session")
    await _ensure_transaction_has_no_writes_or_row_locks(db)
    may_commit = False

    def only_guard_commits(_session: Session) -> None:
        if not may_commit:
            raise RuntimeError("Paid integration guard owns the commit")

    db.info["paid_integration_write"] = True
    event.listen(db.sync_session, "before_commit", only_guard_commits)
    try:
        access = await _require_paid_integration(
            db, workspace_id, service_id, check_transaction=False
        )
        transaction = db.get_transaction()
        yield access
        if db.get_transaction() is not transaction:
            raise RuntimeError("Paid integration transaction changed inside the guard")
        await db.flush()
        await _require_paid_integration(db, workspace_id, service_id, check_transaction=False)
        may_commit = True
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    finally:
        event.remove(db.sync_session, "before_commit", only_guard_commits)
        db.info.pop("paid_integration_write", None)
