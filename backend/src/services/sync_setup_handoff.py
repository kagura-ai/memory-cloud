"""Current-owner setup handoff; no payment, paid license or resource provisioning."""

from typing import cast
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from auth.sync_setup_handoff import SyncSetupHandoffSigner, SyncSetupHandoffUnavailable
from config.settings import get_settings
from models.sync_setup_handoff_schemas import SyncSetupHandoffCreate, SyncSetupHandoffView
from services.sync_audit import add_audit, human_actor
from services.sync_authority import registered_service, sync_authority


def _payment_origin(value: object) -> str:
    """Only a configured HTTPS origin can receive this short-lived credential."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or not value.isascii()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise SyncSetupHandoffUnavailable()
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or url.port == 0
        ):
            raise ValueError("Invalid fixed payment origin")
        return "https://" + url.netloc
    except ValueError:
        raise SyncSetupHandoffUnavailable() from None


async def create_setup_handoff(
    db: AsyncSession,
    workspace_id: UUID,
    principal: SyncPrincipal,
    body: SyncSetupHandoffCreate,
) -> SyncSetupHandoffView:
    """Hold canonical owner/registration fences until signing and COMMIT finish."""
    settings = get_settings()
    origin = _payment_origin(settings.payment_public_base_url)
    signer = SyncSetupHandoffSigner(settings)
    signer.validate_configuration()
    async with sync_authority(db, workspace_id, principal) as authority:
        # A provided human principal is reloaded by sync_authority, never absent.
        current = cast(SyncPrincipal, authority.principal)
        service = await registered_service(db, browser_client_id=current.client_id)
        minted = signer.mint(
            user_id=authority.owner.user_id,
            workspace_id=authority.workspace.id,
            service_id=service.service_id,
            client_id=service.browser_client_id,
            application_id=body.application_id,
            application_revision=body.application_revision,
            input_digest=body.input_digest,
            ownership_epoch=authority.workspace.ownership_epoch,
            valid_until=min(principal.expires_at, current.expires_at),
        )
        authority.deadlines.append(minted.expires_at)
        result = SyncSetupHandoffView(
            token=minted.token,
            kid=minted.kid,
            jti=minted.jti,
            expires_at=minted.expires_at,
            url=origin + "/addons/sync/enter?" + urlencode({"t": minted.token}),
        )
        add_audit(
            db,
            "sync.setup_handoff.created",
            human_actor(authority.owner.user_id, current.client_id),
            workspace_id=authority.workspace.id,
            service_id=service.service_id,
            operation_id=UUID(minted.jti),
        )
        # before_commit runs before SQLAlchemy's implicit flush. Await the audit
        # INSERT first, so expiry during that SQL is seen by the final hook.
        await db.flush()
        await db.commit()
        return result
