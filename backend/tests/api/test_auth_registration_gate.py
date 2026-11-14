"""``_check_registration_allowed``: the invited-user bypass of a closed signup gate.

With registration closed, a new address passes the gate when a pending
invitation is addressed to it and the provider reports the address verified
(#2785: the signup gate then hands over its invitation admission, which records
``signup_admitted``). An email-less invitation in some workspace is not
addressed to anyone, so it lets no address through. Runs against a real
``db_session``.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from api.routes.auth import _check_registration_allowed
from models.auth import AuditLog, User, Workspace, WorkspaceInvitation
from services.signup_gate_service import SignupGateService
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


def _admission(db, email: str, sub: str):
    async def admit() -> bool:
        return await SignupGateService(db)._admit_by_workspace_invitation(
            provider="github", oauth_sub=sub, email=email, ip_address=None, user_agent=None
        )

    return admit


async def test_invitation_addressed_to_the_email_passes_the_gate(db_session):
    email = f"invited-{uuid.uuid4()}@example.com"
    sub = f"gh-{uuid.uuid4().hex[:12]}"
    invitation = await _workspace_with_invitation(db_session, email.upper())
    workspace_id, owner_id = invitation.workspace_id, invitation.invited_by
    try:
        assert (
            await _check_registration_allowed(
                email,
                db_session,
                user_id=sub,
                admit_by_invitation=_admission(db_session, email, sub),
            )
            # #2803: the admission the new account records.
            == "workspace_invitation"
        )
        rows = list(
            await db_session.scalars(select(AuditLog.action).where(AuditLog.user_id == sub))
        )
        assert rows == ["signup_admitted"]
    finally:
        # The admission commits its record (and the seeded rows with it).
        await db_session.rollback()
        await db_session.execute(delete(AuditLog).where(AuditLog.user_id == sub))
        await db_session.execute(
            delete(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace_id)
        )
        await db_session.execute(delete(Workspace).where(Workspace.id == workspace_id))
        await db_session.execute(delete(User).where(User.user_id == owner_id))
        await db_session.commit()


async def test_without_the_gate_admission_an_invitation_admits_nobody(db_session):
    """No ``admit_by_invitation`` = the provider did not report the address verified."""
    email = f"invited-{uuid.uuid4()}@example.com"
    await _workspace_with_invitation(db_session, email)

    blocked = await _check_registration_allowed(email, db_session)

    assert isinstance(blocked, RedirectResponse)
    assert "error=registration_disabled" in blocked.headers["location"]


async def test_email_less_invitation_does_not_pass_an_unrelated_address(db_session):
    await _workspace_with_invitation(db_session, None)
    email = f"y-{uuid.uuid4()}@example.com"

    blocked = await _check_registration_allowed(
        email, db_session, admit_by_invitation=_admission(db_session, email, "gh-none")
    )

    assert isinstance(blocked, RedirectResponse)
    assert blocked.status_code == 303
    assert "error=registration_disabled" in blocked.headers["location"]


async def test_plan_blocked_invitation_does_not_admit_a_new_address(db_session):
    from sqlalchemy import select

    email = f"invited-{uuid.uuid4()}@example.com"
    invite = await _workspace_with_invitation(db_session, email)
    workspace = await db_session.scalar(
        select(Workspace).where(Workspace.id == invite.workspace_id)
    )
    workspace.plan_name = "free"
    await db_session.flush()

    blocked = await _check_registration_allowed(
        email, db_session, admit_by_invitation=_admission(db_session, email, "gh-none")
    )
    assert isinstance(blocked, RedirectResponse)
    assert "error=registration_disabled" in blocked.headers["location"]


async def test_each_admission_is_answered_and_a_login_is_none(db_session, monkeypatch):
    """#2803: the first account, ``ALLOW_REGISTRATION`` and a login, by value."""
    email = f"open-{uuid.uuid4()}@example.com"
    empty = not await db_session.scalar(select(User.id).limit(1))
    if empty:
        assert await _check_registration_allowed(email, db_session) == "first_user"
    seeded = User(
        email=f"seed-{uuid.uuid4()}@example.com", user_id=f"s-{uuid.uuid4()}", role="user"
    )
    db_session.add(seeded)
    await db_session.flush()
    try:
        monkeypatch.setattr(
            "config.settings.get_settings",
            lambda: SimpleNamespace(allow_registration=True, audit_hmac_key="test-hmac-key"),
        )
        assert await _check_registration_allowed(email, db_session) == "open_registration"
        assert await _check_registration_allowed(seeded.email, db_session) is None
    finally:
        await db_session.rollback()
