"""Ordered billing snapshots and live paid-integration commit authorization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from config.billing_addons import ADDON_COLUMNS
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
from models.workspace_billing_schemas import WorkspaceBillingPush, WorkspaceBillingView
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


async def read_ordered_billing_mode(db: AsyncSession, workspace_id: UUID) -> bool:
    """Read the writer fence; a mutating caller must already hold the workspace lock."""
    return bool(
        await db.scalar(
            select(WorkspaceIntegrationState.ordered_billing).where(
                WorkspaceIntegrationState.workspace_id == workspace_id
            )
        )
    )


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


def _digest(
    workspace_id: UUID,
    body: IntegrationEntitlementPush,
) -> str:
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
    db: AsyncSession,
    workspace_id: UUID,
    body: IntegrationEntitlementPush,
) -> IntegrationEntitlementResult:
    """Apply a complete newer snapshot atomically; the caller owns the commit.

    Workspace locking precedes all state/receipt/license access and is shared
    by consumers and existing plan mutations. No provider SDK/status is trusted.
    """
    workspace = await _workspace(db, workspace_id, lock=True)
    if workspace is None or workspace.deleted_at is not None:
        raise NotFoundException("Workspace")
    state = await _state(db, workspace_id)
    ordered = isinstance(body, WorkspaceBillingPush)
    billing_plan = body.plan if isinstance(body, WorkspaceBillingPush) else None
    if state is not None and state.ordered_billing and not ordered:
        logger.info(
            "ordered_billing_refused",
            workspace_id=str(workspace_id),
            route="/internal/workspaces/{workspace_id}/integration-entitlements",
        )
        raise ConflictError(
            "Workspace requires an ordered billing snapshot", reason="ordered_billing_required"
        )
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
    plan_change: dict[str, Any] | None = None
    if outcome == "applied":
        if state is None:
            state = WorkspaceIntegrationState(workspace_id=workspace_id)
            db.add(state)
        state.revision, state.event_id = body.revision, body.event_id
        state.source, state.source_ref = "external_billing", body.source_ref
        if ordered:
            state.ordered_billing = True
        if billing_plan is not None:
            plan_change = {
                "old_plan_name": workspace.plan_name,
                "old_addons": {key: getattr(workspace, col) for key, col in ADDON_COLUMNS.items()},
                "old_entitlement_source": workspace.entitlement_source,
            }
            workspace.plan_name = billing_plan.plan_name
            workspace.entitlement_source = "external_billing"
            for key, column in ADDON_COLUMNS.items():
                setattr(workspace, column, billing_plan.addons.get(key, 0))
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
        if plan_change is not None:
            logger.info(
                "workspace_billing_plan_applied",
                workspace_id=str(workspace_id),
                revision=body.revision,
                **plan_change,
                new_plan_name=workspace.plan_name,
                new_addons={key: getattr(workspace, col) for key, col in ADDON_COLUMNS.items()},
                new_entitlement_source=workspace.entitlement_source,
            )
        logger.info(
            "integration_entitlements_applied",
            workspace_id=str(workspace_id),
            revision=body.revision,
            service_count=len(body.services),
        )
    view = await read_integration_entitlements(db, workspace_id)
    if outcome == "applied":
        from services.sync_management import stop_ineligible_workspace
        from services.workspace_slots import finish_plan_change, prepare_plan_change

        # #2918: the owner's slot lock by try-lock BEFORE the sync stop, so
        # only this workspace row is held while it backs off; the reconcile
        # after it. A plan change that may lower the allowance answers 409
        # ``workspace_slots_busy`` (nothing committed) when the lock stays
        # busy. Duplicate and stale snapshots never get here.
        slot_change = None
        if plan_change is not None:
            slot_change = await prepare_plan_change(
                db,
                workspace_id=workspace_id,
                owner_user_id=workspace.owner_user_id,
                cause="billing_snapshot",
                previous_plan_name=plan_change["old_plan_name"],
                previous_entitlement_source=plan_change["old_entitlement_source"],
                new_plan_name=workspace.plan_name,
                new_entitlement_source=workspace.entitlement_source,
            )
        await stop_ineligible_workspace(db, workspace_id)
        await finish_plan_change(db, slot_change)
    return IntegrationEntitlementResult(
        **view.model_dump(), applied=outcome == "applied", outcome=outcome
    )


async def read_workspace_billing(db: AsyncSession, workspace_id: UUID) -> WorkspaceBillingView:
    """Read plan, quotas and rights under the same retained workspace row lock."""
    view = await read_integration_entitlements(db, workspace_id)
    workspace = await _workspace(db, workspace_id, lock=False)
    if workspace is None:
        raise NotFoundException("Workspace")
    state = await _state(db, workspace_id)
    return WorkspaceBillingView.model_validate(
        {
            **view.model_dump(),
            "plan": {
                "plan_name": workspace.plan_name,
                "addons": {
                    key: getattr(workspace, column) for key, column in ADDON_COLUMNS.items()
                },
            },
            "ordered_billing": bool(state and state.ordered_billing),
            "entitlement_source": workspace.entitlement_source,
        }
    )


@dataclass(frozen=True)
class PaidIntegrationAccess:
    """Point-in-time inspection, never a reusable authorization credential."""

    allowed: bool
    reason: str
    revision: int = 0
    valid_until: datetime | None = None


@dataclass(frozen=True)
class _TierWindow:
    """The paid top-tier window of a workspace's integration state (naive UTC)."""

    revision: int
    valid_from: datetime | None
    valid_until: datetime | None


