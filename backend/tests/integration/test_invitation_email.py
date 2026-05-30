"""Integration tests for the invitation courtesy-email dispatch (Issue #654).

``InvitationService.create_invitation`` sends a workspace-invitation email
after persisting the row. The email is a COURTESY notification: the invitation
row is the source of truth, so an email failure (or an unexpected raise inside
the dispatch path) must NEVER prevent the invitation from being created.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace, WorkspaceInvitation
from services.invitation_service import InvitationService


class _RecordingEmail:
    """Stub EmailService that records calls and can simulate failure modes."""

    def __init__(self, *, behavior: str = "ok") -> None:
        self.calls: list[dict] = []
        self.behavior = behavior  # "ok" | "false" | "raise"

    async def send_workspace_invitation(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        if self.behavior == "raise":
            raise RuntimeError("provider exploded")
        return self.behavior != "false"


async def _seed(db: AsyncSession) -> tuple[Workspace, User]:
    suffix = uuid4().hex[:8]
    inviter = User(
        email=f"inviter-{suffix}@example.com",
        user_id=f"inviter-sub-{suffix}",
        name="Alice Admin",
        role="user",
        auth_method="oauth",
        auth_provider="google",
    )
    db.add(inviter)
    await db.flush()

    ws = Workspace(
        id=uuid4(),
        name=f"ws-{suffix}",
        plan_name="pro",
        owner_user_id=inviter.user_id,
        daily_api_limit=50_000,
        weekly_api_limit=250_000,
    )
    db.add(ws)
    await db.flush()
    return ws, inviter


@pytest.mark.asyncio
class TestInvitationEmailDispatch:
    async def test_create_invitation_dispatches_email_with_expected_args(self, db_session):
        ws, inviter = await _seed(db_session)
        stub = _RecordingEmail()
        svc = InvitationService(db_session, email_service=stub)

        invitation = await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=inviter.user_id,
            role="admin",
            email="invitee@example.com",
            expires_in_days=7,
        )

        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["to_email"] == "invitee@example.com"
        assert call["workspace_name"] == ws.name
        assert call["inviter_name"] == "Alice Admin"
        # The accept_url is absolute and embeds the single-use token.
        assert call["accept_url"].endswith(f"/invite/{invitation.token}")
        # Expiry is Z-suffixed UTC (to_utc_iso), not naive (#489 regression).
        assert call["expires_at_iso"] is not None
        assert call["expires_at_iso"].endswith("Z")

    async def test_never_expires_passes_none_iso(self, db_session):
        ws, inviter = await _seed(db_session)
        stub = _RecordingEmail()
        svc = InvitationService(db_session, email_service=stub)

        await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=inviter.user_id,
            role="admin",
            email="invitee2@example.com",
            expires_in_days=None,
        )

        assert stub.calls[0]["expires_at_iso"] is None

    @pytest.mark.parametrize("behavior", ["raise", "false"])
    async def test_email_failure_does_not_block_invitation(self, db_session, behavior):
        """A raising OR False-returning email provider must not prevent the
        invitation row from being created (courtesy-email contract)."""
        ws, inviter = await _seed(db_session)
        stub = _RecordingEmail(behavior=behavior)
        svc = InvitationService(db_session, email_service=stub)

        invitation = await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=inviter.user_id,
            role="admin",
            email=f"invitee-{behavior}@example.com",
            expires_in_days=None,
        )

        # The dispatch was attempted...
        assert len(stub.calls) == 1
        # ...and the invitation row is present despite the email failure.
        assert invitation.id is not None
        found = (
            await db_session.execute(
                select(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation.id)
            )
        ).scalar_one_or_none()
        assert found is not None
