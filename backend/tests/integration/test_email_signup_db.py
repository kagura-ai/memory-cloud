"""Email + password sign-up against real Postgres (#1734).

What only real transactions show:

- the whole request -> preview -> complete flow, the stored row (hash only,
  normalized address), the created account and what it carries;
- closed registration creates no account, and a link whose admission went
  away is spent with nothing else written;
- a beta invite is redeemed exactly once (also when two sign-ups race it), a
  revoked one admits nobody;
- a failure after admission rolls everything back: invite unredeemed, link
  still usable;
- one account per address: case variants, the "account exists" notice only to
  a verified address, an OAuth sign-up racing an email completion;
- the Google auto-link (and its refusals) with exactly one users row, the
  blocking marker written by unlink and by a reset that unlinked;
- cleanup, erasure labels, the CLI admin predicate.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.email_signup_service as svc_module
from auth.roles import RoleManager
from config.settings import get_settings
from models.auth import AuditLog, EmailSignupRequest, User, UserOAuthProvider
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from models.terms import TermsAcceptance
from services.beta_invite_service import BetaInviteService
from services.email_signup_service import (
    AccountExistsNotice,
    EmailSignupService,
    PendingSignupEmail,
    SignupConsents,
)
from services.signup_gate_service import SignupGateService
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    PasswordLinkInvalidError,
    ProviderLinkConfirmable,
    SignupAccountExistsError,
    SignupNotAllowedError,
)
from utils.hashing import sha256_hex

PASSWORD = "Correct-Horse-Battery-42"
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _addr(prefix: str = "p", domain: str = "example.test") -> str:
    return f"{prefix}-{uuid4().hex[:10]}@{domain}"


def _token(url: str) -> str:
    return url.split("token=", 1)[1]


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def someone(db_session: AsyncSession) -> AsyncIterator[str]:
    """One account, so no test is the deployment's first account."""
    user = User(
        email=_addr("someone"),
        user_id=f"seed-{uuid4().hex[:10]}",
        role="user",
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(user)
    await db_session.commit()
    yield user.user_id


@pytest_asyncio.fixture(loop_scope="session")
async def env(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession, someone: str):
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_email_signup", True)
    monkeypatch.setattr(settings, "enable_beta_invites", True)
    monkeypatch.setattr(settings, "allow_registration", False)
    monkeypatch.setattr(settings, "terms_version", "")
    monkeypatch.setattr(svc_module, "_claim_notice_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(svc_module, "_over_daily_invite_limit", AsyncMock(return_value=False))
    config = await SignupGateService(db_session).get_config()
    previous = (config.enabled, config.mode)
    yield settings
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.commit()


async def _gate(db_session: AsyncSession, *, enabled: bool) -> None:
    await SignupGateService(db_session).update_config(enabled=enabled, mode="manual")


async def _request(maker, email: str, invite: str | None = None):
    async with maker() as s:
        return await EmailSignupService(s).request(
            email=email,
            beta_invite_token_hash=sha256_hex(invite) if invite else None,
            locale="ja",
            timezone="Asia/Tokyo",
        )


async def _complete(maker, token: str, *, terms: str | None = None, name: str | None = "N"):
    async with maker() as s:
        return await EmailSignupService(s).complete(
            raw_token=token,
            new_password=PASSWORD,
            name=name,
            consents=SignupConsents(terms_version=terms),
            ip_address="203.0.113.5",
            user_agent="pytest",
        )


async def _users_for(db_session: AsyncSession, email: str) -> int:
    db_session.expire_all()
    return int(
        await db_session.scalar(
            select(func.count()).select_from(User).where(func.lower(User.email) == email.lower())
        )
        or 0
    )


async def _mint(maker, inviter: str) -> tuple:
    async with maker() as s:
        minted = await BetaInviteService(s).create(user_id=inviter, user_email="x@example.test")
    return minted.invite.id, minted.url.rsplit("/", 1)[1]


class TestOpenRegistration:
    async def test_full_flow(self, env, maker, db_session: AsyncSession, monkeypatch) -> None:
        monkeypatch.setattr(env, "allow_registration", True)
        monkeypatch.setattr(env, "terms_version", "2026-01")
        await _gate(db_session, enabled=False)
        email = _addr("Open")

        pending = await _request(maker, email.upper().replace("@EXAMPLE.TEST", "@example.test"))
        assert isinstance(pending, PendingSignupEmail)
        token = _token(pending.verify_url)
        row = (
            await db_session.execute(
                select(EmailSignupRequest).where(EmailSignupRequest.email == email.lower())
            )
        ).scalar_one()
        assert row.token_hash == sha256_hex(token)
        assert token not in repr(vars(row))
        assert (row.locale, row.timezone) == ("ja", "Asia/Tokyo")
        assert row.expires_at - row.created_at <= timedelta(minutes=61)

        async with maker() as s:
            previewed, _ = await EmailSignupService(s).preview(token)
        assert previewed == email.lower()

        created = await _complete(maker, token, terms="2026-01")
        assert created.user_id.startswith("email:")
        assert created.admission == "open_registration"
        db_session.expire_all()
        user = (
            await db_session.execute(select(User).where(User.user_id == created.user_id))
        ).scalar_one()
        assert user.email == email.lower()
        assert (user.auth_method, user.login_id, user.auth_provider) == ("password", None, None)
        assert user.email_verified_at is not None and user.password_hash
        assert (user.role, user.is_initial_admin) == ("user", False)
        assert (user.locale, user.timezone) == ("ja", "Asia/Tokyo")
        terms = (
            await db_session.execute(
                select(TermsAcceptance).where(TermsAcceptance.user_id == created.user_id)
            )
        ).scalar_one()
        assert (terms.version, terms.source) == ("2026-01", "login")
        audit = (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.user_id == created.user_id, AuditLog.action == "user.signed_up"
                )
            )
        ).scalar_one()
        assert audit.user_email == "email-signup"
        assert email.lower() not in repr(vars(audit))
        # Single use: the row is gone, the link is dead.
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(EmailSignupRequest)
                .where(EmailSignupRequest.email == email.lower())
            )
        ) == 0
        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, token, terms="2026-01")

    async def test_newer_request_replaces_the_older_link(
        self, env, maker, db_session, monkeypatch
    ) -> None:
        monkeypatch.setattr(env, "allow_registration", True)
        await _gate(db_session, enabled=False)
        email = _addr()
        first = await _request(maker, email)
        second = await _request(maker, email)
        assert isinstance(first, PendingSignupEmail) and isinstance(second, PendingSignupEmail)
        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, _token(first.verify_url))
        await _complete(maker, _token(second.verify_url))


