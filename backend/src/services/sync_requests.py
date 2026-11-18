"""Human-requested provenance which never substitutes for worker or paid authority."""

from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal, load_login_identity
from models.auth import Workspace
from models.management_schemas import digest
from models.oidc import OIDCGrant, OIDCToken
from models.sync_activity import SyncManualRequest
from models.sync_feed import SyncFeed, SyncRun
from models.sync_installation import SyncInstallation
from services.sync_activity import activity_boundary
from services.sync_audit import add_audit, human_actor
from services.sync_credentials import CredentialAuthority
from services.sync_feed_authority import FeedFence, current_schema, lock_membership, locked
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ConflictError, MemoryCloudException

# A manual request lasts at most this long and never beyond its login (#2911).
REQUEST_LIFETIME = timedelta(minutes=15)


async def create_request(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    operation_id: UUID,
) -> dict:
    """Issue one idempotent, expiring record from a current authenticated Editor."""
    async with activity_boundary(db, wid, iid, principal, write=True) as (
        installation,
        workspace,
        fence,
    ):
        old = await db.scalar(
            select(SyncManualRequest).where(
                SyncManualRequest.installation_id == iid,
                SyncManualRequest.operation_id == operation_id,
            )
        )
        if old is not None:
            if (
                old.requested_by != principal.user_id
                or old.browser_client_id != principal.client_id
            ):
                raise ConflictError("Manual request identity changed")
            result = request_view(old)
        else:
            now = utcnow()
            pending = await db.scalar(
                select(func.count())
                .select_from(SyncManualRequest)
                .where(
                    SyncManualRequest.installation_id == iid,
                    SyncManualRequest.expires_at > now,
                )
            )
            if pending is not None and pending >= 4:
                raise ConflictError("Too many live manual requests")
            # The request is bound to the login (the OIDC grant) behind this
            # token, not to the token itself (#2911); the boundary above holds
            # the token, grant and client rows.
            login = (
                await db.execute(
                    select(OIDCToken.id, OIDCGrant.expires_at)
                    .join(OIDCGrant, OIDCGrant.id == OIDCToken.grant_id)
                    .where(OIDCToken.access_token_hash == principal.token_hash)
                )
            ).first()
            if login is None:
                raise ConflictError("Manual request authority changed")
            token_id, login_expires_at = login
            row = SyncManualRequest(
                installation_id=iid,
                operation_id=operation_id,
                requested_by=principal.user_id,
                token_id=token_id,
                browser_client_id=principal.client_id,
                source_version_hash=await source_version(db, installation),
                generation=installation.generation,
                ownership_epoch=workspace.ownership_epoch,
                created_at=now,
                expires_at=min(login_expires_at, now + REQUEST_LIFETIME),
            )
            db.add(row)
            await db.flush()
            fence.deadlines.append(row.expires_at)
            add_audit(
                db,
                "sync.request.created",
                human_actor(principal.user_id, principal.client_id),
                installation=installation,
                operation_id=operation_id,
                manual_request_id=row.id,
            )
            result = request_view(row)
        await db.flush()
        await db.commit()
    return result


def request_view(row: SyncManualRequest) -> dict:
    """Return identifiers and expiry only; the request UUID is not bearer authority."""
    return {
        "request_id": str(row.id),
        "operation_id": str(row.operation_id),
        "expires_at": to_utc_iso(row.expires_at),
        "generation": row.generation,
    }


async def validate_request(
    db: AsyncSession,
    authority: CredentialAuthority,
    request_id: UUID,
    *,
    fence: FeedFence,
) -> SyncManualRequest:
    """Validate a worker's attribution while retaining its independent execution identity."""
    return await validate_manual_request(
        db,
        authority.installation,
        authority.service.browser_client_id,
        request_id,
        fence=fence,
    )


async def validate_manual_request(
    db: AsyncSession,
    installation: SyncInstallation,
    browser_client_id: str,
    request_id: UUID,
    *,
    fence: FeedFence,
) -> SyncManualRequest:
    """Shared provenance checks for worker use and a separate human resume confirmation."""
    request = await locked(
        db,
        SyncManualRequest,
        SyncManualRequest.id == request_id,
        SyncManualRequest.installation_id == installation.id,
    )
    workspace = await db.get(Workspace, installation.workspace_id)
    if (
        request is None
        or request.requested_by is None
        or request.token_id is None
        or workspace is None
        or request.generation != installation.generation
        or request.ownership_epoch != workspace.ownership_epoch
        or utcnow() >= request.expires_at
        or request.browser_client_id != browser_client_id
    ):
        raise ConflictError("Manual request is no longer current")
    if request.source_version_hash != await source_version(db, installation):
        raise ConflictError("Manual request source changed")
    token = await locked(db, OIDCToken, OIDCToken.id == request.token_id)
    if token is None:
        raise ConflictError("Manual request is no longer current")
    try:
        # The login behind the stored token, not that token's own 300 s: an
        # expired or rotated access token alone does not end the request, a
        # revoked or expired grant does (#2911).
        live = await load_login_identity(db, token.id, "integrations:manage", lock=True)
        await lock_membership(db, live.user_id, installation, write=True)
    except ConflictError:
        raise
    except (HTTPException, MemoryCloudException):
        # Any refusal of the ORIGINAL requester (identity, role, seat, plan,
        # capacity) is one fixed conflict for every caller (#2912): the worker
        # or another Editor never sees it as a refusal of their own.
        raise ConflictError("Manual request authority changed") from None
    if (live.user_id, live.client_id) != (request.requested_by, request.browser_client_id):
        raise ConflictError("Manual request authority changed")
    fence.deadlines.extend((request.expires_at, live.expires_at))
    return request


async def source_version(db: AsyncSession, installation: SyncInstallation) -> str:
    """Bind all configured source versions and schema, including configuration ABA changes."""
    feeds = (
        await db.scalars(
            select(SyncFeed)
            .where(
                SyncFeed.installation_id == installation.id,
            )
            .order_by(SyncFeed.id)
        )
    ).all()
    schema = await current_schema(db, installation)
    return digest(
        {
            "feeds": [
                (str(feed.id), feed.kind, feed.config_hash, feed.config_version) for feed in feeds
            ],
            "schema": [schema.schema_version, digest(schema.field_definitions)] if schema else None,
        }
    )


async def validate_run_request(
    db: AsyncSession, authority: CredentialAuthority, row: SyncRun, fence: FeedFence
) -> None:
    """An expired/deleted request cannot silently turn a queued manual run into an automatic run."""
    reference = row.manifest.get("manual_request_id")
    if reference is not None:
        await validate_request(db, authority, UUID(reference), fence=fence)
