"""PasswordAccountService against real Postgres (Issue #1678).

Reset, set-up, change and remove flows; the link rules (single use, expiry,
address changed since sending); the last-method guard; that no flow creates a
user; and that neither the token nor the link nor a password reaches the logs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.password import hash_password, verify_password
from models.auth import AuditLog, EmailActionToken, User, UserOAuthProvider
from services.email_service import LoggingEmailService
from services.password_account_service import PasswordAccountService
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

    @pytest.mark.parametrize("failure", ["raises", "false"])
    async def test_failed_send_leaves_no_token(
        self, db_session: AsyncSession, made: _Made, failure: str
    ) -> None:
        user = await _user(db_session, made, password=None)
        user_id = user.user_id  # the rollback expires the instance
        email = _email()
        email.send_password_setup = (
            AsyncMock(side_effect=RuntimeError("smtp"))
            if failure == "raises"
            else AsyncMock(return_value=False)
        )
        with pytest.raises(EmailDispatchError):
            await PasswordAccountService(db_session, email_service=email).request_setup(
                user_id=user_id
            )
        rows = await db_session.scalar(
            select(func.count())
            .select_from(EmailActionToken)
            .where(EmailActionToken.user_id == user_id)
        )
        assert rows == 0


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
