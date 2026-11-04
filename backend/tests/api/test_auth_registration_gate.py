"""``_check_registration_allowed``: the invited-user bypass of a closed signup gate.

With registration closed, a new address passes the gate when a pending
invitation is addressed to it. An email-less invitation in some workspace is
not addressed to anyone, so it lets no address through. Runs against a real
``db_session``.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.responses import RedirectResponse

from api.routes.auth import _check_registration_allowed
from models.auth import User, Workspace, WorkspaceInvitation
from utils.datetime import utcnow


@pytest.fixture(autouse=True)
def _registration_closed(monkeypatch):
    monkeypatch.setattr(
        "config.settings.get_settings",
        lambda: SimpleNamespace(allow_registration=False, audit_hmac_key="test-hmac-key"),
    )


async def _workspace_with_invitation(db, email: str | None) -> WorkspaceInvitation:
    owner = User(
        email=f"owner-{uuid.uuid4()}@example.com",
        user_id=f"user-{uuid.uuid4()}",
        role="user",
    )
    db.add(owner)
    await db.flush()
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"ws-{uuid.uuid4()}",
        owner_user_id=owner.user_id,
        plan_name="pro",
    )
    db.add(ws)
    await db.flush()
    invitation = WorkspaceInvitation(
        workspace_id=ws.id,
        token=uuid.uuid4().hex + uuid.uuid4().hex,
        email=email,
        role="member",
        invited_by=owner.user_id,
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(invitation)
    await db.flush()
    return invitation


async def test_invitation_addressed_to_the_email_passes_the_gate(db_session):
    email = f"invited-{uuid.uuid4()}@example.com"
    await _workspace_with_invitation(db_session, email.upper())

    assert await _check_registration_allowed(email, db_session) is None


async def test_email_less_invitation_does_not_pass_an_unrelated_address(db_session):
    await _workspace_with_invitation(db_session, None)

    blocked = await _check_registration_allowed(f"y-{uuid.uuid4()}@example.com", db_session)

    assert isinstance(blocked, RedirectResponse)
    assert blocked.status_code == 303
    assert "error=registration_disabled" in blocked.headers["location"]