class TestClosedRegistration:
    async def test_request_sends_nothing_and_creates_nothing(self, env, maker, db_session) -> None:
        await _gate(db_session, enabled=False)
        email = _addr("closed")
        assert await _request(maker, email) is None
        assert await _users_for(db_session, email) == 0
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(EmailSignupRequest)
                .where(EmailSignupRequest.email == email)
            )
        ) == 0

    async def test_registration_closed_after_the_link_spends_it_and_creates_nothing(
        self, env, maker, db_session, monkeypatch
    ) -> None:
        monkeypatch.setattr(env, "allow_registration", True)
        await _gate(db_session, enabled=False)
        email = _addr("late")
        pending = await _request(maker, email)
        monkeypatch.setattr(env, "allow_registration", False)
        with pytest.raises(SignupNotAllowedError):
            await _complete(maker, _token(pending.verify_url))
        assert await _users_for(db_session, email) == 0
        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, _token(pending.verify_url))

    async def test_gate_on_ignores_open_registration(
        self, env, maker, db_session, monkeypatch
    ) -> None:
        monkeypatch.setattr(env, "allow_registration", True)
        await _gate(db_session, enabled=True)
        assert await _request(maker, _addr()) is None


class TestBetaInvite:
    async def test_redeemed_once_at_completion(self, env, maker, db_session, someone) -> None:
        await _gate(db_session, enabled=True)
        invite_id, invite = await _mint(maker, someone)
        email = _addr("invitee")
        pending = await _request(maker, email, invite)
        assert isinstance(pending, PendingSignupEmail)
        db_session.expire_all()
        assert (await db_session.get(BetaInvite, invite_id)).redeemed_at is None

        created = await _complete(maker, _token(pending.verify_url))
        assert created.admission == "beta_invite"
        db_session.expire_all()
        invite_row = await db_session.get(BetaInvite, invite_id)
        assert invite_row.redeemed_at is not None
        entry = await db_session.get(SignupAllowlistEntry, invite_row.redeemed_allowlist_entry_id)
        assert (entry.provider, entry.subject_id, entry.source) == (
            "email",
            created.user_id,
            "beta_invite",
        )
        # The inviter sees who it became.
        async with maker() as s:
            summary = await BetaInviteService(s).get_summary(someone)
        assert summary.redeemed_emails[invite_id] == email

        # The email allowlist row never admits an OAuth identity.
        async with maker() as s:
            assert (
                await SignupGateService(s)._allowlist_admission("google", created.user_id, "manual")
                is None
            )

    async def test_revoked_invite_admits_nobody(self, env, maker, db_session, someone) -> None:
        await _gate(db_session, enabled=True)
        invite_id, invite = await _mint(maker, someone)
        email = _addr("revoked")
        pending = await _request(maker, email, invite)
        await db_session.execute(
            update(BetaInvite).where(BetaInvite.id == invite_id).values(revoked_at=utcnow())
        )
        await db_session.commit()
        with pytest.raises(SignupNotAllowedError):
            await _complete(maker, _token(pending.verify_url))
        assert await _users_for(db_session, email) == 0

    async def test_two_sign_ups_racing_one_invite_create_one_account(
        self, env, maker, db_session, someone
    ) -> None:
        await _gate(db_session, enabled=True)
        _, invite = await _mint(maker, someone)
        emails = [_addr("race"), _addr("race")]
        tokens = [_token((await _request(maker, e, invite)).verify_url) for e in emails]
        results = await asyncio.gather(
            *[_complete(maker, t) for t in tokens], return_exceptions=True
        )
        assert sum(not isinstance(r, BaseException) for r in results) == 1
        assert sum(isinstance(r, SignupNotAllowedError) for r in results) == 1
        assert sum([await _users_for(db_session, e) for e in emails]) == 1

    async def test_failure_after_admission_leaves_invite_and_link(
        self, env, maker, db_session, someone, monkeypatch
    ) -> None:
        await _gate(db_session, enabled=True)
        invite_id, invite = await _mint(maker, someone)
        email = _addr("inject")
        token = _token((await _request(maker, email, invite)).verify_url)
        monkeypatch.setattr(
            svc_module, "stage_signup_consents", AsyncMock(side_effect=RuntimeError("injected"))
        )
        with pytest.raises(RuntimeError):
            await _complete(maker, token)
        db_session.expire_all()
        assert (await db_session.get(BetaInvite, invite_id)).redeemed_at is None
        assert await _users_for(db_session, email) == 0
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(EmailSignupRequest)
                .where(EmailSignupRequest.token_hash == sha256_hex(token))
            )
        ) == 1
        monkeypatch.undo()

    @pytest.mark.parametrize("column", ["revoked_at", "redeemed_at"])
    async def test_invite_revoked_or_used_after_the_check_is_never_claimed(
        self, env, maker, db_session, someone, monkeypatch, column: str
    ) -> None:
        """The guarded claim, not the pre-check, decides (revoke / redeem race)."""
        await _gate(db_session, enabled=True)
        invite_id, invite = await _mint(maker, someone)
        email = _addr("killed")
        token = _token((await _request(maker, email, invite)).verify_url)
        original = BetaInviteService.find_redeemable

        async def _then_killed(self, token_hash, now):
            found = await original(self, token_hash, now)
            # Another transaction revokes or redeems the invite between the
            # completion's pre-check and its claim.
            async with maker() as other:
                await other.execute(
                    update(BetaInvite).where(BetaInvite.id == invite_id).values({column: utcnow()})
                )
                await other.commit()
            return found

        monkeypatch.setattr(BetaInviteService, "find_redeemable", _then_killed)
        with pytest.raises(SignupNotAllowedError):
            await _complete(maker, token)
        assert await _users_for(db_session, email) == 0
        db_session.expire_all()
        assert (await db_session.get(BetaInvite, invite_id)).redeemed_allowlist_entry_id is None
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(SignupAllowlistEntry)
                .where(SignupAllowlistEntry.subject_label == email)
            )
        ) == 0

    async def test_an_invite_over_its_daily_request_limit_sends_nothing(
        self, env, maker, db_session, someone, monkeypatch
    ) -> None:
        await _gate(db_session, enabled=True)
        _, invite = await _mint(maker, someone)
        monkeypatch.setattr(svc_module, "_over_daily_invite_limit", AsyncMock(return_value=True))
        email = _addr("limited")
        assert await _request(maker, email, invite) is None
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(EmailSignupRequest)
                .where(EmailSignupRequest.email == email)
            )
        ) == 0


