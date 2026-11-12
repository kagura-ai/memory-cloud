"""The invitation admission record lives and dies with the admission, on Postgres (#2785).

An admission by a pending workspace invitation is recorded as a
``signup_admitted`` audit row. The record is part of the admission:

- email sign-up stages it inside the account-creating transaction, so an
  ``IntegrityError`` at the final commit (an exact-case twin of the address)
  rolls the record back together with the account; the link is spent and the
  answer is "the address has an account";
- an OAuth callback commits it before anything else; when that commit fails
  the callback ends in ``oauth_failed``, no account is created and a beta
  invite presented alongside stays unredeemed.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.email_signup_service as svc_module
from api.routes import auth as auth_routes
from config.settings import get_settings
from models.auth import (
    AuditLog,
    EmailSignupRequest,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.beta_invite import BetaInvite
from models.signup_gate import SignupGateConfig
from services.beta_invite_service import BetaInviteService
from services.email_signup_service import EmailSignupService, SignupConsents
from services.signup_gate_service import SignupGateService
from utils.datetime import utcnow
from utils.exceptions import SignupAccountExistsError
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "Correct-Horse-Battery-42"


def _addr(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}@example.test"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def invited(db_session: AsyncSession, monkeypatch) -> AsyncIterator[SimpleNamespace]:
    """Gate on (manual), an owner with a pro workspace; the gate restored afterwards."""
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_email_signup", True)
    monkeypatch.setattr(settings, "enable_beta_invites", True)
    monkeypatch.setattr(settings, "beta_invite_quota_per_user", 4)
    monkeypatch.setattr(settings, "allow_registration", False)
    monkeypatch.setattr(settings, "terms_version", "")
    monkeypatch.setattr(svc_module, "_claim_notice_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(svc_module, "_over_daily_invite_limit", AsyncMock(return_value=False))
    owner = User(
        email=_addr("owner"),
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
    await db_session.commit()
    gate = SignupGateService(db_session)
    config = await gate.get_config()
    previous = (config.enabled, config.mode)
    await gate.update_config(enabled=True, mode="manual")

    async def invite(email: str) -> WorkspaceInvitation:
        invitation = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid4().hex + uuid4().hex,
            email=email,
            role="member",
            invited_by=owner.user_id,
            expires_at=utcnow() + timedelta(days=7),
        )
        db_session.add(invitation)
        await db_session.commit()
        return invitation

    yield SimpleNamespace(db=db_session, owner=owner.user_id, invite=invite)
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.commit()


async def _admissions(db: AsyncSession, resource: str, user_id: str | None = None) -> int:
    db.expire_all()
    stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "signup_admitted", AuditLog.resource == resource)
    )
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    return int(await db.scalar(stmt) or 0)


async def _users_at(db: AsyncSession, email: str) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count()).select_from(User).where(func.lower(User.email) == email.lower())
        )
        or 0
    )


class TestEmailSignUpRecordRollsBackWithTheAccount:
    async def test_integrity_error_at_commit_leaves_no_record_and_no_account(
        self, invited, maker, monkeypatch
    ) -> None:
        email = _addr("twin")
        await invited.invite(email)
        async with maker() as s:
            pending = await EmailSignupService(s).request(
                email=email, beta_invite_token_hash=None, locale=None, timezone=None
            )
        token = pending.verify_url.split("token=", 1)[1]
        before = await _admissions(invited.db, "signup_gate:email")
        created_ids: list[str] = []

        async def _stage_with_a_twin(db, *, user_id, consents) -> None:
            # An exact-case twin of the address: the unique index fails the
            # final commit, after the admission record was staged.
            created_ids.append(user_id)
            db.add(
                User(
                    email=email,
                    user_id=f"twin-{uuid4().hex[:10]}",
                    role="user",
                    auth_method="oauth",
                    auth_provider="google",
                )
            )

        monkeypatch.setattr(svc_module, "stage_signup_consents", _stage_with_a_twin)

        with pytest.raises(SignupAccountExistsError):
            async with maker() as s:
                await EmailSignupService(s).complete(
                    raw_token=token,
                    new_password=PASSWORD,
                    name=None,
                    consents=SignupConsents(terms_version=None),
                )

        (user_id,) = created_ids
        assert await _admissions(invited.db, "signup_gate:email", user_id) == 0
        assert await _admissions(invited.db, "signup_gate:email") == before
        assert await _users_at(invited.db, email) == 0
        # The link is spent, as for any "address has an account" answer.
        assert (
            await invited.db.scalar(
                select(func.count())
                .select_from(EmailSignupRequest)
                .where(EmailSignupRequest.token_hash == sha256_hex(token))
            )
        ) == 0


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {"oauth2_state:st1": "pending"}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def set(self, key: str, value: str, **_kw) -> bool:
        self.store[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


class _FakeRequest:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers = {"user-agent": "pytest"}
        self.client = MagicMock(host="203.0.113.7")


class TestOAuthRecordCommitFailure:
    async def test_ends_in_oauth_failed_and_spends_no_beta_invite(
        self, invited, maker, monkeypatch
    ) -> None:
        email = _addr("oauth-fail")
        sub = f"go-{uuid4().hex[:12]}"
        await invited.invite(email)
        async with maker() as s:
            minted = await BetaInviteService(s).create(
                user_id=invited.owner, user_email="owner@example.test"
            )
        beta_hash = sha256_hex(minted.url.rsplit("/", 1)[1])

        # The callback up to the gate: Google answers, nothing else is stubbed
        # between the gate and the database.
        manager = MagicMock()
        manager._redis = _FakeRedis()
        monkeypatch.setattr(auth_routes, "_session_manager", manager)
        idp = MagicMock()
        idp.get_user_info_web.return_value = {
            "sub": sub,
            "email": email,
            "email_verified": True,
            "name": "Invitee",
        }
        monkeypatch.setattr(auth_routes, "_oauth2_manager", idp)
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
        monkeypatch.setattr(auth_routes, "_take_beta_invite_hash", lambda _state: beta_hash)
        monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
        monkeypatch.setattr(auth_routes, "_terms_refusal", AsyncMock(return_value=None))
        ensure_user = AsyncMock()
        monkeypatch.setattr(
            auth_routes, "get_role_manager", lambda: SimpleNamespace(ensure_user=ensure_user)
        )

        real_record = SignupGateService._invitation_admission_record

        def _broken_record(self, **kwargs) -> AuditLog:
            row = real_record(self, **kwargs)
            row.resource = None  # NOT NULL: the commit of the record fails
            return row

        monkeypatch.setattr(SignupGateService, "_invitation_admission_record", _broken_record)

        async def _fresh_get_db():
            async with maker() as s:
                yield s

        with patch("services.signup_gate_service.get_db", new=_fresh_get_db):
            response = await auth_routes.google_callback(
                _FakeRequest(), code="c", state="st1", error=None, error_description=None
            )

        assert response.status_code == 303
        assert "error=oauth_failed" in response.headers["location"]
        ensure_user.assert_not_awaited()
        manager.create_session.assert_not_called()
        assert await _admissions(invited.db, "signup_gate:google", sub) == 0
        assert await _users_at(invited.db, email) == 0
        invited.db.expire_all()
        invite = await invited.db.get(BetaInvite, minted.invite.id)
        assert invite is not None and invite.redeemed_at is None
        await invited.db.execute(delete(BetaInvite).where(BetaInvite.id == minted.invite.id))
        await invited.db.commit()
