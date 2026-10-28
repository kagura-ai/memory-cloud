"""Best-effort post-commit ownership notifications shared by API and CLI."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace
from services.email_service import get_email_service
from utils.logger import get_logger

logger = get_logger(__name__)


async def notify_new_owner_best_effort(
    db: AsyncSession, workspace_id: UUID, new_owner_id: str
) -> None:
    """Email the new owner that the workspace was transferred to them (#1103).

    Best-effort: the transfer is already committed, so the WHOLE block is guarded
    — neither the email resolution queries nor the (no-raise) EmailService call
    may surface to the caller. A new owner without an email is simply skipped.
    """
    try:
        email = (
            await db.execute(select(User.email).where(User.user_id == new_owner_id))
        ).scalar_one_or_none()
        if not email:
            return
        name = (
            await db.execute(select(Workspace.name).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        await get_email_service().send_workspace_ownership_transferred(
            to_email=email,
            workspace_name=name or "your workspace",
        )
    except Exception as exc:
        # Notification is best-effort — swallow everything (resolution query or
        # provider error) so it never affects the committed transfer. Log the
        # exception TYPE only, not str(exc): a SQLAlchemy error string echoes the
        # bound parameters (here the client-supplied new_owner_id), so we keep the
        # same no-request-field-echo discipline as the Resend provider layer.
        logger.warning(
            "ownership_transfer_notify_failed",
            workspace_id=str(workspace_id),
            error_type=type(exc).__name__,
        )
