"""A pending workspace invitation passes the signup gate (#2785), on real rows.

Gate on, an invitation addressed to the applicant's verified address admits a
new person — Google, GitHub and email sign-up alike — and the person then
joins the invited workspace:

- each refusal reason is refused as before, per provider: expired, accepted,
  revoked (deleted), link-only, another address, a plan without team
  invitations, a deleted workspace, an owner-role row, an unverified address;
- each admission leaves one ``signup_admitted`` row naming the invitation,
  its workspace and inviter; no address, token or URL;
- a beta invite presented alongside stays unredeemed;
- an existing account at the address never reaches the invitation (today's
  refusal, one users row);
- one pending invitation can admit a second identity at the same address
  (each admission recorded);
- gate off with registration closed: the same rule and record;
- a pending Google allowlist row is promoted for a verified address only;
- addressed invitations are listed to, and accepted by, an account whose
  address is verified; a link-only invitation is accepted by the link holder.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.email_signup_service as email_signup_module
from api.routes.invitations import get_pending_invitations
from auth.roles import RoleManager
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
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.beta_invite_service import BetaInviteService
from services.email_signup_service import EmailSignupService, PendingSignupEmail, SignupConsents
from services.invitation_service import InvitationService
from services.signup_gate_service import SignupGateService
from services.workspace_service import ensure_first_workspace
from utils.datetime import utcnow
from utils.exceptions import ConflictError, SignupNotAllowedError, ValidationError
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "Correct-Horse-Battery-42"
PROVIDERS = ["google", "github"]


def _addr(prefix: str = "inv") -> str:
    return f"{prefix}-{uuid4().hex[:10]}@example.test"


def _sub(provider: str) -> str:
    return f"{provider[:2]}-{uuid4().hex[:12]}"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@dataclass
class _World:
    db: AsyncSession
    owner: str = ""
    workspace_id: UUID | None = None
    workspaces: list[UUID] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)

    async def workspace(self, plan: str = "pro") -> UUID:
        ws = Workspace(
            id=uuid4(), name=f"ws-{uuid4().hex[:8]}", owner_user_id=self.owner, plan_name=plan
        )
        self.db.add(ws)
        await self.db.flush()
        self.db.add(WorkspaceMember(workspace_id=ws.id, user_id=self.owner, role="owner"))
        await self.db.commit()
        self.workspaces.append(ws.id)
        return ws.id

    async def invite(
        self,
        email: str | None,
        *,
        workspace_id: UUID | None = None,
        role: str = "member",
        expires_at=None,
        accepted: bool = False,
    ) -> WorkspaceInvitation:
        invitation = WorkspaceInvitation(
            workspace_id=workspace_id or self.workspace_id,
            token=uuid4().hex + uuid4().hex,
            email=email,
            role=role,
            invited_by=self.owner,
            expires_at=expires_at if expires_at is not None else utcnow() + timedelta(days=7),
            accepted_at=utcnow() if accepted else None,
        )
        self.db.add(invitation)
        await self.db.commit()
        await self.db.refresh(invitation)  # the server-generated public id
        return invitation

    async def account(self, email: str, *, verified: bool, **columns) -> str:
        user_id = columns.pop("user_id", f"acct-{uuid4().hex[:10]}")
        self.db.add(
            User(
                email=email,
                user_id=user_id,
                role=columns.pop("role", "user"),
                email_verified_at=utcnow() if verified else None,
                **columns,
            )
        )
        await self.db.commit()
        self.users.append(user_id)
        return user_id


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[_World]:
    """An inviting owner with a ``pro`` workspace; the gate restored afterwards."""
    w = _World(db=db_session)
    w.owner = await w.account(_addr("owner"), verified=True)
    w.workspace_id = await w.workspace()
    config = await SignupGateService(db_session).get_config()
    previous = (config.enabled, config.mode)
    yield w
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id.in_(w.subjects)))
    await db_session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def gate_on(world: _World) -> _World:
    await SignupGateService(world.db).update_config(enabled=True, mode="manual")
    return world


@pytest_asyncio.fixture(loop_scope="session")
async def gate_off(world: _World, monkeypatch) -> _World:
    monkeypatch.setattr(get_settings(), "allow_registration", False)
    await SignupGateService(world.db).update_config(enabled=False, mode="manual")
    return world


async def _gate(
    maker,
    w: _World,
    *,
    provider: str,
    email: str,
    verified: bool = True,
    sub: str | None = None,
    beta_hash: str | None = None,
) -> tuple[RedirectResponse | None, str]:
    """The gate as a callback runs it: its own session."""
    sub = sub or _sub(provider)
    w.subjects.append(sub)
    async with maker() as s:
        result = await SignupGateService(s).check_access(
            provider=provider,  # type: ignore[arg-type]
            oauth_sub=sub,
            email=email,
            username="octocat" if provider == "github" else email,
            ip_address="203.0.113.8",
            user_agent="pytest",
            beta_invite_token_hash=beta_hash,
            email_verified=verified,
        )
    return result, sub


async def _rows(db: AsyncSession, subject: str, action: str) -> list[AuditLog]:
    # READ COMMITTED: the statement sees what the gate's session committed;
    # nothing is expired, so the test's ORM objects stay loaded.
    return list(
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.user_id == subject, AuditLog.action == action)
            .execution_options(populate_existing=True)
        )
    )


def _fresh_get_db(maker):
    async def _fake():
        async with maker() as s:
            yield s

    return patch("db.base.get_db", new=_fake)


class TestGateOnAdmits:
    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_addressed_invitation_admits_and_is_recorded(
        self, gate_on, maker, provider
    ) -> None:
        email = _addr()
        invitation = await gate_on.invite(email.upper())

        result, sub = await _gate(maker, gate_on, provider=provider, email=email)

        assert result is None
        (row,) = await _rows(gate_on.db, sub, "signup_admitted")
        assert row.resource == f"signup_gate:{provider}"
        assert row.user_metadata == {
            "provider": provider,
            "admission": "workspace_invitation",
            "invitations": [
                {
                    "id": invitation.public_id,
                    "workspace_id": str(gate_on.workspace_id),
                    "invited_by": gate_on.owner,
                }
            ],
            "invitation_count": 1,
        }
        dumped = repr(vars(row))
        assert email not in dumped and email.upper() not in dumped
        assert invitation.token not in dumped
        assert await _rows(gate_on.db, sub, "signup_blocked") == []

    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_a_beta_invite_presented_alongside_stays_unredeemed(
        self, gate_on, maker, provider, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "enable_beta_invites", True)
        monkeypatch.setattr(get_settings(), "beta_invite_quota_per_user", 4)
        async with maker() as s:
            minted = await BetaInviteService(s).create(
                user_id=gate_on.owner, user_email="owner@example.test"
            )
        token = minted.url.rsplit("/", 1)[1]
        email = _addr()
        await gate_on.invite(email)

        result, sub = await _gate(
            maker, gate_on, provider=provider, email=email, beta_hash=sha256_hex(token)
        )

        assert result is None
        gate_on.db.expire_all()
        invite = await gate_on.db.get(BetaInvite, minted.invite.id)
        assert invite is not None and invite.redeemed_at is None
        assert await _rows(gate_on.db, sub, "beta_invite.redeemed") == []
        assert len(await _rows(gate_on.db, sub, "signup_admitted")) == 1
        await gate_on.db.execute(delete(BetaInvite).where(BetaInvite.id == minted.invite.id))
        await gate_on.db.commit()

    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_one_pending_invitation_admits_each_identity_it_meets(
        self, gate_on, maker, provider
    ) -> None:
        """Nothing is consumed: a second identity proving the same address is
        admitted too while the invitation is pending; both are recorded."""
        email = _addr()
        invitation = await gate_on.invite(email)

        first, sub_a = await _gate(maker, gate_on, provider=provider, email=email)
        second, sub_b = await _gate(maker, gate_on, provider=provider, email=email)

        assert first is None and second is None
        ids = [
            (await _rows(gate_on.db, sub, "signup_admitted"))[0].user_metadata["invitations"][0][
                "id"
            ]
            for sub in (sub_a, sub_b)
        ]
        assert ids == [invitation.public_id, invitation.public_id]


class TestGateOnRefuses:
    CASES = [
        "expired",
        "accepted",
        "revoked",
        "link_only",
        "other_address",
        "plan_blocked",
        "deleted_workspace",
        "owner_role",
        "unverified",
    ]

    @pytest.mark.parametrize("provider", PROVIDERS)
    @pytest.mark.parametrize("case", CASES)
    async def test_refused_as_before(self, gate_on, maker, provider, case) -> None:
        email = _addr()
        verified = True
        if case == "expired":
            await gate_on.invite(email, expires_at=utcnow() - timedelta(minutes=1))
        elif case == "accepted":
            await gate_on.invite(email, accepted=True)
        elif case == "revoked":
            invitation = await gate_on.invite(email)
            async with maker() as s:
                await InvitationService(s).delete_invitation(
                    invitation.public_id, gate_on.workspace_id
                )
                await s.commit()
        elif case == "link_only":
            await gate_on.invite(None)
        elif case == "other_address":
            await gate_on.invite(_addr("someone-else"))
        elif case == "plan_blocked":
            await gate_on.invite(email, workspace_id=await gate_on.workspace(plan="free"))
        elif case == "deleted_workspace":
            ws_id = await gate_on.workspace()
            await gate_on.invite(email, workspace_id=ws_id)
            await gate_on.db.execute(
                update(Workspace).where(Workspace.id == ws_id).values(deleted_at=utcnow())
            )
            await gate_on.db.commit()
        elif case == "owner_role":
            await gate_on.invite(email, role="owner")
        elif case == "unverified":
            await gate_on.invite(email)
            verified = False

        result, sub = await _gate(maker, gate_on, provider=provider, email=email, verified=verified)

        assert isinstance(result, RedirectResponse)
        assert "/signup-blocked" in result.headers["location"]
        assert len(await _rows(gate_on.db, sub, "signup_blocked")) == 1
        assert await _rows(gate_on.db, sub, "signup_admitted") == []


class TestExistingAccountNeverReachesTheInvitation:
    @pytest.mark.parametrize(
        "kind", ["github_account", "email_account_totp", "email_account_admin", "email_blocked"]
    )
    async def test_today_refusal_and_one_users_row(self, gate_on, maker, kind) -> None:
        email = _addr()
        await gate_on.invite(email)
        if kind == "github_account":
            await gate_on.account(email, verified=True, auth_method="oauth", auth_provider="github")
        else:
            await gate_on.account(
                email,
                verified=True,
                user_id=f"email:{uuid4()}",
                auth_method="password",
                password_hash="not-a-real-hash",
                totp_enabled=kind == "email_account_totp",
                role="admin" if kind == "email_account_admin" else "user",
                provider_auto_link_blocked_at=utcnow() if kind == "email_blocked" else None,
            )

        result, sub = await _gate(maker, gate_on, provider="google", email=email)

        assert result is None  # an existing account: the gate lets ensure_user decide
        assert await _rows(gate_on.db, sub, "signup_admitted") == []
        with (
            _fresh_get_db(maker),
            patch("services.security_notification_service.spawn_security_notification"),
            pytest.raises(ConflictError),
        ):
            await RoleManager(use_postgres=True).ensure_user(
                email=email,
                user_id=sub,
                auth_provider="google",
                email_verified=True,
                email_authoritative=True,
            )
        gate_on.db.expire_all()
        count = await gate_on.db.scalar(
            select(func.count()).select_from(User).where(func.lower(User.email) == email)
        )
        assert count == 1


class TestLanding:
    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_admitted_person_joins_the_invited_workspace(
        self, gate_on, maker, provider
    ) -> None:
        email = _addr()
        invitation = await gate_on.invite(email)
        result, sub = await _gate(maker, gate_on, provider=provider, email=email)
        assert result is None

        with (
            _fresh_get_db(maker),
            patch("services.security_notification_service.spawn_security_notification"),
        ):
            await RoleManager(use_postgres=True).ensure_user(
                email=email,
                user_id=sub,
                auth_provider=provider,
                email_verified=True,
                email_authoritative=False,
            )
        gate_on.users.append(sub)
        async with maker() as s:
            # The callbacks skip the personal workspace while invitations wait.
            assert await InvitationService(s).get_pending_invitations_for_email(email=email)
            workspace, member = await InvitationService(s).accept_invitation(
                token=invitation.token, user_id=sub, user_email=email
            )
            await s.commit()
        assert workspace.id == gate_on.workspace_id
        assert member.role == "member"


class TestGateOff:
    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_verified_address_admits_with_the_same_record(
        self, gate_off, maker, provider
    ) -> None:
        email = _addr()
        invitation = await gate_off.invite(email)

        result, sub = await _gate(maker, gate_off, provider=provider, email=email)

        assert result is None
        (row,) = await _rows(gate_off.db, sub, "signup_admitted")
        assert row.user_metadata["invitations"][0]["id"] == invitation.public_id

    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_unverified_address_is_refused(self, gate_off, maker, provider) -> None:
        email = _addr()
        await gate_off.invite(email)

        result, sub = await _gate(maker, gate_off, provider=provider, email=email, verified=False)

        assert isinstance(result, RedirectResponse)
        assert "error=registration_disabled" in result.headers["location"]
        assert await _rows(gate_off.db, sub, "signup_admitted") == []


class TestPendingGoogleRow:
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_promoted_for_a_verified_address_only(
        self, world, maker, monkeypatch, enabled
    ) -> None:
        monkeypatch.setattr(get_settings(), "allow_registration", False)
        await SignupGateService(world.db).update_config(enabled=enabled, mode="manual")
        email = _addr("pending")
        sentinel = f"pending:{email}"
        entry = SignupAllowlistEntry(
            provider="google",
            subject_id=sentinel,
            subject_label=email,
            github_user_id=f"google:{uuid4().hex[:20]}",
            github_username=email,
            source="manual",
            state="active",
            added_by_user_id=world.owner,
        )
        world.db.add(entry)
        await world.db.commit()
        entry_id = entry.id

        refused, _ = await _gate(maker, world, provider="google", email=email, verified=False)
        assert isinstance(refused, RedirectResponse)
        world.db.expire_all()
        assert (await world.db.get(SignupAllowlistEntry, entry_id)).subject_id == sentinel

        admitted, sub = await _gate(maker, world, provider="google", email=email)
        assert admitted is None
        world.db.expire_all()
        assert (await world.db.get(SignupAllowlistEntry, entry_id)).subject_id == sub
        await world.db.execute(
            delete(SignupAllowlistEntry).where(SignupAllowlistEntry.id == entry_id)
        )
        await world.db.commit()


class TestAddressedInvitationsNeedAVerifiedAddress:
    async def test_unverified_account_neither_lists_nor_accepts(self, world, maker) -> None:
        email = _addr()
        invitation = await world.invite(email)
        user_id = await world.account(email, verified=False)

        listed = await get_pending_invitations(
            current_user={"user_id": user_id, "email": email}, db=world.db
        )
        assert listed.count == 0
        async with maker() as s:
            with pytest.raises(ValidationError, match="verified"):
                await InvitationService(s).accept_invitation(
                    token=invitation.token, user_id=user_id, user_email=email
                )
            await s.rollback()
        world.db.expire_all()
        member = await world.db.scalar(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == world.workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
        assert member is None

    async def test_verified_account_lists_and_accepts(self, world, maker) -> None:
        email = _addr()
        invitation = await world.invite(email)
        user_id = await world.account(email, verified=True)

        listed = await get_pending_invitations(
            current_user={"user_id": user_id, "email": email}, db=world.db
        )
        assert [i.id for i in listed.pending_invitations] == [invitation.public_id]
        async with maker() as s:
            await InvitationService(s).accept_invitation(
                token=invitation.token, user_id=user_id, user_email=email
            )
            await s.commit()

    async def test_link_only_invitation_is_accepted_by_the_link_holder(self, world, maker) -> None:
        invitation = await world.invite(None)
        email = _addr()
        user_id = await world.account(email, verified=False)

        async with maker() as s:
            workspace, _member = await InvitationService(s).accept_invitation(
                token=invitation.token, user_id=user_id, user_email=email
            )
            await s.commit()
        assert workspace.id == world.workspace_id


@pytest_asyncio.fixture(loop_scope="session")
async def email_env(gate_on: _World, monkeypatch) -> _World:
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_email_signup", True)
    monkeypatch.setattr(settings, "enable_beta_invites", True)
    monkeypatch.setattr(settings, "beta_invite_quota_per_user", 4)
    monkeypatch.setattr(settings, "allow_registration", False)
    monkeypatch.setattr(settings, "terms_version", "")
    monkeypatch.setattr(email_signup_module, "_claim_notice_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(
        email_signup_module, "_over_daily_invite_limit", AsyncMock(return_value=False)
    )
    return gate_on


async def _request(maker, email: str, beta_hash: str | None = None):
    async with maker() as s:
        return await EmailSignupService(s).request(
            email=email, beta_invite_token_hash=beta_hash, locale=None, timezone=None
        )


async def _complete(maker, token: str):
    async with maker() as s:
        return await EmailSignupService(s).complete(
            raw_token=token,
            new_password=PASSWORD,
            name="Invitee",
            consents=SignupConsents(terms_version=None),
            ip_address="203.0.113.5",
            user_agent="pytest",
        )


async def _email_admissions(db: AsyncSession) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "signup_admitted",
                AuditLog.resource == "signup_gate:email",
            )
        )
        or 0
    )


class TestEmailSignUpGateOn:
    async def test_invitee_signs_up_and_joins(self, email_env, maker) -> None:
        w = email_env
        email = _addr()
        invitation = await w.invite(email)
        public_id, token = invitation.public_id, invitation.token
        admissions_before = await _email_admissions(w.db)

        pending = await _request(maker, email)
        assert isinstance(pending, PendingSignupEmail)
        # The request-time check records nothing.
        assert await _email_admissions(w.db) == admissions_before
        created = await _complete(maker, pending.verify_url.split("token=", 1)[1])
        w.subjects.append(created.user_id)

        assert created.admission == "workspace_invitation"
        (row,) = await _rows(w.db, created.user_id, "signup_admitted")
        assert row.user_email == "email-signup"
        assert row.resource == "signup_gate:email"
        assert row.user_metadata["invitations"][0] == {
            "id": public_id,
            "workspace_id": str(w.workspace_id),
            "invited_by": w.owner,
        }
        (signed_up,) = await _rows(w.db, created.user_id, "user.signed_up")
        assert signed_up.user_metadata["admission"] == "workspace_invitation"

        async with maker() as s:
            await ensure_first_workspace(s, created.user_id, email)
            await s.commit()
        w.db.expire_all()
        owned = await w.db.scalar(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.owner_user_id == created.user_id)
        )
        assert owned == 0
        async with maker() as s:
            workspace, _ = await InvitationService(s).accept_invitation(
                token=token, user_id=created.user_id, user_email=email
            )
            await s.commit()
        assert workspace.id == w.workspace_id

    async def test_beta_invite_on_the_request_stays_unredeemed(self, email_env, maker) -> None:
        w = email_env
        async with maker() as s:
            minted = await BetaInviteService(s).create(
                user_id=w.owner, user_email="owner@example.test"
            )
        beta_hash = sha256_hex(minted.url.rsplit("/", 1)[1])
        email = _addr()
        await w.invite(email)

        pending = await _request(maker, email, beta_hash)
        created = await _complete(maker, pending.verify_url.split("token=", 1)[1])
        w.subjects.append(created.user_id)

        assert created.admission == "workspace_invitation"
        w.db.expire_all()
        invite = await w.db.get(BetaInvite, minted.invite.id)
        assert invite is not None and invite.redeemed_at is None
        await w.db.execute(delete(BetaInvite).where(BetaInvite.id == minted.invite.id))
        await w.db.commit()

    async def test_invitation_gone_before_completion_without_a_beta_invite(
        self, email_env, maker
    ) -> None:
        w = email_env
        email = _addr()
        invitation = await w.invite(email)
        pending = await _request(maker, email)
        await w.db.execute(
            delete(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation.id)
        )
        await w.db.commit()

        with pytest.raises(SignupNotAllowedError):
            await _complete(maker, pending.verify_url.split("token=", 1)[1])

        w.db.expire_all()
        assert (
            await w.db.scalar(select(func.count()).select_from(User).where(User.email == email))
            == 0
        )
        assert (
            await w.db.scalar(
                select(func.count())
                .select_from(EmailSignupRequest)
                .where(EmailSignupRequest.email == email)
            )
            == 0
        )

    async def test_invitation_gone_before_completion_falls_to_the_beta_invite(
        self, email_env, maker
    ) -> None:
        w = email_env
        async with maker() as s:
            minted = await BetaInviteService(s).create(
                user_id=w.owner, user_email="owner@example.test"
            )
        beta_hash = sha256_hex(minted.url.rsplit("/", 1)[1])
        email = _addr()
        invitation = await w.invite(email)
        pending = await _request(maker, email, beta_hash)
        await w.db.execute(
            delete(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation.id)
        )
        await w.db.commit()

        created = await _complete(maker, pending.verify_url.split("token=", 1)[1])
        w.subjects.append(created.user_id)

        assert created.admission == "beta_invite"
        assert await _rows(w.db, created.user_id, "signup_admitted") == []
        w.db.expire_all()
        invite = await w.db.get(BetaInvite, minted.invite.id)
        assert invite is not None and invite.redeemed_at is not None
