"""An invitee signs up by email with the gate on and joins, on Postgres (#2785 with #1734, #2761).

One flow across the milestone: the signup gate is on, a pending workspace
invitation is addressed to the person's address, and the person signs up
with email + password and ticks the announcement-email box on the completion
page. Afterwards:

- exactly one account exists for the address, with a verified address, the
  role ``user`` and the admission recorded as ``workspace_invitation``;
- the consent history holds one ``signup`` row with the answer and version;
- no personal workspace was created (the invitation stands in for it), the
  invitation is listed to the account and it accepts it.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.email_signup_service as svc_module
from api.routes.invitations import get_pending_invitations
from config.settings import get_settings
from models.announcement_consent import AnnouncementEmailConsent
from models.auth import (
    AuditLog,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.signup_gate import SignupGateConfig
from services.announcement_consent_service import (
    ANNOUNCEMENT_CONSENT_WORDING_VERSION as V,
)
from services.announcement_consent_service import resolve_signup_consent
from services.email_signup_service import EmailSignupService, PendingSignupEmail, SignupConsents
from services.invitation_service import InvitationService
from services.signup_gate_service import SignupGateService
from services.workspace_service import ensure_first_workspace
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "Correct-Horse-Battery-42"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def gate_on(db_session: AsyncSession, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_email_signup", True)
    monkeypatch.setattr(settings, "allow_registration", False)
    monkeypatch.setattr(settings, "terms_version", "")
    monkeypatch.setattr(settings, "announcement_emails_enabled", True)
    monkeypatch.setattr(svc_module, "_claim_notice_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(svc_module, "_over_daily_invite_limit", AsyncMock(return_value=False))
    gate = SignupGateService(db_session)
    config = await gate.get_config()
    previous = (config.enabled, config.mode)
    await gate.update_config(enabled=True, mode="manual")
    yield
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.commit()


async def test_invitee_signs_up_by_email_opts_in_and_joins(gate_on, maker, db_session) -> None:
    owner = User(
        email=f"owner-{uuid4().hex[:8]}@example.test",
        user_id=f"acct-{uuid4().hex[:10]}",
        role="user",
        email_verified_at=utcnow(),
    )
    db_session.add(owner)
    await db_session.flush()
    ws = Workspace(
        id=uuid4(), name=f"ws-{uuid4().hex[:8]}", owner_user_id=owner.user_id, plan_name="pro"
    )
    db_session.add(ws)
    await db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.user_id, role="owner"))
    email = f"invitee-{uuid4().hex[:10]}@example.test"
    invitation = WorkspaceInvitation(
        workspace_id=ws.id,
        token=uuid4().hex + uuid4().hex,
        email=email.upper(),
        role="member",
        invited_by=owner.user_id,
        expires_at=utcnow() + timedelta(days=7),
    )
    db_session.add(invitation)
    await db_session.commit()
    invitation_token, workspace_id = invitation.token, ws.id

    # Request -> the emailed link -> completion with the box ticked.
    async with maker() as s:
        pending = await EmailSignupService(s).request(
            email=email, beta_invite_token_hash=None, locale="en", timezone="UTC"
        )
    assert isinstance(pending, PendingSignupEmail)
    async with maker() as s:
        created = await EmailSignupService(s).complete(
            raw_token=pending.verify_url.split("token=", 1)[1],
            new_password=PASSWORD,
            name="Invitee",
            consents=SignupConsents(
                terms_version=None, announcement=resolve_signup_consent(True, V)
            ),
        )
    async with maker() as s:
        await ensure_first_workspace(s, created.user_id, created.email)
        await s.commit()

    db_session.expire_all()
    (user,) = (await db_session.scalars(select(User).where(func.lower(User.email) == email))).all()
    assert user.user_id == created.user_id
    assert user.email_verified_at is not None
    assert (user.role, user.is_initial_admin) == ("user", False)
    assert created.admission == "workspace_invitation"
    admitted = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.user_id == created.user_id, AuditLog.action == "signup_admitted")
    )
    assert admitted == 1
    consents = (
        await db_session.execute(
            select(
                AnnouncementEmailConsent.value,
                AnnouncementEmailConsent.source,
                AnnouncementEmailConsent.wording_version,
            ).where(AnnouncementEmailConsent.user_id == created.user_id)
        )
    ).all()
    assert [tuple(r) for r in consents] == [(True, "signup", V)]
    owned = await db_session.scalar(
        select(func.count()).select_from(Workspace).where(Workspace.owner_user_id == user.user_id)
    )
    assert owned == 0

    # The invitation is listed to the new account, and it accepts it.
    session_user = {"user_id": created.user_id, "email": created.email}
    async with maker() as s:
        listed = await get_pending_invitations(current_user=session_user, db=s)
    assert listed.count == 1
    async with maker() as s:
        workspace, member = await InvitationService(s).accept_invitation(
            token=invitation_token, user_id=created.user_id, user_email=created.email
        )
        await s.commit()
    assert (workspace.id, member.role) == (workspace_id, "member")
