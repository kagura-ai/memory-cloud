"""A provider connected to an email sign-up account after an email confirmation (#2786), real Postgres.

What only real transactions show:

- a GitHub sign-in with the address of an email sign-up account ends in
  ``ProviderLinkConfirmable``, the offer records a pending link and mails it,
  and following it in the same browser links GitHub to that account: one
  ``users`` row, one provider row, the audit rows; the next GitHub sign-in
  goes through the link and proves nothing for identity linking yet;
- opened in another browser (or with no cookie) nothing is linked and the
  link stays usable; a second use, an expired link, and a changed account
  (password, two-step verification, role, address, block marker) link
  nothing; an identity linked elsewhere meanwhile is refused;
- "This wasn't me" discards the link and sets the block marker: no further
  offer, no automatic Google link;
- a completion racing "This wasn't me" ends one way only.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.provider_link import provider_link_established
from auth.roles import RoleManager
from models.auth import AuditLog, User, UserOAuthProvider
from services import provider_link_confirmation_service as svc
from tests.integration.test_email_signup_db import (  # noqa: F401
    _email_account,
    _fresh_get_db,
    _gate,
    _users_for,
    env,
    maker,
    someone,
)
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    PasswordLinkInvalidError,
    ProviderLinkConfirmable,
    ProviderLinkWrongBrowserError,
)
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

NONCE = "N" * 43
OTHER_NONCE = "M" * 43


@pytest_asyncio.fixture(loop_scope="session")
async def gate_off(env, db_session) -> None:  # noqa: F811
    await _gate(db_session, enabled=False)


@pytest_asyncio.fixture(loop_scope="session")
async def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _github(maker, email: str, sub: str):  # noqa: F811
    with (
        _fresh_get_db(maker),
        patch("services.security_notification_service.spawn_security_notification"),
    ):
        return await RoleManager(use_postgres=True).ensure_user(
            email=email,
            user_id=sub,
            name="From GitHub",
            auth_provider="github",
            email_verified=True,
            email_authoritative=False,
        )


async def _offer(maker, redis, email: str, sub: str) -> str:  # noqa: F811
    """Sign in with GitHub, start the offer as the callback would; the mailed token."""
    with pytest.raises(ProviderLinkConfirmable) as caught:
        await _github(maker, email, sub)
    mail = MagicMock()
    mail.send_provider_link_confirmation = AsyncMock(return_value=True)
    await svc.process_link_offer(
        account_user_id=caught.value.account_user_id,
        account_stamp=caught.value.account_stamp,
        provider="github",
        user_info={"sub": sub, "email": email, "login": "octo-cat", "name": "Octo"},
        browser_hash=sha256_hex(NONCE),
        return_to="http://localhost:3000/device?c=1",
        ip_address="203.0.113.5",
        user_agent="pytest",
        session_factory=maker,
        email_service=mail,
        redis=redis,
    )
    mail.send_provider_link_confirmation.assert_awaited_once()
    kwargs = mail.send_provider_link_confirmation.await_args.kwargs
    assert kwargs["to_email"] == email
    return kwargs["confirm_url"].split("token=", 1)[1]


async def _complete(maker, redis, token: str, nonce: str | None = NONCE):  # noqa: F811
    with patch("services.security_notification_service.spawn_security_notification") as notify:
        async with maker() as s:
            confirmed = await svc.ProviderLinkConfirmationService(s, redis=redis).complete(
                raw_token=token, browser_nonce=nonce, ip_address="203.0.113.6", user_agent="pytest"
            )
    return confirmed, notify


async def _cancel(maker, redis, token: str) -> None:  # noqa: F811
    async with maker() as s:
        await svc.ProviderLinkConfirmationService(s, redis=redis).cancel(
            raw_token=token, ip_address="203.0.113.6", user_agent="pytest"
        )


async def _github_links(db_session: AsyncSession, user_id: str) -> int:
    db_session.expire_all()
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(UserOAuthProvider)
            .where(UserOAuthProvider.user_id == user_id, UserOAuthProvider.provider == "github")
        )
        or 0
    )


async def _actions(db_session: AsyncSession, user_id: str) -> list[str]:
    db_session.expire_all()
    rows = await db_session.scalars(
        select(AuditLog.action).where(AuditLog.user_id == user_id).order_by(AuditLog.id)
    )
    return list(rows)


def _sub() -> str:
    return str(uuid4().int)[:9]


class TestConfirmedLink:
    async def test_same_browser_links_github_to_the_email_account(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        sub = _sub()
        token = await _offer(maker, redis, email, sub)

        confirmed, notify = await _complete(maker, redis, token)

        assert confirmed.user_id == user_id
        assert confirmed.provider == "github"
        assert confirmed.return_to == "http://localhost:3000/device?c=1"
        assert await _users_for(db_session, email) == 1
        assert await _github_links(db_session, user_id) == 1
        actions = await _actions(db_session, user_id)
        assert "oauth_provider_link_confirmation_sent" in actions
        assert "oauth_provider_link_confirmed" in actions
        notify.assert_called_once()
        assert notify.call_args.kwargs["event"] == "sign_in_method_link_confirmed"
        assert "octo-cat" in notify.call_args.kwargs["sign_in_method"]

        # The next GitHub sign-in goes through the link to the same account...
        await _github(maker, email, sub)
        assert await _users_for(db_session, email) == 1
        # ...and the new link proves nothing for identity linking yet.
        db_session.expire_all()
        link = (
            await db_session.execute(
                select(UserOAuthProvider).where(
                    UserOAuthProvider.provider == "github", UserOAuthProvider.oauth_sub == sub
                )
            )
        ).scalar_one()
        user = (await db_session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        assert link.user_id == user_id
        assert not provider_link_established(
            sub_is_user_id=False, linked_at=link.linked_at, account_created_at=user.created_at
        )
        # Single use.
        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, redis, token)

    @pytest.mark.parametrize("nonce", [None, OTHER_NONCE])
    async def test_another_browser_links_nothing_and_keeps_the_link(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
        nonce,
    ) -> None:
        """The key threat: the owner opens the link in their own browser."""
        email, user_id = await _email_account(maker, domain="example.test")
        token = await _offer(maker, redis, email, _sub())

        with pytest.raises(ProviderLinkWrongBrowserError):
            await _complete(maker, redis, token, nonce)

        assert await _github_links(db_session, user_id) == 0
        # The browser that started the sign-in can still use it.
        confirmed, _ = await _complete(maker, redis, token)
        assert confirmed.user_id == user_id

    async def test_an_expired_link_links_nothing(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        token = await _offer(maker, redis, email, _sub())
        await redis.delete(f"link_confirm:{sha256_hex(token)}")  # the 15-minute TTL ran out

        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, redis, token)
        assert await _github_links(db_session, user_id) == 0


class TestAccountChangedMeanwhile:
    @pytest.mark.parametrize(
        "change",
        [
            {"password_hash": "$2b$12$" + "x" * 53},
            {"totp_enabled": True},
            {"role": "admin"},
            {"provider_auto_link_blocked_at": "now"},
            {"mfa_generation": "g2"},
            # Outside the account stamp: only the eligibility re-check under
            # the row lock refuses it.
            {"email_verified_at": None},
        ],
    )
    async def test_a_changed_account_links_nothing(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
        change,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        token = await _offer(maker, redis, email, _sub())
        values = {k: (utcnow() if v == "now" else v) for k, v in change.items()}
        await db_session.execute(update(User).where(User.user_id == user_id).values(values))
        await db_session.commit()

        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, redis, token)
        assert await _github_links(db_session, user_id) == 0
        # The link is spent.
        assert await redis.get(f"link_confirm:{sha256_hex(token)}") is None

    async def test_a_changed_address_links_nothing(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        token = await _offer(maker, redis, email, _sub())
        await db_session.execute(
            update(User).where(User.user_id == user_id).values(email=f"moved-{email}")
        )
        await db_session.commit()

        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, redis, token)
        assert await _github_links(db_session, user_id) == 0

    async def test_an_identity_linked_elsewhere_meanwhile_is_refused(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
        someone,  # noqa: F811
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        sub = _sub()
        token = await _offer(maker, redis, email, sub)
        db_session.add(
            UserOAuthProvider(user_id=someone, provider="github", oauth_sub=sub, linked_at=utcnow())
        )
        await db_session.commit()

        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, redis, token)
        assert await _github_links(db_session, user_id) == 0


class TestExclusions:
    @pytest.mark.parametrize(
        "change",
        [{"totp_enabled": True}, {"role": "admin"}, {"provider_auto_link_blocked_at": "now"}],
    )
    async def test_no_offer_for_excluded_accounts(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        change,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        values = {k: (utcnow() if v == "now" else v) for k, v in change.items()}
        await db_session.execute(update(User).where(User.user_id == user_id).values(values))
        await db_session.commit()

        with pytest.raises(ConflictError) as caught:
            await _github(maker, email, _sub())
        assert type(caught.value) is ConflictError
        assert await _users_for(db_session, email) == 1


class TestDecline:
    async def test_this_wasnt_me_discards_and_blocks(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
    ) -> None:
        email, user_id = await _email_account(maker, domain="gmail.com")
        sub = _sub()
        token = await _offer(maker, redis, email, sub)

        await _cancel(maker, redis, token)
        await _cancel(maker, redis, token)  # idempotent

        db_session.expire_all()
        user = (await db_session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        assert user.provider_auto_link_blocked_at is not None
        assert (await _actions(db_session, user_id)).count(
            "oauth_provider_link_confirmation_declined"
        ) == 1
        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, redis, token)
        # The same identity gets no further offer...
        with pytest.raises(ConflictError) as caught:
            await _github(maker, email, sub)
        assert type(caught.value) is ConflictError
        # ...and Google no longer links automatically.
        with (
            _fresh_get_db(maker),
            patch("services.security_notification_service.spawn_security_notification"),
            pytest.raises(ConflictError),
        ):
            await RoleManager(use_postgres=True).ensure_user(
                email=email,
                user_id=f"g-{uuid4().hex[:10]}",
                auth_provider="google",
                email_verified=True,
                email_authoritative=True,
            )
        assert await _github_links(db_session, user_id) == 0
        assert await _users_for(db_session, email) == 1

    async def test_a_failed_write_keeps_the_link_for_a_retry(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        token = await _offer(maker, redis, email, _sub())

        async def blocked() -> object:
            db_session.expire_all()
            return (
                await db_session.execute(
                    select(User.provider_auto_link_blocked_at).where(User.user_id == user_id)
                )
            ).scalar_one()

        down = OperationalError("COMMIT", {}, Exception("database unavailable"))
        async with maker() as s:
            with (
                patch.object(s, "commit", AsyncMock(side_effect=down)),
                pytest.raises(OperationalError),
            ):
                await svc.ProviderLinkConfirmationService(s, redis=redis).cancel(
                    raw_token=token, ip_address="203.0.113.6", user_agent="pytest"
                )
        assert await blocked() is None
        assert await redis.ttl(f"link_confirm:{sha256_hex(token)}") > 0

        await _cancel(maker, redis, token)  # the owner clicks again

        assert await blocked() is not None
        assert (await _actions(db_session, user_id)).count(
            "oauth_provider_link_confirmation_declined"
        ) == 1
        assert await redis.get(f"link_confirm:{sha256_hex(token)}") is None

    async def test_complete_racing_cancel_ends_one_way(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        token = await _offer(maker, redis, email, _sub())

        results = await asyncio.gather(
            _complete(maker, redis, token), _cancel(maker, redis, token), return_exceptions=True
        )

        linked = await _github_links(db_session, user_id)
        db_session.expire_all()
        blocked = (
            await db_session.execute(
                select(User.provider_auto_link_blocked_at).where(User.user_id == user_id)
            )
        ).scalar_one()
        assert (linked, blocked is not None) in ((1, False), (0, True))
        if linked:
            assert not isinstance(results[0], BaseException)
        else:
            assert isinstance(results[0], PasswordLinkInvalidError)


class TestLimits:
    async def test_a_newer_offer_from_the_same_identity_replaces_the_older(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        redis,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        sub = _sub()
        first = await _offer(maker, redis, email, sub)
        second = await _offer(maker, redis, email, sub)

        with pytest.raises(PasswordLinkInvalidError):
            await _complete(maker, redis, first)
        confirmed, _ = await _complete(maker, redis, second)
        assert confirmed.user_id == user_id
        assert await _users_for(db_session, email) == 1


def test_ttl_is_fifteen_minutes() -> None:
    assert svc.LINK_CONFIRM_TTL == timedelta(minutes=15)


class TestLogs:
    async def test_the_whole_flow_never_logs_the_token_the_nonce_or_the_link(
        self,
        gate_off,
        maker,  # noqa: F811
        redis,
    ) -> None:
        import auth.roles as roles_module

        email, _ = await _email_account(maker, domain="example.test")
        with (
            patch.object(svc, "logger") as svc_log,
            patch.object(roles_module, "logger") as roles_log,
        ):
            token = await _offer(maker, redis, email, _sub())
            with pytest.raises(ProviderLinkWrongBrowserError):
                await _complete(maker, redis, token, OTHER_NONCE)
            await _complete(maker, redis, token)
        logged = repr(svc_log.mock_calls) + repr(roles_log.mock_calls)
        assert token not in logged
        assert NONCE not in logged and OTHER_NONCE not in logged
        assert "link/confirm" not in logged
        assert email not in logged
