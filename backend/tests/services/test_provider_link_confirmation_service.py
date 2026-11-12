"""The offer, preview and browser check of a provider link confirmed by email (#2786).

Mocked database, an in-memory Redis. The row-locked completion, the
refusals at completion and "This wasn't me" against Postgres are in
``tests/integration/test_provider_link_confirm_db.py``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from auth.roles import account_stamp
from config.settings import get_settings
from services import provider_link_confirmation_service as svc
from utils.exceptions import PasswordLinkInvalidError, ProviderLinkWrongBrowserError
from utils.hashing import sha256_hex

NONCE = "N" * 43
OTHER_NONCE = "M" * 43
GITHUB_INFO = {
    "sub": "583231",
    "email": "person@example.com",
    "email_verified": True,
    "login": "octo-cat",
    "name": "Octo\nCat​",
    "picture": "https://avatars.githubusercontent.com/u/583231?v=4",
}


def _account(**overrides):
    values = {
        "id": 7,
        "user_id": "email:abc",
        "email": "person@example.com",
        "name": "Person",
        "picture": None,
        "role": "user",
        "auth_method": "password",
        "login_id": None,
        "email_verified_at": datetime(2026, 1, 1),
        "totp_enabled": False,
        "provider_auto_link_blocked_at": None,
        "password_hash": "$2b$hash",
        "mfa_generation": None,
        "locale": "ja",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _factory(user):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()

    @asynccontextmanager
    async def _session():
        yield db

    return _session, db


async def _offer(redis, *, user=None, info=None, stamp=None, deliverable="person@example.com"):
    user = user or _account()
    factory, db = _factory(user)
    mail = MagicMock()
    mail.send_provider_link_confirmation = AsyncMock(return_value=True)
    with patch(
        "services.security_notification_service.resolve_deliverable_address",
        AsyncMock(return_value=deliverable),
    ):
        await svc.process_link_offer(
            account_user_id=user.user_id,
            account_stamp=stamp if stamp is not None else account_stamp(user),
            provider="github",
            user_info=info or GITHUB_INFO,
            browser_hash=sha256_hex(NONCE),
            return_to="http://localhost:3000/device?c=1",
            ip_address="203.0.113.7",
            user_agent="pytest",
            session_factory=factory,
            email_service=mail,
            redis=redis,
        )
    return mail, db


def _token(mail) -> str:
    url = mail.send_provider_link_confirmation.await_args.kwargs["confirm_url"]
    assert url.startswith(f"{get_settings().frontend_url.rstrip('/')}/link/confirm?token=")
    return url.split("token=", 1)[1]


async def _records(redis) -> list[dict]:
    keys = [k async for k in redis.scan_iter("link_confirm:*")]
    return [json.loads(await redis.get(k)) for k in keys]


class TestOffer:
    @pytest.mark.asyncio
    async def test_records_and_mails_one_confirmation(self, redis) -> None:
        mail, db = await _offer(redis)

        mail.send_provider_link_confirmation.assert_awaited_once()
        kwargs = mail.send_provider_link_confirmation.await_args.kwargs
        assert kwargs["to_email"] == "person@example.com"
        assert kwargs["provider_label"] == "GitHub"
        assert kwargs["provider_login"] == "octo-cat"
        assert kwargs["expires_in_minutes"] == 15
        assert kwargs["locale"] == "ja"
        token = _token(mail)
        records = await _records(redis)
        assert len(records) == 1
        record = records[0]
        assert await redis.ttl(f"link_confirm:{sha256_hex(token)}") == 900
        assert record["account_user_id"] == "email:abc"
        assert record["provider"] == "github"
        assert record["oauth_sub"] == "583231"
        assert record["provider_login"] == "octo-cat"
        assert record["provider_name"] == "Octo Cat"
        assert record["provider_avatar_url"] == GITHUB_INFO["picture"]
        assert record["browser_hash"] == sha256_hex(NONCE)
        assert record["return_to"] == "http://localhost:3000/device?c=1"
        # Neither the address nor the token nor the nonce is stored.
        raw = json.dumps(record)
        assert "person@example.com" not in raw
        assert token not in raw and NONCE not in raw
        audit = db.add.call_args.args[0]
        assert audit.action == "oauth_provider_link_confirmation_sent"
        assert audit.user_id == "email:abc"
        assert audit.user_email == "link-confirmation"
        assert audit.user_metadata == {"identity_hmac": svc.identity_hmac("github", "583231")}
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_new_offer_from_the_same_identity_replaces_the_old_one(self, redis) -> None:
        first, _ = await _offer(redis)
        second, _ = await _offer(redis)

        records = await _records(redis)
        assert len(records) == 1
        assert await redis.get(f"link_confirm:{sha256_hex(_token(first))}") is None
        assert await redis.get(f"link_confirm:{sha256_hex(_token(second))}") is not None

    @pytest.mark.asyncio
    async def test_another_identity_does_not_replace_the_owners_pending_link(self, redis) -> None:
        own, _ = await _offer(redis)
        await _offer(redis, info={**GITHUB_INFO, "sub": "999", "login": "someone"})

        assert len(await _records(redis)) == 2
        assert await redis.get(f"link_confirm:{sha256_hex(_token(own))}") is not None

    @pytest.mark.asyncio
    async def test_three_mails_per_identity_a_day(self, redis) -> None:
        sent = 0
        for _ in range(5):
            mail, _ = await _offer(redis)
            sent += mail.send_provider_link_confirmation.await_count
        assert sent == 3
        assert (
            0
            < await redis.ttl(
                next(iter([k async for k in redis.scan_iter("link_confirm_sent_identity:*")]))
            )
            <= 86400
        )

    @pytest.mark.asyncio
    async def test_three_mails_per_account_a_day_across_identities(self, redis) -> None:
        sent = 0
        for n in range(5):
            mail, _ = await _offer(redis, info={**GITHUB_INFO, "sub": f"id-{n}"})
            sent += mail.send_provider_link_confirmation.await_count
        assert sent == 3

    @pytest.mark.asyncio
    async def test_nothing_is_sent_when_redis_cannot_count(self, redis) -> None:
        broken = MagicMock(wraps=redis)
        broken.incr = AsyncMock(side_effect=ConnectionError("down"))
        mail, db = await _offer(broken)
        mail.send_provider_link_confirmation.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.parametrize(
        ("user", "reason"),
        [
            (_account(totp_enabled=True), "totp"),
            (_account(role="admin"), "admin"),
            (_account(provider_auto_link_blocked_at=datetime(2026, 2, 1)), "blocked"),
            (_account(login_id="cli"), "not an email sign-up account"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_account_that_cannot_take_the_link_gets_nothing(
        self, redis, user, reason
    ) -> None:
        mail, db = await _offer(redis, user=user)
        mail.send_provider_link_confirmation.assert_not_awaited()
        assert await _records(redis) == [], reason

    @pytest.mark.asyncio
    async def test_an_account_that_changed_since_the_sign_in_gets_nothing(self, redis) -> None:
        mail, _ = await _offer(redis, stamp="another-stamp")
        mail.send_provider_link_confirmation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_mail_without_a_deliverable_address(self, redis) -> None:
        mail, _ = await _offer(redis, deliverable=None)
        mail.send_provider_link_confirmation.assert_not_awaited()
        assert await _records(redis) == []

    @pytest.mark.parametrize("login", [None, "", "octo cat", "a" * 40, "évil", "x\r\ny"])
    @pytest.mark.asyncio
    async def test_a_login_outside_githubs_alphabet_gets_no_offer(self, redis, login) -> None:
        mail, _ = await _offer(redis, info={**GITHUB_INFO, "login": login})
        mail.send_provider_link_confirmation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_switch_off_offers_nothing(self, redis, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "enable_provider_link_confirmation", False)
        mail, _ = await _offer(redis)
        mail.send_provider_link_confirmation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_logs_never_carry_the_token_the_nonce_or_the_link(self, redis) -> None:
        with patch.object(svc, "logger") as log:
            mail, _ = await _offer(redis)
        token = _token(mail)
        logged = repr(log.mock_calls)
        assert token not in logged
        assert NONCE not in logged
        assert "link/confirm" not in logged
        assert "person@example.com" not in logged

    @pytest.mark.asyncio
    async def test_never_raises(self, redis) -> None:
        def _boom():
            raise RuntimeError("db down")

        await svc.process_link_offer(
            account_user_id="email:abc",
            account_stamp="s",
            provider="github",
            user_info=GITHUB_INFO,
            browser_hash=sha256_hex(NONCE),
            return_to=None,
            ip_address=None,
            user_agent=None,
            session_factory=_boom,
            email_service=MagicMock(),
            redis=redis,
        )


class TestDisplayValues:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Octo Cat", "Octo Cat"),
            ("a\nb\tc", "a b c"),
            ("‮evil", "evil"),
            ("", None),
            (None, None),
            (12, None),
        ],
    )
    def test_display_name(self, value, expected) -> None:
        assert svc.display_name(value) == expected

    def test_display_name_is_capped(self) -> None:
        name = svc.display_name("x" * 200)
        assert name is not None and len(name) == 80

    @pytest.mark.parametrize(
        ("provider", "url", "kept"),
        [
            ("github", "https://avatars.githubusercontent.com/u/1?v=4", True),
            ("google", "https://lh3.googleusercontent.com/a/x", True),
            ("github", "http://avatars.githubusercontent.com/u/1", False),
            ("github", "https://evil.example/u/1", False),
            ("github", "https://lh3.googleusercontent.com/a/x", False),
            ("github", "https://user@avatars.githubusercontent.com/u/1", False),
            ("github", "javascript:alert(1)", False),
        ],
    )
    def test_avatar_url(self, provider, url, kept) -> None:
        assert (svc.avatar_url(provider, url) == url) is kept

    def test_google_login_is_the_identity_address(self) -> None:
        assert svc.provider_login("google", {"email": "p@example.com"}) == "p@example.com"
        assert svc.provider_login("google", {"email": "p @example.com"}) is None


class TestPreviewAndBrowserCheck:
    async def _pending(self, redis) -> str:
        mail, _ = await _offer(redis)
        return _token(mail)

    @pytest.mark.asyncio
    async def test_preview_says_which_browser(self, redis) -> None:
        token = await self._pending(redis)
        service = svc.ProviderLinkConfirmationService(AsyncMock(), redis=redis)

        same = await service.preview(raw_token=token, browser_nonce=NONCE)
        other = await service.preview(raw_token=token, browser_nonce=OTHER_NONCE)
        none = await service.preview(raw_token=token, browser_nonce=None)

        assert same.same_browser is True
        assert other.same_browser is False and none.same_browser is False
        assert same.provider == "github" and same.provider_login == "octo-cat"
        # Preview consumes nothing.
        assert len(await _records(redis)) == 1

    @pytest.mark.parametrize("token", ["", "short", "x" * 43, "!" * 43])
    @pytest.mark.asyncio
    async def test_an_unknown_or_malformed_token_is_invalid(self, redis, token) -> None:
        service = svc.ProviderLinkConfirmationService(AsyncMock(), redis=redis)
        with pytest.raises(PasswordLinkInvalidError):
            await service.preview(raw_token=token, browser_nonce=NONCE)
        with pytest.raises(PasswordLinkInvalidError):
            await service.complete(
                raw_token=token, browser_nonce=NONCE, ip_address=None, user_agent=None
            )

    @pytest.mark.parametrize("nonce", [None, OTHER_NONCE])
    @pytest.mark.asyncio
    async def test_complete_in_another_browser_is_refused_and_keeps_the_link(
        self, redis, nonce
    ) -> None:
        """The owner opens a link someone else's sign-in caused, in their own browser."""
        token = await self._pending(redis)
        db = AsyncMock()
        service = svc.ProviderLinkConfirmationService(db, redis=redis)

        with pytest.raises(ProviderLinkWrongBrowserError) as caught:
            await service.complete(
                raw_token=token, browser_nonce=nonce, ip_address=None, user_agent=None
            )

        assert caught.value.status_code == 403
        assert caught.value.error_code == "AUTH-310"
        db.execute.assert_not_awaited()
        db.commit.assert_not_awaited()
        assert len(await _records(redis)) == 1

    @pytest.mark.parametrize(
        "tampered",
        [
            "not json",
            json.dumps(["x"]),
            json.dumps({"provider": "gitlab"}),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_corrupted_record_is_invalid(self, redis, tampered) -> None:
        token = "T" * 43
        await redis.set(f"link_confirm:{sha256_hex(token)}", tampered)
        service = svc.ProviderLinkConfirmationService(AsyncMock(), redis=redis)
        with pytest.raises(PasswordLinkInvalidError):
            await service.preview(raw_token=token, browser_nonce=NONCE)

    @pytest.mark.asyncio
    async def test_cancel_with_an_unknown_token_changes_nothing(self, redis) -> None:
        db = AsyncMock()
        service = svc.ProviderLinkConfirmationService(db, redis=redis)
        await service.cancel(raw_token="T" * 43, ip_address=None, user_agent=None)
        await service.cancel(raw_token="bad", ip_address=None, user_agent=None)
        db.execute.assert_not_awaited()