@dataclass(frozen=True)
class _LicenseWindow:
    """One service licence's window (naive UTC)."""

    valid_from: datetime
    valid_until: datetime


def _decide_access(
    plan_name: str | None,
    tier: _TierWindow | None,
    license: _LicenseWindow | None,
    now: datetime,
) -> PaidIntegrationAccess:
    """The one rule that decides whether a service is in force (#3010).

    The workspace is on the top tier with an open tier window, the licence's
    window is open, and ``now`` is inside both; the earlier end wins. Used by
    every admission (``_access``) and by the display read
    ``inspect_workspace_services``.
    """
    if tier is None:
        return PaidIntegrationAccess(False, "not_entitled")
    if plan_name != "promax" or tier.valid_from is None or tier.valid_until is None:
        return PaidIntegrationAccess(False, "max_required", tier.revision)
    if license is None:
        return PaidIntegrationAccess(False, "not_entitled", tier.revision)
    if now < max(tier.valid_from, license.valid_from):
        return PaidIntegrationAccess(False, "not_started", tier.revision)
    end = min(tier.valid_until, license.valid_until)
    if now >= end:
        return PaidIntegrationAccess(False, "expired", tier.revision)
    return PaidIntegrationAccess(True, "active", tier.revision, end)


async def _access(
    db: AsyncSession, workspace_id: UUID, service_id: str, *, lock: bool
) -> PaidIntegrationAccess:
    workspace = await _workspace(db, workspace_id, lock=lock)
    if workspace is None or workspace.deleted_at is not None:
        return PaidIntegrationAccess(False, "unavailable")
    state = await _state(db, workspace_id)
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
    # Deliberately after lock acquisition and all DB awaits, not transaction
    # start time (which would remain stale throughout a long-running write).
    now = utcnow()
    return _decide_access(
        workspace.plan_name,
        None
        if state is None
        else _TierWindow(state.revision, state.max_valid_from, state.max_valid_until),
        None if license is None else _LicenseWindow(license.valid_from, license.valid_until),
        now,
    )


@dataclass(frozen=True)
class ServiceInForce:
    """A service licence in force now, and when its access ends (aware UTC)."""

    service_id: str
    valid_until: datetime


async def inspect_workspace_services(
    db: AsyncSession, plans: Mapping[UUID, str]
) -> dict[UUID, tuple[ServiceInForce, ...]]:
    """The services in force now for each listed workspace, for display only.

    ``plans`` maps each live workspace to its current plan name, as the caller
    read it. Two plain reads for all workspaces together (state rows, licence
    rows), no lock; the rule is ``_decide_access``. A workspace with nothing in
    force is absent. Each tuple is ordered by service id. Like
    ``inspect_paid_integration`` it never admits a write.
    """
    if not plans:
        return {}
    ids = list(plans)
    tiers = {
        row.workspace_id: _TierWindow(row.revision, row.max_valid_from, row.max_valid_until)
        for row in await db.execute(
            select(
                WorkspaceIntegrationState.workspace_id,
                WorkspaceIntegrationState.revision,
                WorkspaceIntegrationState.max_valid_from,
                WorkspaceIntegrationState.max_valid_until,
            ).where(WorkspaceIntegrationState.workspace_id.in_(ids))
        )
    }
    licenses = (
        await db.execute(
            select(
                WorkspaceIntegrationLicense.workspace_id,
                WorkspaceIntegrationLicense.service_id,
                WorkspaceIntegrationLicense.valid_from,
                WorkspaceIntegrationLicense.valid_until,
            )
            .where(WorkspaceIntegrationLicense.workspace_id.in_(ids))
            .order_by(
                WorkspaceIntegrationLicense.workspace_id,
                WorkspaceIntegrationLicense.service_id,
            )
        )
    ).all()
    now = utcnow()
    found: dict[UUID, list[ServiceInForce]] = {}
    for row in licenses:
        access = _decide_access(
            plans[row.workspace_id],
            tiers.get(row.workspace_id),
            _LicenseWindow(row.valid_from, row.valid_until),
            now,
        )
        if access.allowed and access.valid_until is not None:
            found.setdefault(row.workspace_id, []).append(
                ServiceInForce(row.service_id, access.valid_until.replace(tzinfo=UTC))
            )
    return {workspace_id: tuple(items) for workspace_id, items in found.items()}


async def inspect_paid_integration(
    db: AsyncSession, workspace_id: UUID, service_id: str
) -> PaidIntegrationAccess:
    """Inspect current eligibility for display only; it takes no lock.

    A cached inspection never admits a write: writes enter
    ``paid_integration_write`` and read-only admissions ``require_paid_integration``.
    """
    return await _access(db, workspace_id, service_id, lock=False)


