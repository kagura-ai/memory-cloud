"""How an OAuth account was admitted is recorded once, on real rows (#2803).

The real ``SignupGateService.decide`` and ``RoleManager.ensure_user`` against
Postgres:

- each OAuth admission stores its ``admitted_via`` and, for a beta invite,
  the invite id, the issuer and (user-minted only) the inviter: an invite
  link (user-minted; admin-minted in ``SYSTEM_ADMIN_MODE=service``), a manual
  allowlist row and a promoted pending Google row (gate on and off), a
  workspace invitation (gate on and off), open registration (an invite token
  presented alongside stays unredeemed), the first user (gate on and off,
  private schema) and two concurrent first sign-ups (one ``first_user``, one
  ``unknown``);
- a returning invitee after an ``email_in_use`` failure, and a second tab
  of the same redemption, both record the invite;
- nothing rewrites the record: a later login, the race retry, an OAuth
  identity linked to an existing e-mail account;
- removing the allowlist row, or the invite (by deleting its inviter), keeps
  ``admitted_via``, the issuer and the invite id.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from auth.admission import AdmissionRecord
from auth.roles import FIRST_ACCOUNT_LOCK_KEY, RoleManager
from config.settings import get_settings
from db.base import Base
from models.auth import (
    AuditLog,
    User,
    UserOAuthProvider,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.beta_invite_service import BetaInviteService
from services.signup_gate_service import SignupGateService
from tests.integration.test_first_account_race_db import _WAITERS, _get_db_on, _tables
from utils.datetime import utcnow
from utils.exceptions import ConflictError
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

LABEL = "SENTINEL-LABEL-2803-oauth"
PROVIDERS = ["google", "github"]


def _addr(prefix: str = "adm") -> str:
    return f"{prefix}-{uuid4().hex[:10]}@example.test"


def _sub(provider: str = "google") -> str:
    return f"{provider[:2]}-{uuid4().hex[:12]}"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _fresh_get_db(maker: async_sessionmaker[AsyncSession]):
    async def _fake():
        async with maker() as s:
            yield s

    return patch("db.base.get_db", new=_fake)


@dataclass
class _World:
    db: AsyncSession
    maker: async_sessionmaker[AsyncSession]
    inviter: str = ""
    admin: str = ""
    owner: str = ""
    workspace_id: UUID | None = None
    users: list[str] = field(default_factory=list)

    async def person(self, *, role: str = "user", email: str | None = None, **cols) -> str:
        user_id = cols.pop("user_id", f"p-{uuid4().hex[:10]}")
        self.db.add(
            User(
                email=email or _addr("person"),
                user_id=user_id,
                role=role,
                auth_method=cols.pop("auth_method", "oauth"),
                auth_provider=cols.pop("auth_provider", "google"),
                **cols,
            )
        )
        await self.db.commit()
        self.users.append(user_id)
        return user_id

    async def gate(self, *, enabled: bool) -> None:
        await SignupGateService(self.db).update_config(enabled=enabled, mode="manual")

    async def invite(self, inviter: str | None = None) -> tuple[UUID, str]:
        token = uuid4().hex + uuid4().hex
        invite = BetaInvite(
            token_hash=sha256_hex(token),
            inviter_user_id=inviter or self.inviter,
            expires_at=utcnow() + timedelta(days=7),
            label=LABEL,
        )
        self.db.add(invite)
        await self.db.commit()
        return invite.id, token

    async def allow(self, provider: str, subject_id: str, label: str) -> None:
        await SignupGateService(self.db).add_to_allowlist_entry(
            provider=provider,  # type: ignore[arg-type]
            subject_id=subject_id,
            subject_label=label,
            added_by_user_id=self.admin,
            github_user_id=subject_id if provider == "github" else None,
            github_username=label if provider == "github" else None,
        )

    async def invitation(self, email: str) -> None:
        self.db.add(
            WorkspaceInvitation(
                workspace_id=self.workspace_id,
                token=uuid4().hex + uuid4().hex,
                email=email,
                role="member",
                invited_by=self.owner,
                expires_at=utcnow() + timedelta(days=7),
            )
        )
        await self.db.commit()

    async def decide(
        self,
        *,
        sub: str,
        email: str,
        provider: str = "google",
        token: str | None = None,
        verified: bool = True,
    ):
        async with self.maker() as s:
            return await SignupGateService(s).decide(
                provider=provider,  # type: ignore[arg-type]
                oauth_sub=sub,
                email=email,
                username=email,
                ip_address="203.0.113.7",
                user_agent="pytest",
                beta_invite_token_hash=sha256_hex(token) if token else None,
                email_verified=verified,
            )

    async def create(self, *, sub: str, email: str, admission, provider: str = "google"):
        self.users.append(sub)
        with (
            _fresh_get_db(self.maker),
            patch("services.security_notification_service.spawn_security_notification"),
        ):
            return await RoleManager(use_postgres=True).ensure_user(
                email=email,
                user_id=sub,
                auth_provider=provider,
                email_verified=True,
                email_authoritative=provider == "google",
                admission=admission,
            )

    async def record(self, user_id: str) -> tuple:
        row = (
            await self.db.execute(
                select(
                    User.admitted_via,
                    User.admitted_beta_invite_id,
                    User.admitted_invite_issuer,
                    User.admitted_by_user_id,
                )
                .where(User.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).one()
        return tuple(row)


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession, maker, monkeypatch) -> AsyncIterator[_World]:
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_beta_invites", True)
    monkeypatch.setattr(settings, "allow_registration", False)
    monkeypatch.setattr(settings, "system_admin_mode", "session")
    w = _World(db=db_session, maker=maker)
    w.inviter = await w.person()
    w.admin = await w.person(role="admin")
    w.owner = await w.person()
    ws = Workspace(id=uuid4(), name=f"ws-{uuid4().hex[:8]}", owner_user_id=w.owner, plan_name="pro")
    db_session.add(ws)
    await db_session.flush()
    ws_id = ws.id
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=w.owner, role="owner"))
    await db_session.commit()
    w.workspace_id = ws.id
    config = await SignupGateService(db_session).get_config()
    previous = (config.enabled, config.mode)
    yield w
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.execute(delete(Workspace).where(Workspace.id == ws_id))
    await db_session.execute(
        delete(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id.in_(w.users))
    )
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id.in_(w.users)))
    await db_session.execute(delete(User).where(User.user_id.in_(w.users)))
    await db_session.commit()


async def _redeemed(db: AsyncSession, invite_id: UUID) -> bool:
    redeemed_at = await db.scalar(
        select(BetaInvite.redeemed_at)
        .where(BetaInvite.id == invite_id)
        .execution_options(populate_existing=True)
    )
    return redeemed_at is not None


class TestEachOAuthAdmissionIsRecorded:
    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_user_minted_invite_link(self, world, provider) -> None:
        await world.gate(enabled=True)
        invite_id, token = await world.invite()
        sub, email = _sub(provider), _addr()

        gate = await world.decide(sub=sub, email=email, provider=provider, token=token)

        assert gate == AdmissionRecord("beta_invite", invite_id, world.inviter)
        await world.create(sub=sub, email=email, admission=gate, provider=provider)
        assert await world.record(sub) == ("beta_invite", invite_id, "user", world.inviter)

    async def test_admin_minted_invite_in_service_mode(self, world, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", "service")
        await world.gate(enabled=True)
        invite_id, token = await world.invite(world.admin)
        sub, email = _sub(), _addr()

        gate = await world.decide(sub=sub, email=email, token=token)
        await world.create(sub=sub, email=email, admission=gate)

        assert await world.record(sub) == ("beta_invite", invite_id, "admin", None)

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_manual_allowlist(self, world, enabled, provider) -> None:
        await world.gate(enabled=enabled)
        sub, email = _sub(provider), _addr()
        await world.allow(provider, sub, email)

        gate = await world.decide(sub=sub, email=email, provider=provider)

        assert gate == AdmissionRecord("allowlist")
        await world.create(sub=sub, email=email, admission=gate, provider=provider)
        assert await world.record(sub) == ("allowlist", None, None, None)

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_promoted_pending_google_row(self, world, enabled) -> None:
        await world.gate(enabled=enabled)
        sub, email = _sub(), _addr()
        await world.allow("google", f"pending:{email}", email)

        gate = await world.decide(sub=sub, email=email)

        assert gate == AdmissionRecord("allowlist")
        await world.create(sub=sub, email=email, admission=gate)
        assert await world.record(sub) == ("allowlist", None, None, None)

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("provider", PROVIDERS)
    async def test_workspace_invitation(self, world, enabled, provider) -> None:
        await world.gate(enabled=enabled)
        sub, email = _sub(provider), _addr()
        await world.invitation(email)

        gate = await world.decide(sub=sub, email=email, provider=provider)

        assert gate == AdmissionRecord("workspace_invitation")
        await world.create(sub=sub, email=email, admission=gate, provider=provider)
        assert await world.record(sub) == ("workspace_invitation", None, None, None)

    async def test_open_registration_leaves_an_invite_unredeemed(self, world, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "allow_registration", True)
        await world.gate(enabled=False)
        invite_id, token = await world.invite()
        sub, email = _sub(), _addr()

        gate = await world.decide(sub=sub, email=email, token=token)

        assert gate == AdmissionRecord("open_registration")
        await world.create(sub=sub, email=email, admission=gate)
        assert await world.record(sub) == ("open_registration", None, None, None)
        assert not await _redeemed(world.db, invite_id)

    async def test_existing_account_is_a_login(self, world) -> None:
        await world.gate(enabled=True)
        email = _addr()
        sub = await world.person(email=email)

        assert await world.decide(sub=sub, email=email) is None

    async def test_blocked_is_a_redirect(self, world) -> None:
        await world.gate(enabled=True)

        gate = await world.decide(sub=_sub(), email=_addr())

        assert isinstance(gate, RedirectResponse)


class TestReturningInvitee:
    async def test_after_email_in_use_the_invite_is_recorded(self, world) -> None:
        await world.gate(enabled=True)
        invite_id, token = await world.invite()
        sub, first_email = _sub(), _addr("first")

        first = await world.decide(sub=sub, email=first_email, token=token)
        assert first == AdmissionRecord("beta_invite", invite_id, world.inviter)
        # Another account takes the address before the row is inserted.
        await world.person(email=first_email, auth_provider="github")
        with pytest.raises(ConflictError):
            await world.create(sub=sub, email=first_email, admission=first)

        # Back with another address: the redeemed invite's allowlist row admits.
        email = _addr()
        again = await world.decide(sub=sub, email=email)
        assert again == AdmissionRecord("beta_invite", invite_id, world.inviter)
        await world.create(sub=sub, email=email, admission=again)
        assert await world.record(sub) == ("beta_invite", invite_id, "user", world.inviter)

    async def test_second_tab_of_the_same_redemption(self, world, monkeypatch) -> None:
        await world.gate(enabled=True)
        invite_id, token = await world.invite()
        sub, email = _sub(), _addr()
        async with world.maker() as s:
            stale = await BetaInviteService(s).find_redeemable(sha256_hex(token), utcnow())
        assert stale is not None

        first = await world.decide(sub=sub, email=email, token=token)
        # The other tab read the invite before the first committed: its
        # allowlist insert collides with the winning row and answers from it.
        monkeypatch.setattr(BetaInviteService, "find_redeemable", AsyncMock(return_value=stale))
        async with world.maker() as s:
            second = await SignupGateService(s)._redeem_beta_invite(
                token_hash=sha256_hex(token),
                provider="google",
                oauth_sub=sub,
                email=email,
                username=email,
                ip_address=None,
                user_agent=None,
            )

        expected = AdmissionRecord("beta_invite", invite_id, world.inviter)
        assert first == second == expected
        await world.create(sub=sub, email=email, admission=second)
        assert await world.record(sub) == ("beta_invite", invite_id, "user", world.inviter)

    async def test_invite_gone_still_records_beta_invite(self, world) -> None:
        await world.gate(enabled=True)
        gone_inviter = await world.person()
        invite_id, token = await world.invite(gone_inviter)
        sub, email = _sub(), _addr()
        assert await world.decide(sub=sub, email=email, token=token)
        await world.db.execute(delete(User).where(User.user_id == gone_inviter))
        await world.db.commit()

        gate = await world.decide(sub=sub, email=email)

        assert gate == AdmissionRecord("beta_invite")
        await world.create(sub=sub, email=email, admission=gate)
        assert await world.record(sub) == ("beta_invite", None, None, None)


class TestTheRecordIsNeverRewritten:
    async def test_a_later_login(self, world) -> None:
        await world.gate(enabled=True)
        invite_id, token = await world.invite()
        sub, email = _sub(), _addr()
        await world.create(
            sub=sub, email=email, admission=await world.decide(sub=sub, email=email, token=token)
        )

        await world.create(sub=sub, email=email, admission=None)
        await world.create(sub=sub, email=email, admission=AdmissionRecord("allowlist"))

        assert await world.record(sub) == ("beta_invite", invite_id, "user", world.inviter)

    async def test_the_race_retry(self, world) -> None:
        """A concurrent first sign-in inserts the row between the lookup and the
        insert: the retry signs in to that row and writes no admission."""
        sub, email = _sub(), _addr()
        world.users.append(sub)
        real = RoleManager._link_or_refuse_address_holder

        async def _concurrent_insert(self, **kwargs):
            async with world.maker() as other:
                other.add(
                    User(
                        email=email,
                        user_id=sub,
                        role="user",
                        auth_method="oauth",
                        auth_provider="google",
                        admitted_via="workspace_invitation",
                    )
                )
                await other.flush()
                other.add(UserOAuthProvider(user_id=sub, provider="google", oauth_sub=sub))
                await other.commit()
            return await real(self, **kwargs)

        with patch.object(RoleManager, "_link_or_refuse_address_holder", _concurrent_insert):
            await world.create(sub=sub, email=email, admission=AdmissionRecord("allowlist"))

        assert await world.record(sub) == ("workspace_invitation", None, None, None)

    async def test_an_identity_linked_to_an_email_account(self, world) -> None:
        email = _addr("linked")
        owner = await world.person(
            email=email,
            user_id=f"email:{uuid4()}",
            auth_method="password",
            auth_provider=None,
            password_hash="not-a-real-hash",
            email_verified_at=utcnow(),
            admitted_via="workspace_invitation",
        )
        sub = _sub()

        await world.create(sub=sub, email=email, admission=AdmissionRecord("allowlist"))

        assert await world.record(owner) == ("workspace_invitation", None, None, None)
        rows = (await world.db.execute(select(User.user_id).where(User.email == email))).all()
        assert [r[0] for r in rows] == [owner]
        linked = await world.db.scalar(
            select(UserOAuthProvider.user_id).where(UserOAuthProvider.oauth_sub == sub)
        )
        assert linked == owner

    async def test_allowlist_row_removed_or_invite_deleted(self, world) -> None:
        await world.gate(enabled=True)
        short_lived = await world.person()
        invite_id, token = await world.invite(short_lived)
        sub, email = _sub(), _addr()
        await world.create(
            sub=sub, email=email, admission=await world.decide(sub=sub, email=email, token=token)
        )
        assert await world.record(sub) == ("beta_invite", invite_id, "user", short_lived)

        await world.db.execute(
            delete(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id == sub)
        )
        await world.db.commit()
        assert await world.record(sub) == ("beta_invite", invite_id, "user", short_lived)

        # Deleting the inviter cascades the invite away; only the link clears.
        await world.db.execute(delete(User).where(User.user_id == short_lived))
        await world.db.commit()
        assert await world.db.get(BetaInvite, invite_id) is None
        assert await world.record(sub) == ("beta_invite", invite_id, "user", None)


# ---------------------------------------------------------------------------
# First user: an empty users table, in a private schema.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def empty_maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = "admission_" + uuid4().hex
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
    )
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema, "timezone": "UTC"}},
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
            await conn.execute(
                SignupGateConfig.__table__.insert().values(id=1, enabled=False, mode="manual")
            )
    except (OSError, ConnectionError, TimeoutError) as exc:
        await engine.dispose()
        if os.environ.get("REQUIRE_TEST_DATABASE") == "1":
            pytest.fail(f"PostgreSQL required: {type(exc).__name__}")
        pytest.skip("PostgreSQL test server unavailable")
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


async def _first_decide(maker, *, sub: str, email: str):
    async with maker() as s:
        return await SignupGateService(s).decide(
            provider="google",
            oauth_sub=sub,
            email=email,
            username=email,
            email_verified=True,
        )


async def _first_create(sub: str, email: str, admission) -> None:
    await RoleManager(use_postgres=True).ensure_user(
        email=email,
        user_id=sub,
        auth_provider="google",
        email_verified=True,
        admission=admission,
    )


async def _records(maker) -> list[tuple[str, str]]:
    async with maker() as s:
        rows = await s.execute(select(User.user_id, User.admitted_via).order_by(User.id))
        return [(r[0], r[1]) for r in rows]


@pytest.mark.parametrize("enabled", [True, False])
async def test_first_user(empty_maker, enabled, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
    monkeypatch.setattr(get_settings(), "allow_registration", False)
    async with empty_maker() as s:
        await SignupGateService(s).update_config(enabled=enabled, mode="manual")
    sub, email = _sub(), _addr()

    gate = await _first_decide(empty_maker, sub=sub, email=email)

    assert gate == AdmissionRecord("first_user")
    with _get_db_on(empty_maker):
        await _first_create(sub, email, gate)
    assert await _records(empty_maker) == [(sub, "first_user")]


async def test_two_concurrent_first_sign_ups_in_service_mode(empty_maker, monkeypatch) -> None:
    """Both pass the gate as the first user; the account-creating transaction
    decides again under the first-account lock: one first_user, one unknown."""
    monkeypatch.setattr(get_settings(), "system_admin_mode", "service")
    monkeypatch.setattr(get_settings(), "allow_registration", False)
    a, b = (_sub(), _addr("a")), (_sub(), _addr("b"))
    gates = [await _first_decide(empty_maker, sub=s, email=e) for s, e in (a, b)]
    assert gates == [AdmissionRecord("first_user"), AdmissionRecord("first_user")]

    engine = empty_maker.kw["bind"]
    tasks: list[asyncio.Task] = []
    async with engine.connect() as holder, engine.connect() as poller:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": FIRST_ACCOUNT_LOCK_KEY},
        )
        try:
            with _get_db_on(empty_maker):
                tasks = [
                    asyncio.ensure_future(_first_create(s, e, g))
                    for (s, e), g in zip((a, b), gates, strict=True)
                ]
                async with asyncio.timeout(10):
                    while (
                        len((await poller.execute(_WAITERS, {"key": FIRST_ACCOUNT_LOCK_KEY})).all())
                        < 2
                    ):
                        assert not any(t.done() for t in tasks), [
                            t.result() for t in tasks if t.done()
                        ]
                        await poller.rollback()
                        await asyncio.sleep(0.05)
                await holder.rollback()
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        finally:
            await holder.rollback()
            for t in tasks:
                if not t.done():
                    t.cancel()

    assert not any(isinstance(r, BaseException) for r in results), results
    assert sorted(via for _, via in await _records(empty_maker)) == ["first_user", "unknown"]
