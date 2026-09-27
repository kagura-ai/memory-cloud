"""PasswordAccountService against real Postgres (Issue #1678).

Reset, set-up, change and remove flows; the link rules (single use, expiry,
address changed since sending); the last-method guard; that no flow creates a
user; and that neither the token nor the link nor a password reaches the logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.password import hash_password, verify_password
from models.auth import AuditLog, EmailActionToken, User, UserOAuthProvider
from services import password_account_service as password_service_module
from services.email_action_token_service import EmailActionTokenService
from services.email_service import LoggingEmailService
from services.password_account_service import PasswordAccountService, process_reset_request
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    CurrentPasswordMismatchError,
    EmailDispatchError,
    PasswordLinkInvalidError,
    PasswordSetupNotAllowedError,
    ValidationError,
)
from utils.hashing import sha256_hex

OLD = "Old-Password-123!"
NEW = "New-Password-456!"

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Made:
    def __init__(self) -> None:
        self.user_ids: list[str] = []


@pytest_asyncio.fixture(loop_scope="session")
async def made(db_session: AsyncSession) -> AsyncIterator[_Made]:
    record = _Made()
    yield record
    await db_session.rollback()
    if record.user_ids:
        await db_session.execute(delete(AuditLog).where(AuditLog.user_id.in_(record.user_ids)))
        await db_session.execute(delete(User).where(User.user_id.in_(record.user_ids)))
        await db_session.commit()


async def _user(
    db: AsyncSession,
    made: _Made,
    *,
    password: str | None = OLD,
    verified: bool = True,
    email: str | None = None,
    provider: str | None = None,
) -> User:
    uid = f"u_{uuid4().hex[:10]}"
    user = User(
        user_id=uid,
        email=email or f"{uid}@pw.example",
        name="PW",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        password_hash=hash_password(password) if password else None,
        email_verified_at=utcnow() if verified else None,
    )
    db.add(user)
    if provider:
        db.add(UserOAuthProvider(user_id=uid, provider=provider, oauth_sub=f"sub-{uid}"))
    await db.commit()
    made.user_ids.append(uid)
    return user


def _token_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


async def _user_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


def _email() -> AsyncMock:
    email = AsyncMock()
    email.send_password_reset = AsyncMock(return_value=True)
    email.send_password_setup = AsyncMock(return_value=True)
    return email


async def _reload(db: AsyncSession, user_id: str) -> User:
    db.expire_all()
    return (await db.execute(select(User).where(User.user_id == user_id))).scalar_one()


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    async def test_full_flow(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made)
        before = await _user_count(db_session)
        email = _email()
        service = PasswordAccountService(db_session, email_service=email)

        pending = await service.request_reset(email=f"  {user.email.upper()} ")
        assert pending is not None
        assert pending.to_email == user.email
        assert pending.expires_in_minutes == 30
        assert "/password/reset?token=" in pending.reset_url
        await service.send_reset_email(pending)
        email.send_password_reset.assert_awaited_once()

        user_id = await service.complete_reset(
            raw_token=_token_from(pending.reset_url), new_password=NEW
        )
        assert user_id == user.user_id
        refreshed = await _reload(db_session, user.user_id)
        assert refreshed.password_hash and verify_password(NEW, refreshed.password_hash)
        assert await _user_count(db_session) == before

    @pytest.mark.parametrize("kind", ["unknown", "unverified", "no_password", "local"])
    async def test_ineligible_addresses_get_nothing(
        self, db_session: AsyncSession, made: _Made, kind: str
    ) -> None:
        if kind == "unknown":
            address = f"nobody-{uuid4().hex[:6]}@pw.example"
        elif kind == "unverified":
            address = (await _user(db_session, made, verified=False)).email
        elif kind == "no_password":
            address = (await _user(db_session, made, password=None)).email
        else:
            address = (await _user(db_session, made, email=f"x{uuid4().hex[:6]}@local")).email
        rows_before = await db_session.scalar(select(func.count()).select_from(EmailActionToken))

        pending = await PasswordAccountService(db_session, email_service=_email()).request_reset(
            email=address
        )

        assert pending is None
        rows_after = await db_session.scalar(select(func.count()).select_from(EmailActionToken))
        assert rows_after == rows_before

    async def test_link_works_once(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made)
        service = PasswordAccountService(db_session, email_service=_email())
        pending = await service.request_reset(email=user.email)
        assert pending is not None
        token = _token_from(pending.reset_url)

        await service.complete_reset(raw_token=token, new_password=NEW)
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=token, new_password="Another-Pass-789!")

    async def test_expired_link(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made)
        service = PasswordAccountService(db_session, email_service=_email())
        pending = await service.request_reset(email=user.email)
        assert pending is not None
        token = _token_from(pending.reset_url)
        await db_session.execute(
            update(EmailActionToken)
            .where(EmailActionToken.token_hash == sha256_hex(token))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db_session.commit()

        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=token, new_password=NEW)

    async def test_policy_failure_keeps_the_link(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        service = PasswordAccountService(db_session, email_service=_email())
        pending = await service.request_reset(email=user.email)
        assert pending is not None
        token = _token_from(pending.reset_url)

        with pytest.raises(ValidationError):
            await service.complete_reset(raw_token=token, new_password="weak")
        await service.complete_reset(raw_token=token, new_password=NEW)

    async def test_link_dies_when_the_email_changed(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        service = PasswordAccountService(db_session, email_service=_email())
        pending = await service.request_reset(email=user.email)
        assert pending is not None
        await db_session.execute(
            update(User)
            .where(User.user_id == user.user_id)
            .values(email=f"moved-{uuid4().hex[:6]}@pw.example")
        )
        await db_session.commit()

        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=_token_from(pending.reset_url), new_password=NEW)

    async def test_a_newer_request_invalidates_the_older_link(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        service = PasswordAccountService(db_session, email_service=_email())
        first = await service.request_reset(email=user.email)
        second = await service.request_reset(email=user.email)
        assert first is not None and second is not None
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=_token_from(first.reset_url), new_password=NEW)
        await service.complete_reset(raw_token=_token_from(second.reset_url), new_password=NEW)

    async def test_a_setup_link_is_not_a_reset_link(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, password=None)
        email = _email()
        service = PasswordAccountService(db_session, email_service=email)
        await service.request_setup(user_id=user.user_id)
        token = _token_from(email.send_password_setup.await_args.kwargs["setup_url"])
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=token, new_password=NEW)

    async def test_send_failure_is_swallowed(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made)
        email = _email()
        email.send_password_reset = AsyncMock(side_effect=RuntimeError("boom"))
        service = PasswordAccountService(db_session, email_service=email)
        pending = await service.request_reset(email=user.email)
        assert pending is not None
        await service.send_reset_email(pending)  # does not raise


class TestBackgroundResetRequest:
    """``process_reset_request`` does all of a reset request's work after the
    response, on its own session: lookup, token, audit row, commit, email."""

    @staticmethod
    def _factory(async_engine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def test_issues_and_sends_a_link_for_an_eligible_account(
        self, async_engine, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        email = _email()

        await process_reset_request(
            email=f"  {user.email.upper()} ",
            ip_address="192.0.2.1",
            user_agent="pytest",
            session_factory=self._factory(async_engine),
            email_service=email,
        )

        email.send_password_reset.assert_awaited_once()
        kwargs = email.send_password_reset.await_args.kwargs
        assert kwargs["to_email"] == user.email
        audits = await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.user_id == user.user_id, AuditLog.action == "password_reset_requested")
        )
        assert audits == 1
        await PasswordAccountService(db_session, email_service=_email()).complete_reset(
            raw_token=_token_from(kwargs["reset_url"]), new_password=NEW
        )

    @pytest.mark.parametrize("kind", ["unknown", "unverified", "no_password", "local"])
    async def test_does_nothing_for_an_ineligible_address(
        self, async_engine, db_session: AsyncSession, made: _Made, kind: str
    ) -> None:
        if kind == "unknown":
            address = f"nobody-{uuid4().hex[:6]}@pw.example"
        elif kind == "unverified":
            address = (await _user(db_session, made, verified=False)).email
        elif kind == "no_password":
            address = (await _user(db_session, made, password=None)).email
        else:
            address = (await _user(db_session, made, email=f"x{uuid4().hex[:6]}@local")).email
        tokens_before = await db_session.scalar(select(func.count()).select_from(EmailActionToken))
        audits_before = await db_session.scalar(select(func.count()).select_from(AuditLog))
        email = _email()

        await process_reset_request(
            email=address, session_factory=self._factory(async_engine), email_service=email
        )

        email.send_password_reset.assert_not_awaited()
        assert (
            await db_session.scalar(select(func.count()).select_from(EmailActionToken))
            == tokens_before
        )
        assert await db_session.scalar(select(func.count()).select_from(AuditLog)) == audits_before

    async def test_a_failure_is_logged_without_the_address(self) -> None:
        address = f"secret-{uuid4().hex[:6]}@pw.example"

        def _broken_factory():
            raise RuntimeError(f"db down while handling {address}")

        email = _email()
        with structlog.testing.capture_logs() as logs:
            await process_reset_request(  # does not raise
                email=address, session_factory=_broken_factory, email_service=email
            )

        email.send_password_reset.assert_not_awaited()
        assert any(e.get("event") == "password_reset_request_failed" for e in logs)
        assert address not in repr(logs)


# ---------------------------------------------------------------------------
# Set up
# ---------------------------------------------------------------------------


class TestSetup:
    async def test_full_flow_verifies_the_email(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, password=None, verified=False, provider="github")
        before = await _user_count(db_session)
        email = _email()
        service = PasswordAccountService(db_session, email_service=email)

        await service.request_setup(user_id=user.user_id)
        kwargs = email.send_password_setup.await_args.kwargs
        assert kwargs["to_email"] == user.email
        assert kwargs["expires_in_minutes"] == 30
        token = _token_from(kwargs["setup_url"])
        assert "/password/setup?token=" in kwargs["setup_url"]

        user_id = await service.complete_setup(raw_token=token, new_password=NEW)

        assert user_id == user.user_id
        refreshed = await _reload(db_session, user.user_id)
        assert refreshed.password_hash and verify_password(NEW, refreshed.password_hash)
        assert refreshed.email_verified_at is not None
        assert refreshed.auth_method == "oauth"  # the original method is unchanged
        assert await _user_count(db_session) == before
        providers = await db_session.scalar(
            select(func.count())
            .select_from(UserOAuthProvider)
            .where(UserOAuthProvider.user_id == user.user_id)
        )
        assert providers == 1

    async def test_keeps_an_existing_verification_time(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, password=None)
        verified_at = user.email_verified_at
        email = _email()
        service = PasswordAccountService(db_session, email_service=email)
        await service.request_setup(user_id=user.user_id)
        token = _token_from(email.send_password_setup.await_args.kwargs["setup_url"])
        await service.complete_setup(raw_token=token, new_password=NEW)
        assert (await _reload(db_session, user.user_id)).email_verified_at == verified_at

    async def test_refused_when_a_password_exists(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        with pytest.raises(ConflictError):
            await PasswordAccountService(db_session, email_service=_email()).request_setup(
                user_id=user.user_id
            )

    async def test_refused_for_local_accounts(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made, password=None, email=f"cli-{uuid4().hex[:6]}@LOCAL")
        with pytest.raises(PasswordSetupNotAllowedError):
            await PasswordAccountService(db_session, email_service=_email()).request_setup(
                user_id=user.user_id
            )

    @pytest.mark.parametrize("failure", ["raises", "false", "timeout"])
    async def test_failed_send_is_a_503_but_keeps_the_committed_token(
        self, db_session: AsyncSession, made: _Made, failure: str, monkeypatch
    ) -> None:
        # The token is committed before the send: a send that times out may
        # still deliver its email (the provider call cannot be cancelled), and
        # its link must then work. A stranded token is harmless: the next
        # request invalidates it.
        user = await _user(db_session, made, password=None)
        user_id = user.user_id
        email = _email()
        if failure == "raises":
            email.send_password_setup = AsyncMock(side_effect=RuntimeError("smtp"))
        elif failure == "false":
            email.send_password_setup = AsyncMock(return_value=False)
        else:
            monkeypatch.setattr(password_service_module, "_EMAIL_TIMEOUT_SECONDS", 0.01)

            async def _slow(**kwargs) -> bool:
                await asyncio.sleep(1)
                return True

            email.send_password_setup = AsyncMock(side_effect=_slow)
        service = PasswordAccountService(db_session, email_service=email)

        with pytest.raises(EmailDispatchError):
            await service.request_setup(user_id=user_id)

        token = _token_from(email.send_password_setup.await_args.kwargs["setup_url"])
        assert await _live_links(db_session, user_id) == 1
        requested = await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.user_id == user_id, AuditLog.action == "password_setup_requested")
        )
        assert requested == 1
        # The delivered-late link still works.
        await service.complete_setup(raw_token=token, new_password=NEW)

    async def test_token_is_committed_before_the_send(
        self, async_engine, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, password=None)
        seen_by_another_session: list[int] = []
        factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

        async def _send(**kwargs) -> bool:
            async with factory() as other:
                seen_by_another_session.append(await _live_links(other, user.user_id))
            return True

        email = _email()
        email.send_password_setup = AsyncMock(side_effect=_send)
        await PasswordAccountService(db_session, email_service=email).request_setup(
            user_id=user.user_id
        )
        assert seen_by_another_session == [1]


# ---------------------------------------------------------------------------
# Change / remove
# ---------------------------------------------------------------------------


class TestChange:
    async def test_changes_with_the_current_password(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        await PasswordAccountService(db_session).change(
            user_id=user.user_id, current_password=OLD, new_password=NEW
        )
        refreshed = await _reload(db_session, user.user_id)
        assert refreshed.password_hash and verify_password(NEW, refreshed.password_hash)

    async def test_wrong_current_password(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made)
        with pytest.raises(CurrentPasswordMismatchError):
            await PasswordAccountService(db_session).change(
                user_id=user.user_id, current_password="Wrong-Pass-000!", new_password=NEW
            )

    async def test_policy(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made)
        with pytest.raises(ValidationError) as exc_info:
            await PasswordAccountService(db_session).change(
                user_id=user.user_id, current_password=OLD, new_password="alllowercase1!"
            )
        assert "uppercase" in exc_info.value.message
        assert "alllowercase1!" not in exc_info.value.message

    async def test_no_password_to_change(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made, password=None)
        with pytest.raises(ConflictError):
            await PasswordAccountService(db_session).change(
                user_id=user.user_id, current_password=OLD, new_password=NEW
            )


class TestRemove:
    async def test_refused_as_the_last_method(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made)
        with pytest.raises(ConflictError):
            await PasswordAccountService(db_session).remove(
                user_id=user.user_id, current_password=OLD
            )
        assert (await _reload(db_session, user.user_id)).password_hash is not None

    async def test_removed_when_a_provider_remains(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, provider="google")
        await PasswordAccountService(db_session).remove(user_id=user.user_id, current_password=OLD)
        assert (await _reload(db_session, user.user_id)).password_hash is None

    async def test_wrong_current_password(self, db_session: AsyncSession, made: _Made) -> None:
        user = await _user(db_session, made, provider="google")
        with pytest.raises(CurrentPasswordMismatchError):
            await PasswordAccountService(db_session).remove(
                user_id=user.user_id, current_password="Wrong-Pass-000!"
            )


# ---------------------------------------------------------------------------
# Outstanding links die when the password changes
# ---------------------------------------------------------------------------


async def _issue_link(db: AsyncSession, user: User, purpose: str) -> str:
    issued = await EmailActionTokenService(db).issue(
        user_id=user.user_id,
        email=user.email,
        purpose=purpose,  # type: ignore[arg-type]
    )
    await db.commit()
    return issued.raw_token


async def _live_links(db: AsyncSession, user_id: str) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(EmailActionToken)
            .where(EmailActionToken.user_id == user_id, EmailActionToken.used_at.is_(None))
        )
        or 0
    )


class TestOutstandingLinks:
    async def test_change_kills_an_outstanding_reset_link(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        service = PasswordAccountService(db_session, email_service=_email())
        token = await _issue_link(db_session, user, "reset_password")

        await service.change(user_id=user.user_id, current_password=OLD, new_password=NEW)

        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=token, new_password="Another-Pass-789!")

    async def test_remove_kills_outstanding_links(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, provider="google")
        await _issue_link(db_session, user, "reset_password")
        await _issue_link(db_session, user, "set_password")

        await PasswordAccountService(db_session).remove(user_id=user.user_id, current_password=OLD)

        assert await _live_links(db_session, user.user_id) == 0

    async def test_complete_reset_kills_the_other_links(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        setup = await _issue_link(db_session, user, "set_password")
        reset = await _issue_link(db_session, user, "reset_password")
        service = PasswordAccountService(db_session, email_service=_email())

        await service.complete_reset(raw_token=reset, new_password=NEW)

        assert await _live_links(db_session, user.user_id) == 0
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_setup(raw_token=setup, new_password=NEW)

    async def test_complete_setup_kills_the_other_links(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, password=None)
        reset = await _issue_link(db_session, user, "reset_password")
        setup = await _issue_link(db_session, user, "set_password")
        service = PasswordAccountService(db_session, email_service=_email())

        await service.complete_setup(raw_token=setup, new_password=NEW)

        assert await _live_links(db_session, user.user_id) == 0
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=reset, new_password="Another-Pass-789!")

    async def test_reset_link_refused_once_the_password_is_gone(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made)
        token = await _issue_link(db_session, user, "reset_password")
        await db_session.execute(
            update(User).where(User.user_id == user.user_id).values(password_hash=None)
        )
        await db_session.commit()
        service = PasswordAccountService(db_session, email_service=_email())

        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=token, new_password=NEW)
        assert (await _reload(db_session, user.user_id)).password_hash is None

        # The link stays burned even once the account is eligible again.
        await db_session.execute(
            update(User)
            .where(User.user_id == user.user_id)
            .values(password_hash=hash_password(OLD))
        )
        await db_session.commit()
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_reset(raw_token=token, new_password=NEW)

    async def test_setup_link_refused_once_a_password_exists(
        self, db_session: AsyncSession, made: _Made
    ) -> None:
        user = await _user(db_session, made, password=None)
        token = await _issue_link(db_session, user, "set_password")
        await db_session.execute(
            update(User)
            .where(User.user_id == user.user_id)
            .values(password_hash=hash_password(OLD))
        )
        await db_session.commit()
        service = PasswordAccountService(db_session, email_service=_email())

        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_setup(raw_token=token, new_password=NEW)
        refreshed = await _reload(db_session, user.user_id)
        assert refreshed.password_hash and verify_password(OLD, refreshed.password_hash)

        # The link stays burned even once the account is eligible again.
        await db_session.execute(
            update(User).where(User.user_id == user.user_id).values(password_hash=None)
        )
        await db_session.commit()
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete_setup(raw_token=token, new_password=NEW)


# ---------------------------------------------------------------------------
# Session revocation runs before the commit
# ---------------------------------------------------------------------------


class _RevocationFailed(Exception):
    pass


async def _committed_hash(async_engine, user_id: str) -> str | None:
    """Read ``password_hash`` on a separate connection: committed state only."""
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as other:
        return await other.scalar(select(User.password_hash).where(User.user_id == user_id))


async def _run_flow(flow: str, db: AsyncSession, made: _Made, revoke) -> tuple[User, str | None]:
    """Run one password write with ``revoke_sessions``; return (user, link token)."""
    service = PasswordAccountService(db, email_service=_email())
    if flow == "reset":
        user = await _user(db, made)
        pending = await service.request_reset(email=user.email)
        assert pending is not None
        token = _token_from(pending.reset_url)
        await service.complete_reset(raw_token=token, new_password=NEW, revoke_sessions=revoke)
        return user, token
    if flow == "setup":
        user = await _user(db, made, password=None)
        await service.request_setup(user_id=user.user_id)
        token = _token_from(
            service.email_service.send_password_setup.await_args.kwargs["setup_url"]
        )
        await service.complete_setup(raw_token=token, new_password=NEW, revoke_sessions=revoke)
        return user, token
    if flow == "change":
        user = await _user(db, made)
        await service.change(
            user_id=user.user_id, current_password=OLD, new_password=NEW, revoke_sessions=revoke
        )
        return user, None
    user = await _user(db, made, provider="github")
    await service.remove(user_id=user.user_id, current_password=OLD, revoke_sessions=revoke)
    return user, None


FLOWS = ["reset", "setup", "change", "remove"]


class TestSessionRevocationOrder:
    @pytest.mark.parametrize("flow", FLOWS)
    async def test_revokes_before_the_write_is_committed(
        self, async_engine, db_session: AsyncSession, made: _Made, flow: str
    ) -> None:
        seen: list[tuple[str, str | None]] = []
        users: list[str] = []

        def revoke(user_id: str) -> None:
            users.append(user_id)

        async def _check_uncommitted() -> None:
            seen.append((users[0], await _committed_hash(async_engine, users[0])))

        # The callback is sync; record the committed state right after it.
        original_commit = db_session.commit

        async def commit() -> None:
            if users and not seen:
                await _check_uncommitted()
            await original_commit()

        db_session.commit = commit  # type: ignore[method-assign]
        try:
            user, _ = await _run_flow(flow, db_session, made, revoke)
        finally:
            db_session.commit = original_commit  # type: ignore[method-assign]

        assert users == [user.user_id]
        # At revocation time the new password was not committed yet.
        (_, committed_then) = seen[0]
        if flow == "setup":
            assert committed_then is None
        else:
            assert committed_then is not None and verify_password(OLD, committed_then)
        refreshed = await _reload(db_session, user.user_id)
        if flow == "remove":
            assert refreshed.password_hash is None
        else:
            assert refreshed.password_hash and verify_password(NEW, refreshed.password_hash)

    @pytest.mark.parametrize("flow", FLOWS)
    async def test_a_failed_revocation_rolls_the_write_back(
        self, db_session: AsyncSession, made: _Made, flow: str
    ) -> None:
        def revoke(user_id: str) -> None:
            raise _RevocationFailed

        # _run_flow creates the user itself; capture its id from the audit.
        with pytest.raises(_RevocationFailed):
            await _run_flow(flow, db_session, made, revoke)

        user_id = made.user_ids[-1]
        refreshed = await _reload(db_session, user_id)
        if flow == "setup":
            assert refreshed.password_hash is None
        else:
            assert refreshed.password_hash and verify_password(OLD, refreshed.password_hash)
        written = await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.user_id == user_id,
                AuditLog.action.in_(
                    ["password_reset", "password_set", "password_changed", "password_removed"]
                ),
            )
        )
        assert written == 0

    @pytest.mark.parametrize("flow", ["reset", "setup"])
    async def test_the_link_survives_a_failed_revocation(
        self, db_session: AsyncSession, made: _Made, flow: str
    ) -> None:
        state: dict[str, str | None] = {}

        def revoke(user_id: str) -> None:
            raise _RevocationFailed

        service = PasswordAccountService(db_session, email_service=_email())
        if flow == "reset":
            user = await _user(db_session, made)
            pending = await service.request_reset(email=user.email)
            assert pending is not None
            state["token"] = _token_from(pending.reset_url)
            complete = service.complete_reset
        else:
            user = await _user(db_session, made, password=None)
            await service.request_setup(user_id=user.user_id)
            state["token"] = _token_from(
                service.email_service.send_password_setup.await_args.kwargs["setup_url"]
            )
            complete = service.complete_setup
        token = state["token"]
        assert token is not None
        with pytest.raises(_RevocationFailed):
            await complete(raw_token=token, new_password=NEW, revoke_sessions=revoke)

        # Retrying once Redis is back works: the link was not burned.
        await complete(raw_token=token, new_password=NEW, revoke_sessions=lambda _uid: None)
        refreshed = await _reload(db_session, user.user_id)
        assert refreshed.password_hash and verify_password(NEW, refreshed.password_hash)


# ---------------------------------------------------------------------------
# bcrypt runs off the event loop
# ---------------------------------------------------------------------------


class TestBcryptOffTheLoop:
    @pytest.fixture
    def offloaded(self, monkeypatch) -> list[object]:
        calls: list[object] = []
        real = asyncio.to_thread

        async def _to_thread(func, /, *args, **kwargs):
            calls.append(func)
            return await real(func, *args, **kwargs)

        monkeypatch.setattr(password_service_module.asyncio, "to_thread", _to_thread)
        return calls

    @pytest.mark.parametrize("flow", FLOWS)
    async def test_hash_and_verify_are_offloaded(
        self, db_session: AsyncSession, made: _Made, flow: str, offloaded: list[object]
    ) -> None:
        await _run_flow(flow, db_session, made, None)
        names = {getattr(f, "__name__", "") for f in offloaded}
        if flow in ("change", "remove"):
            assert "verify_password" in names
        if flow != "remove":
            assert "hash_password" in names


# ---------------------------------------------------------------------------
# Nothing secret reaches the logs
# ---------------------------------------------------------------------------


class TestNoSecretsInLogs:
    async def test_reset_and_setup_flows(self, db_session: AsyncSession, made: _Made) -> None:
        reset_user = await _user(db_session, made)
        setup_user = await _user(db_session, made, password=None)
        logging_email = LoggingEmailService()
        setup_urls: list[str] = []
        original_setup = logging_email.send_password_setup

        async def _capture_setup(**kwargs):
            setup_urls.append(kwargs["setup_url"])
            return await original_setup(**kwargs)

        logging_email.send_password_setup = _capture_setup  # type: ignore[method-assign]
        service = PasswordAccountService(db_session, email_service=logging_email)

        with structlog.testing.capture_logs() as logs:
            pending = await service.request_reset(email=reset_user.email)
            assert pending is not None
            await service.send_reset_email(pending)
            reset_token = _token_from(pending.reset_url)
            await service.complete_reset(raw_token=reset_token, new_password=NEW)

            await service.request_setup(user_id=setup_user.user_id)
            setup_token = _token_from(setup_urls[0])
            await service.complete_setup(raw_token=setup_token, new_password=NEW)

            await service.change(user_id=reset_user.user_id, current_password=NEW, new_password=OLD)

        dump = repr(logs)
        assert logs, "expected the flows to log"
        for secret in (reset_token, setup_token, pending.reset_url, setup_urls[0], NEW, OLD):
            assert secret not in dump
        assert reset_user.email not in dump
        assert setup_user.email not in dump
        # The logging backend still flags the emails for manual dispatch.
        assert any(e.get("email_dispatch_required") for e in logs)