async def _refuse_started_transaction(db: AsyncSession) -> None:
    """Refuse a transaction that has already written or locked a row.

    The workspace row lock must be the transaction's first write or row lock,
    so an earlier write cannot ride on the paid check's commit and a held
    ``users`` row can never wait on a workspace row (the global lock order of
    ``tests/integration/test_erasure_lock_order_db.py``). PostgreSQL assigns a
    transaction id at the first write or row lock (``FOR UPDATE`` / ``NO KEY
    UPDATE`` / ``SHARE`` / ``KEY SHARE``), a temp table or a WAL-logged
    ``nextval``; all of these are refused. An ``ACCESS EXCLUSIVE`` table lock
    assigns one only when ``wal_level`` is ``replica`` or ``logical`` (the
    default), to log the lock for standbys; under ``wal_level=minimal`` it
    assigns none. Plain reads, advisory locks, table locks below ``ACCESS
    EXCLUSIVE`` and, under ``minimal``, ``ACCESS EXCLUSIVE`` itself are not
    detected here; callers order those under the global lock order.

    Authentication bookkeeping counts as a write: ``APIKeyManager.verify_key``
    flushes ``api_keys.last_used_at`` and, for an agent-bound key,
    ``AgentRegistryService.touch_last_seen`` updates ``agents.last_seen_at``,
    each once per throttle window, in the request session without
    committing. A consumer commits that bookkeeping (``await db.commit()``)
    after authenticating and before entering, or the entry is refused for
    exactly the callers whose bookkeeping was due. The check only reads: it
    never assigns an id itself (``pg_current_xact_id()`` would).
    """
    # Sync first, so autoflush cannot flush the caller's pending changes.
    if db.new or db.dirty or db.deleted:
        raise RuntimeError(
            "Paid integration admission must be the transaction's first write or row lock"
        )
    if await db.scalar(text("SELECT pg_current_xact_id_if_assigned() IS NULL")) is not True:
        raise RuntimeError(
            "Paid integration admission must be the transaction's first write or row lock"
        )


async def _require_paid_integration_locked(
    db: AsyncSession, workspace_id: UUID, service_id: str
) -> PaidIntegrationAccess:
    result = await _access(db, workspace_id, service_id, lock=True)
    if not result.allowed:
        raise AuthorizationError(
            "Paid integration is not available", reason="paid_integration_unavailable"
        )
    return result


async def require_paid_integration(
    db: AsyncSession, workspace_id: UUID, service_id: str
) -> PaidIntegrationAccess:
    """Admit a read-only action under the workspace row lock, held until transaction end.

    It must be the transaction's first write or row lock (earlier plain reads
    are fine); otherwise it raises ``RuntimeError`` and leaves the transaction
    to the caller. Authenticate first, then ``await db.commit()`` before this
    call: API-key authentication writes ``api_keys.last_used_at`` /
    ``agents.last_seen_at`` into the request session once per throttle window
    (see ``_refuse_started_transaction``). One admission per transaction: a second call, or a call
    inside ``paid_integration_write``, is refused.
    """
    await _refuse_started_transaction(db)
    return await _require_paid_integration_locked(db, workspace_id, service_id)


@asynccontextmanager
async def paid_integration_write(
    db: AsyncSession, workspace_id: UUID, service_id: str
) -> AsyncIterator[PaidIntegrationAccess]:
    """Own a write transaction and recheck paid rights after flush, before commit.

    Authenticate first, commit the authentication's bookkeeping
    (``await db.commit()``: API-key authentication writes
    ``api_keys.last_used_at`` / ``agents.last_seen_at`` into the request
    session once per throttle window), then enter as the transaction's first
    write or row lock: a session with pending changes, or a transaction that
    has already written or locked a row, is refused with ``RuntimeError``
    before anything is registered or rolled back (see
    ``_refuse_started_transaction``; earlier plain reads are fine). Then verify mutable role/installation/target
    authority under the held lock. The caller must hold any further
    authority-row locks through this helper's commit. No caller-controlled
    commit, rollback or nested transaction is allowed. Cancellation, expiry
    and errors roll back the batch.
    """
    if db.new or db.dirty or db.deleted or db.info.get("paid_integration_write"):
        raise RuntimeError("Paid integration guard requires a clean, unguarded session")
    # Outside the try: a refusal neither rolls back the caller's transaction
    # nor pops an outer guard's flag.
    await _refuse_started_transaction(db)
    may_commit = False

    def only_guard_commits(_session: Session) -> None:
        if not may_commit:
            raise RuntimeError("Paid integration guard owns the commit")

    db.info["paid_integration_write"] = True
    event.listen(db.sync_session, "before_commit", only_guard_commits)
    try:
        access = await _require_paid_integration_locked(db, workspace_id, service_id)
        transaction = db.get_transaction()
        yield access
        if db.get_transaction() is not transaction:
            raise RuntimeError("Paid integration transaction changed inside the guard")
        await db.flush()
        await _require_paid_integration_locked(db, workspace_id, service_id)
        may_commit = True
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    finally:
        event.remove(db.sync_session, "before_commit", only_guard_commits)
        db.info.pop("paid_integration_write", None)
