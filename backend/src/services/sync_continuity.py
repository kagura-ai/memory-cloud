"""Read-only established-target continuity and fresh retained-target consent checks."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.sync_installation import SYNC_OPERATIONS, SyncInstallation
from models.sync_installation_schemas import (
    SyncActivation,
    SyncEstablishedTargetObservation,
    SyncReconnectValidation,
    SyncReconnectValidationView,
)
from models.sync_management import SyncManagedTarget
from models.sync_mode_access_schemas import SyncModeStatus
from services import sync_installations as installations
from services.sync_authority import registered_service, sync_authority
from services.sync_feed_authority import locked, runs_enabled
from services.sync_managed_guard import target_fence
from services.sync_management import default_mode_view, management_enabled, mode_view
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException


def reconnect_enabled() -> None:
    """The switches the reconnect itself checks, in its order (#2917).

    The management router checks ``management_enabled`` and the reconnect's
    feed fence then ``runs_enabled``. Validation answers the same fixed 404
    before it reads any state, so a disabled rollout is refused before funding,
    not after.
    """
    management_enabled()
    runs_enabled()


async def _management(db: AsyncSession, row: SyncInstallation) -> SyncModeStatus:
    """Inspect the original target fence without enrolling it or changing its mode."""
    fenced = await target_fence(db, row.context_id)
    if fenced.mode is None:
        if await locked(db, SyncManagedTarget, SyncManagedTarget.installation_id == row.id):
            raise ConflictError("Retained target management changed; retry")
        return SyncModeStatus.model_validate(default_mode_view(row))
    if (
        fenced.mode.installation_id != row.id
        or fenced.mode.context_id != row.context_id
        or fenced.mode.resource_pk != row.resource_pk
        or fenced.mode.generation != row.generation
    ):
        raise ConflictError("Retained target management changed; retry")
    return SyncModeStatus.model_validate(mode_view(fenced.mode))


async def observe_established(
    db: AsyncSession, wid: UUID, body: SyncActivation
) -> SyncEstablishedTargetObservation:
    """Ignore only the historical owner/epoch for an already consumed exact binding.

    A lost first activation, fresh consent, rebound installation, paused state or
    manual mode cannot acquire provisioning/resume authority from this metadata.
    The caller must independently require its own previously confirmed target,
    current payment/right evidence and the explicit active/feed state.
    """
    async with sync_authority(db, wid, None) as authority:
        observed = utcnow()
        until = observed + timedelta(seconds=30)
        authority.deadlines.append(until)
        approval = await installations._approval(db, wid, body.authorization_id)
        installations._match_application(approval, body, None)
        service = await registered_service(db, service_id=approval.service_id)
        row = await installations._installation(db, wid)
        if row is not None and row.application_id != body.application_id:
            raise ConflictError("Workspace has a different retained synchronization approval")
        original = (
            row
            if row is not None
            and (row.authorization_id == approval.id or approval.used_at is not None)
            else None
        )
        management = await _management(db, original) if original is not None else None
        view = await installations.installation_view(db, original) if original is not None else None
        continuity = bool(
            original is not None
            and original.authorization_id == approval.id
            and approval.used_at is not None
            and approval.revoked_at is None
            and approval.service_generation == service.generation
            and approval.browser_client_id == service.browser_client_id
            and approval.service_client_id == service.service_client_id
            and approval.operations == list(SYNC_OPERATIONS)
            and original.service_id == approval.service_id
            and original.service_client_id == service.service_client_id
            and original.operations == list(SYNC_OPERATIONS)
            and view is not None
            and view.target_available
        )
        result = SyncEstablishedTargetObservation(
            contract_version="sync-established-target-observation-v1",
            workspace_id=wid,
            application_id=body.application_id,
            authorization=installations.authorization_view(approval),
            ownership_epoch=authority.workspace.ownership_epoch,
            continuity_current=continuity,
            installation=view,
            management=management,
            observed_at=observed,
            valid_until=until,
        )
        await db.commit()
        return result


async def validate_reconnect(
    db: AsyncSession, wid: UUID, body: SyncReconnectValidation
) -> SyncReconnectValidationView:
    """Inspect fresh owner consent and retained manual target before a future charge.

    No paid guard is needed for a read before funding. Actual reconnect remains
    the separate paid, current-owner operation and must recheck all conditions.
    """
    reconnect_enabled()
    async with sync_authority(db, wid, None) as authority:
        observed = utcnow()
        approval = await installations._approval(db, wid, body.authorization_id)
        installations._match_application(approval, body, None)
        service = await registered_service(db, service_id=approval.service_id)
        installations._validate_pending(approval, authority, service)
        row = await installations._installation(db, wid)
        if row is None or row.id != body.installation_id:
            raise NotFoundException("Synchronization target")
        if (
            row.application_id != approval.application_id
            or row.service_id != approval.service_id
            or row.service_client_id != service.service_client_id
            or row.operations != list(SYNC_OPERATIONS)
        ):
            raise ConflictError("Owner consent does not match the retained installation")
        management = await _management(db, row)
        if row.state != "revoked" or row.revoked_at is None or management.mode != "manual":
            raise ConflictError("Finish unlinking into manual mode before reconnecting")
        view = await installations.installation_view(db, row)
        if not view.target_available:
            raise ConflictError("The retained synchronization target is unavailable")
        until = min(observed + timedelta(seconds=30), approval.expires_at)
        authority.deadlines.append(until)
        result = SyncReconnectValidationView(
            contract_version="sync-reconnect-validation-v1",
            workspace_id=wid,
            application_id=body.application_id,
            authorization=installations.authorization_view(approval),
            ownership_epoch=authority.workspace.ownership_epoch,
            installation=view,
            management=management,
            observed_at=observed,
            valid_until=until,
        )
        await db.commit()
        return result