class TestOneAccountPerAddress:
    async def _existing(self, db_session, email: str, *, verified: bool) -> str:
        user = User(
            email=email,
            user_id=f"g-{uuid4().hex[:10]}",
            role="user",
            auth_method="oauth",
            auth_provider="google",
            email_verified_at=utcnow() if verified else None,
        )
        db_session.add(user)
        await db_session.commit()
        return user.user_id

    async def test_existing_verified_account_gets_a_notice_not_a_link(
        self, env, maker, db_session, monkeypatch
    ) -> None:
        monkeypatch.setattr(env, "allow_registration", True)
        await _gate(db_session, enabled=False)
        email = _addr("Known")
        await self._existing(db_session, email, verified=True)
        outcome = await _request(maker, email.lower())
        assert isinstance(outcome, AccountExistsNotice)
        assert outcome.to_email == email

    async def test_unverified_or_closed_gets_nothing(
        self, env, maker, db_session, monkeypatch
    ) -> None:
        await _gate(db_session, enabled=False)
        verified = _addr("v")
        await self._existing(db_session, verified, verified=True)
        assert await _request(maker, verified) is None  # registration closed
        monkeypatch.setattr(env, "allow_registration", True)
        unverified = _addr("u")
        await self._existing(db_session, unverified, verified=False)
        assert await _request(maker, unverified) is None

    async def test_account_created_after_the_link_is_a_409(
        self, env, maker, db_session, monkeypatch
    ) -> None:
        monkeypatch.setattr(env, "allow_registration", True)
        await _gate(db_session, enabled=False)
        email = _addr("after")
        token = _token((await _request(maker, email)).verify_url)
        await self._existing(db_session, email.upper(), verified=True)
        with pytest.raises(SignupAccountExistsError):
            await _complete(maker, token)
        assert await _users_for(db_session, email) == 1

    async def test_oauth_sign_up_racing_an_email_completion_leaves_one_row(
        self, env, maker, db_session, monkeypatch, async_engine
    ) -> None:
        monkeypatch.setattr(env, "allow_registration", True)
        await _gate(db_session, enabled=False)
        email = _addr("racer")
        token = _token((await _request(maker, email)).verify_url)

        async def oauth() -> object:
            with _fresh_get_db(maker):
                return await RoleManager(use_postgres=True).ensure_user(
                    email=email.upper(),
                    user_id=f"g-{uuid4().hex[:10]}",
                    auth_provider="google",
                    email_verified=True,
                    email_authoritative=False,
                )

        results = await asyncio.gather(oauth(), _complete(maker, token), return_exceptions=True)
        assert sum(not isinstance(r, BaseException) for r in results) == 1
        assert await _users_for(db_session, email) == 1


def _fresh_get_db(maker):
    @asynccontextmanager
    async def _scope():
        async with maker() as s:
            yield s

    async def _fake():
        async with _scope() as s:
            yield s

    return patch("db.base.get_db", new=_fake)


async def _email_account(maker, *, domain: str = "gmail.com") -> tuple[str, str]:
    email = _addr("auto", domain)
    settings = get_settings()
    settings_allow = settings.allow_registration
    try:
        settings.allow_registration = True
        async with maker() as s:
            pending = await EmailSignupService(s).request(
                email=email, beta_invite_token_hash=None, locale=None, timezone=None
            )
        created = await _complete(maker, _token(pending.verify_url))
    finally:
        settings.allow_registration = settings_allow
    return email, created.user_id


async def _google(maker, email: str, sub: str, *, authoritative: bool = True):
    with (
        _fresh_get_db(maker),
        patch("services.security_notification_service.spawn_security_notification"),
    ):
        return await RoleManager(use_postgres=True).ensure_user(
            email=email,
            user_id=sub,
            name="From Google",
            auth_provider="google",
            email_verified=True,
            email_authoritative=authoritative,
        )


class TestAutoLink:
    @pytest_asyncio.fixture(loop_scope="session")
    async def gate_off(self, env, db_session) -> None:
        await _gate(db_session, enabled=False)

    async def test_google_links_and_one_row_remains(self, gate_off, maker, db_session) -> None:
        email, user_id = await _email_account(maker)
        sub = f"g-{uuid4().hex[:10]}"
        await _google(maker, email.capitalize(), sub)
        db_session.expire_all()
        link = (
            await db_session.execute(
                select(UserOAuthProvider).where(
                    UserOAuthProvider.provider == "google", UserOAuthProvider.oauth_sub == sub
                )
            )
        ).scalar_one()
        assert link.user_id == user_id
        assert await _users_for(db_session, email) == 1
        user = (await db_session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        assert user.name == "N"  # never overwritten by the provider
        # Signing in again goes through the link.
        await _google(maker, email, sub)
        assert await _users_for(db_session, email) == 1

    async def test_not_authoritative_totp_admin_and_unlinked_are_refused(
        self, gate_off, maker, db_session
    ) -> None:
        email, _ = await _email_account(maker)
        # #2786: a ConflictError that may be confirmed by email; nothing linked.
        with pytest.raises(ProviderLinkConfirmable):
            await _google(maker, email, f"g-{uuid4().hex[:10]}", authoritative=False)
        assert await _users_for(db_session, email) == 1

        for column, value in (("totp_enabled", True), ("role", "admin")):
            email, user_id = await _email_account(maker)
            await db_session.execute(
                update(User).where(User.user_id == user_id).values({column: value})
            )
            await db_session.commit()
            with pytest.raises(ConflictError):
                await _google(maker, email, f"g-{uuid4().hex[:10]}")
            assert await _users_for(db_session, email) == 1

    @pytest.mark.parametrize(
        ("provider", "domain", "extra"),
        [
            # GitHub never links in v1, even for a verified Gmail / Workspace address.
            ("github", "gmail.com", {}),
            ("github", "example.test", {"hd": "example.test"}),
            # A consumer Google account on another domain is not authoritative.
            ("google", "example.test", {}),
        ],
    )
    async def test_github_and_non_authoritative_google_get_email_in_use(
        self, gate_off, maker, db_session, provider: str, domain: str, extra: dict
    ) -> None:
        """The callbacks' decision, end to end: refused, one row, no link."""
        from api.routes.auth import provider_authoritative_for_address

        email, user_id = await _email_account(maker, domain=domain)
        user_info = {"email": email, "email_verified": True, **extra}
        with (
            _fresh_get_db(maker),
            patch("services.security_notification_service.spawn_security_notification"),
            pytest.raises(ConflictError),
        ):
            await RoleManager(use_postgres=True).ensure_user(
                email=email,
                user_id=f"{provider[:2]}-{uuid4().hex[:10]}",
                name="From Provider",
                auth_provider=provider,
                email_verified=True,
                email_authoritative=provider_authoritative_for_address(provider, user_info),
            )
        assert await _users_for(db_session, email) == 1
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(UserOAuthProvider)
                .where(UserOAuthProvider.user_id == user_id)
            )
        ) == 0

    async def test_auto_link_waits_for_a_concurrent_block_and_honours_it(
        self, gate_off, maker, db_session
    ) -> None:
        """The conditions are read under the row lock an unlink or a reset holds."""
        email, user_id = await _email_account(maker)
        async with maker() as holder:
            user = (
                await holder.execute(select(User).where(User.user_id == user_id).with_for_update())
            ).scalar_one()
            user.provider_auto_link_blocked_at = utcnow()
            await holder.flush()
            sign_in = asyncio.create_task(_google(maker, email, f"g-{uuid4().hex[:10]}"))
            await asyncio.sleep(0.5)
            assert not sign_in.done()  # waiting for the row lock
            await holder.commit()
        with pytest.raises(ConflictError):
            await sign_in
        assert await _users_for(db_session, email) == 1

    async def test_unlink_blocks_the_next_auto_link(self, gate_off, maker, db_session) -> None:
        from services.account_linking_service import AccountLinkingService

        email, user_id = await _email_account(maker)
        await _google(maker, email, f"g-{uuid4().hex[:10]}")
        async with maker() as s:
            await AccountLinkingService(s).unlink(user_id=user_id, provider="google")
        db_session.expire_all()
        user = (await db_session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        assert user.provider_auto_link_blocked_at is not None
        with pytest.raises(ConflictError):
            await _google(maker, email, f"g-{uuid4().hex[:10]}")
        assert await _users_for(db_session, email) == 1

    async def test_reset_blocks_only_when_it_unlinked(self, gate_off, maker, db_session) -> None:
        from services.email_action_token_service import EmailActionTokenService
        from services.password_account_service import PasswordAccountService

        async def reset(user_id: str, email: str) -> None:
            async with maker() as s:
                issued = await EmailActionTokenService(s).issue(
                    user_id=user_id, email=email, purpose="reset_password"
                )
                await s.commit()
            async with maker() as s:
                await PasswordAccountService(s).complete_reset(
                    raw_token=issued.raw_token, new_password=PASSWORD + "x"
                )

        async def marker(user_id: str):
            db_session.expire_all()
            return (
                await db_session.execute(
                    select(User.provider_auto_link_blocked_at).where(User.user_id == user_id)
                )
            ).scalar_one()

        plain_email, plain_id = await _email_account(maker)
        await reset(plain_id, plain_email)
        assert await marker(plain_id) is None

        linked_email, linked_id = await _email_account(maker)
        await _google(maker, linked_email, f"g-{uuid4().hex[:10]}")
        await reset(linked_id, linked_email)
        assert await marker(linked_id) is not None


class TestHousekeeping:
    async def test_cleanup_deletes_expired_requests(self, env, db_session) -> None:
        from tasks.email_action_token_tasks import cleanup_email_signup_requests

        expired = EmailSignupRequest(
            email=_addr(), token_hash=sha256_hex(uuid4().hex), expires_at=utcnow() - timedelta(1)
        )
        live = EmailSignupRequest(
            email=_addr(), token_hash=sha256_hex(uuid4().hex), expires_at=utcnow() + timedelta(1)
        )
        db_session.add_all([expired, live])
        await db_session.commit()
        await cleanup_email_signup_requests(db_session)
        await db_session.commit()
        left = set(
            await db_session.scalars(
                select(EmailSignupRequest.id).where(
                    EmailSignupRequest.id.in_([expired.id, live.id])
                )
            )
        )
        assert left == {live.id}

    async def test_erasure_blanks_the_allowlist_labels(
        self, env, maker, db_session, someone
    ) -> None:
        from services.account_erasure_service import (
            ERASED_ALLOWLIST_LABEL,
            AccountErasureService,
        )

        await _gate(db_session, enabled=True)
        _, invite = await _mint(maker, someone)
        email = _addr("erase")
        created = await _complete(maker, _token((await _request(maker, email, invite)).verify_url))
        async with maker() as s:
            changed = await AccountErasureService(s)._blank_signup_allowlist_labels(created.user_id)
            await s.commit()
        assert changed == 1
        db_session.expire_all()
        entry = (
            await db_session.execute(
                select(SignupAllowlistEntry).where(
                    SignupAllowlistEntry.provider == "email",
                    SignupAllowlistEntry.subject_id == created.user_id,
                )
            )
        ).scalar_one()
        assert entry.subject_label == entry.github_username == ERASED_ALLOWLIST_LABEL

    async def test_erasure_blanks_linked_provider_rows_and_spares_others(
        self, env, maker, db_session, someone
    ) -> None:
        """The pre-existing gap: rows of the account's linked identities, too."""
        from services.account_erasure_service import (
            ERASED_ALLOWLIST_LABEL,
            AccountErasureService,
        )

        await _gate(db_session, enabled=True)
        _, invite = await _mint(maker, someone)
        email = _addr("eraselinked")
        created = await _complete(maker, _token((await _request(maker, email, invite)).verify_url))
        sub = f"g-{uuid4().hex[:10]}"
        await _google(maker, email, sub)

        def _row(subject_id: str, label: str) -> SignupAllowlistEntry:
            return SignupAllowlistEntry(
                provider="google",
                subject_id=subject_id,
                subject_label=label,
                github_user_id=f"google:{subject_id}",
                github_username=label,
                source="manual",
                state="active",
            )

        other_sub = f"g-{uuid4().hex[:10]}"
        other_label = _addr("bystander")
        db_session.add_all([_row(sub, email), _row(other_sub, other_label)])
        await db_session.commit()

        async with maker() as s:
            changed = await AccountErasureService(s)._blank_signup_allowlist_labels(created.user_id)
            await s.commit()
        assert changed == 2
        db_session.expire_all()
        labels = dict(
            (
                await db_session.execute(
                    select(
                        SignupAllowlistEntry.subject_id, SignupAllowlistEntry.subject_label
                    ).where(SignupAllowlistEntry.subject_id.in_([created.user_id, sub, other_sub]))
                )
            ).all()
        )
        assert labels == {
            created.user_id: ERASED_ALLOWLIST_LABEL,
            sub: ERASED_ALLOWLIST_LABEL,
            other_sub: other_label,
        }

    async def test_cli_password_admin_is_never_an_email_account(
        self, env, maker, db_session, monkeypatch
    ) -> None:
        from cli import delete_admin

        monkeypatch.setattr(env, "allow_registration", True)
        await _gate(db_session, enabled=False)
        email = _addr("emailadmin")
        created = await _complete(maker, _token((await _request(maker, email)).verify_url))
        await db_session.execute(
            update(User).where(User.user_id == created.user_id).values(role="admin")
        )
        login = f"cli{uuid4().hex[:6]}"
        db_session.add(
            User(
                email=f"{login}@local",
                user_id=f"local:{login}",
                role="admin",
                auth_method="password",
                login_id=login,
                password_hash="x",
            )
        )
        await db_session.commit()
        async with maker() as s:
            admin = await delete_admin._password_admin(s)
        assert admin is not None and admin.user_id == f"local:{login}"
        await db_session.execute(delete(User).where(User.user_id == f"local:{login}"))
        await db_session.commit()
