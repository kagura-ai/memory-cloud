"""Security-change notification emails (Issue #1752).

Covers the coalescing window (first occurrence sent at once, repeats buffered,
one digest when the window closes, the ``ZREM`` claim, Redis down → send at
once), failure isolation, the deliverable-address rule against real Postgres,
sanitizing of untrusted names / user agents, and the rendered body (never a
secret, token or action link).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, UserOAuthProvider
from services import security_notification_service as sns
from services.email_service import LoggingEmailService
from services.security_notification_service import (
    SecurityEvent,
    SecurityOccurrence,
    render_security_notification,
    resolve_deliverable_address,
    sanitize_display_text,
)
from utils.datetime import utcnow

OWNER = "owner-1"
ADDRESS = "owner@example.test"


@asynccontextmanager
async def _session() -> AsyncIterator[MagicMock]:
    yield MagicMock()


def _factory() -> MagicMock:
    return MagicMock(side_effect=lambda: _session())


# The production value, captured before the autouse fixture below patches it.
IMMEDIATE_BUDGET = sns._IMMEDIATE_PER_WINDOW


@pytest.fixture(autouse=True)
def one_immediate_notice(monkeypatch) -> None:
    """One notice at once per window, so the second occurrence is already
    buffered: most tests here exercise the window machinery. The tests of the
    budget itself set ``IMMEDIATE_BUDGET`` back."""
    monkeypatch.setattr(sns, "_IMMEDIATE_PER_WINDOW", 1)


@pytest.fixture
def redis(monkeypatch) -> fakeredis.aioredis.FakeRedis:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(sns, "get_redis_client", lambda: fake)
    return fake


@pytest.fixture
def deliverable(monkeypatch) -> AsyncMock:
    resolve = AsyncMock(return_value=ADDRESS)
    monkeypatch.setattr(sns, "resolve_deliverable_address", resolve)
    return resolve


@pytest.fixture
def email() -> AsyncMock:
    service = AsyncMock()
    service.send_security_notification = AsyncMock(return_value=True)
    return service


async def _notify(email: AsyncMock, event=SecurityEvent.API_KEY_CREATED, **kwargs) -> None:
    await sns.notify_security_event(
        kwargs.pop("user_id", OWNER),
        event,
        ip=kwargs.pop("ip", "192.0.2.10"),
        user_agent=kwargs.pop("user_agent", "pytest-agent/1.0"),
        session_factory=_factory(),
        email_service=email,
        **kwargs,
    )


def _window_end() -> float:
    return sns._now_score() + sns.get_settings().security_notification_window_seconds


EVENT = "api_key_created"


async def _wid(redis, user_id: str = OWNER) -> str | None:
    """The id of the (user, EVENT)'s open window."""
    parsed = sns._parse_pointer(await redis.get(sns._open_key(user_id, EVENT)))
    return parsed[0] if parsed else None


def _k(template: str, wid: str) -> str:
    return sns._key(template, OWNER, EVENT, wid)


def _m(wid: str) -> str:
    return sns._member(OWNER, EVENT, wid)


async def _window_keys(redis) -> list[str]:
    """Every notice key left in Redis except the due set."""
    return sorted(k for k in await redis.keys("security_notify:*") if k != sns._DUE_KEY)


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------


class TestCoalescing:
    @pytest.mark.asyncio
    async def test_first_occurrence_is_sent_at_once(self, redis, deliverable, email) -> None:
        await _notify(email, key_name="ci-key")

        email.send_security_notification.assert_awaited_once()
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["to_email"] == ADDRESS
        assert kwargs["event"] == "api_key_created"
        assert kwargs["digest"] is False
        (occurrence,) = kwargs["occurrences"]
        assert occurrence.key_name == "ci-key"
        assert occurrence.ip == "192.0.2.10"
        assert occurrence.occurred_at.endswith(" UTC")
        assert kwargs["profile_page_url"].endswith("/profile")
        wid = await _wid(redis)
        assert wid is not None
        # Nothing to send later yet: no due entry, and the pointer expires by
        # itself soon after the window.
        assert await redis.zcard(sns._DUE_KEY) == 0
        assert 0 < await redis.ttl(sns._open_key(OWNER, EVENT)) <= sns._pointer_ttl()

    @pytest.mark.asyncio
    async def test_repeats_in_the_window_are_buffered(self, redis, deliverable, email) -> None:
        await _notify(email, key_name="a")
        await _notify(email, key_name="b")
        await _notify(email, key_name="c")

        assert email.send_security_notification.await_count == 1
        assert await redis.llen(_k(sns._BUFFER_KEY, await _wid(redis))) == 2

    @pytest.mark.asyncio
    async def test_other_events_and_users_have_their_own_windows(
        self, redis, deliverable, email
    ) -> None:
        await _notify(email, SecurityEvent.API_KEY_CREATED)
        await _notify(email, SecurityEvent.PASSWORD_CHANGED)
        await _notify(email, SecurityEvent.API_KEY_CREATED, user_id="owner-2")
        assert email.send_security_notification.await_count == 3

    @pytest.mark.asyncio
    async def test_digest_lists_the_buffered_occurrences_when_the_window_closes(
        self, redis, deliverable, email
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        await _notify(email, key_name="third")
        email.send_security_notification.reset_mock()

        # Not due yet: nothing is sent.
        assert (
            await sns.flush_due_security_notifications(
                session_factory=_factory(), email_service=email
            )
            == 0
        )
        email.send_security_notification.assert_not_awaited()

        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is True
        assert [o.key_name for o in kwargs["occurrences"]] == ["second", "third"]
        assert await redis.zcard(sns._DUE_KEY) == 0
        assert await _window_keys(redis) == []

        # The next occurrence opens a new window and is sent at once.
        email.send_security_notification.reset_mock()
        await _notify(email, key_name="fourth")
        assert email.send_security_notification.await_args.kwargs["digest"] is False

    @pytest.mark.asyncio
    async def test_a_window_with_nothing_buffered_sends_no_digest(
        self, redis, deliverable, email
    ) -> None:
        await _notify(email)
        email.send_security_notification.reset_mock()
        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        assert sent == 0
        email.send_security_notification.assert_not_awaited()
        assert await redis.zcard(sns._DUE_KEY) == 0

    @pytest.mark.asyncio
    async def test_a_window_claimed_elsewhere_is_not_sent_twice(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        email.send_security_notification.reset_mock()

        # Another process claims the window between our ZRANGEBYSCORE and claim.
        real_claim = sns._claim_window

        async def _claimed_elsewhere_first(client, *args):
            assert await real_claim(client, *args) is True  # the other process
            return await real_claim(client, *args)  # ours finds no due entry

        monkeypatch.setattr(sns, "_claim_window", _claimed_elsewhere_first)
        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        assert sent == 0
        email.send_security_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_down_sends_at_once(self, monkeypatch, deliverable, email) -> None:
        broken = MagicMock()
        broken.pipeline = MagicMock(side_effect=ConnectionError("redis down"))
        monkeypatch.setattr(sns, "get_redis_client", lambda: broken)

        await _notify(email)
        await _notify(email)

        assert email.send_security_notification.await_count == 2

    @pytest.mark.asyncio
    async def test_flush_with_redis_down_does_not_raise(self, monkeypatch, email) -> None:
        def _boom():
            raise ConnectionError("redis down")

        monkeypatch.setattr(sns, "get_redis_client", _boom)
        assert await sns.flush_due_security_notifications(email_service=email) == 0

    @pytest.mark.asyncio
    async def test_digest_rechecks_the_recipient(self, redis, deliverable, email) -> None:
        await _notify(email)
        await _notify(email)
        email.send_security_notification.reset_mock()
        deliverable.return_value = None  # e.g. the account was erased meanwhile

        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        assert sent == 0
        email.send_security_notification.assert_not_awaited()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class TestFailuresAreSwallowed:
    @pytest.mark.asyncio
    async def test_email_failure_is_logged(self, redis, deliverable, monkeypatch) -> None:
        email = AsyncMock()
        email.send_security_notification = AsyncMock(side_effect=RuntimeError("provider down"))
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)

        await _notify(email)  # does not raise

        logger.error.assert_called_once()
        assert logger.error.call_args.args[0] == "security_notification_send_failed"
        assert logger.error.call_args.kwargs["error_type"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_send_returning_false_is_logged(self, redis, deliverable, monkeypatch) -> None:
        email = AsyncMock()
        email.send_security_notification = AsyncMock(return_value=False)
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        await _notify(email)
        assert logger.error.call_args.kwargs["error_type"] == "send_returned_false"

    @pytest.mark.asyncio
    async def test_db_failure_is_logged(self, redis, email, monkeypatch) -> None:
        monkeypatch.setattr(
            sns, "resolve_deliverable_address", AsyncMock(side_effect=OSError("db down"))
        )
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)

        await _notify(email)  # does not raise

        email.send_security_notification.assert_not_awaited()
        assert logger.error.call_args.args[0] == "security_notification_failed"
        assert logger.error.call_args.kwargs["error_type"] == "OSError"

    @pytest.mark.asyncio
    async def test_undeliverable_address_is_skipped_without_logging_it(
        self, redis, email, monkeypatch
    ) -> None:
        monkeypatch.setattr(sns, "resolve_deliverable_address", AsyncMock(return_value=None))
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        await _notify(email)
        email.send_security_notification.assert_not_awaited()
        logger.info.assert_called_once_with(
            "security_notification_skipped", user_id=OWNER, security_event="api_key_created"
        )


# ---------------------------------------------------------------------------
# Actor and client lookups
# ---------------------------------------------------------------------------


class TestLookups:
    @pytest.mark.asyncio
    async def test_actor_is_named(self, redis, deliverable, email, monkeypatch) -> None:
        monkeypatch.setattr(
            sns, "_actor_label", AsyncMock(return_value="Ada Admin (ada@example.test)")
        )
        await _notify(email, actor_user_id="admin-1")
        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.actor == "Ada Admin (ada@example.test)"

    @pytest.mark.asyncio
    async def test_client_name_is_looked_up_by_id(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        lookup = AsyncMock(return_value="Claude Desktop")
        monkeypatch.setattr(sns, "_client_name", lookup)
        await _notify(email, SecurityEvent.OAUTH_CLIENT_AUTHORIZED, client_id="cid-1")
        assert lookup.await_args.args[1] == "cid-1"
        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.client_name == "Claude Desktop"

    def test_schedule_captures_ip_ua_and_drops_self_actor(self) -> None:
        from types import SimpleNamespace

        from fastapi import BackgroundTasks

        tasks = BackgroundTasks()
        request = SimpleNamespace(
            client=SimpleNamespace(host="198.51.100.4"), headers={"user-agent": "UA/1"}
        )
        sns.schedule_security_notification(
            tasks,
            user_id=OWNER,
            event=SecurityEvent.API_KEY_CREATED,
            request=request,
            key_name="k",
            actor_user_id=OWNER,
        )
        (task,) = tasks.tasks
        assert task.func is sns.notify_security_event
        assert task.args == (OWNER, SecurityEvent.API_KEY_CREATED)
        assert task.kwargs["ip"] == "198.51.100.4"
        assert task.kwargs["user_agent"] == "UA/1"
        assert task.kwargs["actor_user_id"] is None
        assert isinstance(task.kwargs["occurred_at"], datetime)


# ---------------------------------------------------------------------------
# Deliverable address (real Postgres)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def made_users(db_session: AsyncSession) -> AsyncIterator[list[str]]:
    ids: list[str] = []
    yield ids
    await db_session.rollback()
    if ids:
        await db_session.execute(
            delete(UserOAuthProvider).where(UserOAuthProvider.user_id.in_(ids))
        )
        await db_session.execute(delete(User).where(User.user_id.in_(ids)))
        await db_session.commit()


async def _make_user(
    db: AsyncSession, ids: list[str], *, verified: bool, provider: bool, email: str | None = None
) -> User:
    uid = f"sn_{uuid4().hex[:10]}"
    user = User(
        user_id=uid,
        email=email or f"{uid}@notify.example",
        name="SN",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        email_verified_at=utcnow() if verified else None,
    )
    db.add(user)
    if provider:
        db.add(UserOAuthProvider(user_id=uid, provider="google", oauth_sub=f"sub-{uid}"))
    await db.commit()
    ids.append(uid)
    return user


class TestDeliverableAddress:
    pytestmark = pytest.mark.asyncio(loop_scope="session")

    async def test_verified_email(self, db_session: AsyncSession, made_users) -> None:
        user = await _make_user(db_session, made_users, verified=True, provider=False)
        assert await resolve_deliverable_address(db_session, user.user_id) == user.email

    async def test_linked_provider_alone_is_not_proof(
        self, db_session: AsyncSession, made_users
    ) -> None:
        # Google can return an unverified address; only email_verified_at counts.
        user = await _make_user(db_session, made_users, verified=False, provider=True)
        assert await resolve_deliverable_address(db_session, user.user_id) is None

    async def test_oauth_user_verified_on_sign_in(
        self, db_session: AsyncSession, made_users
    ) -> None:
        user = await _make_user(db_session, made_users, verified=True, provider=True)
        assert await resolve_deliverable_address(db_session, user.user_id) == user.email

    async def test_unverified_password_only_user(
        self, db_session: AsyncSession, made_users
    ) -> None:
        user = await _make_user(db_session, made_users, verified=False, provider=False)
        assert await resolve_deliverable_address(db_session, user.user_id) is None

    async def test_local_address_never(self, db_session: AsyncSession, made_users) -> None:
        uid_email = f"cli_{uuid4().hex[:8]}@LOCAL"
        user = await _make_user(
            db_session, made_users, verified=True, provider=True, email=uid_email
        )
        assert await resolve_deliverable_address(db_session, user.user_id) is None

    async def test_unknown_user(self, db_session: AsyncSession) -> None:
        assert await resolve_deliverable_address(db_session, "no-such-user") is None


# ---------------------------------------------------------------------------
# Sanitizing and rendering
# ---------------------------------------------------------------------------


class TestSanitize:
    def test_strips_newlines_and_controls(self) -> None:
        text = sanitize_display_text("Evil\r\nSubject: hi\x00\x1b[31m\u202eapp", 80)
        assert text is not None
        assert "\n" not in text and "\r" not in text and "\x00" not in text
        assert "\x1b" not in text and "\u202e" not in text
        assert text.startswith("Evil Subject: hi")

    def test_defangs_urls_and_hosts(self) -> None:
        text = sanitize_display_text("Visit https://evil.example/login or www.phish.test now", 200)
        assert text is not None
        assert "://" not in text
        assert "evil.example" not in text and "evil[.]example" in text
        assert "www." not in text.lower()
        assert "phish.test" not in text

    def test_keeps_version_numbers(self) -> None:
        assert sanitize_display_text("Claude Desktop 1.2.3", 80) == "Claude Desktop 1.2.3"

    def test_caps_length(self) -> None:
        text = sanitize_display_text("A" * 500, 80)
        assert text is not None and len(text) == 80 and text.endswith("…")

    def test_is_idempotent(self) -> None:
        once = sanitize_display_text("x https://a.example www.b.test\nC" + "z" * 300, 80)
        assert sanitize_display_text(once, 80) == once

    def test_empty(self) -> None:
        assert sanitize_display_text("\n\t ", 80) is None
        assert sanitize_display_text(None, 80) is None

    def test_ip(self) -> None:
        assert sns.sanitize_ip("2001:db8::1") == "2001:db8::1"
        assert sns.sanitize_ip("not\nan ip") == "not an ip"
        assert sns.sanitize_ip(None) is None

    @pytest.mark.asyncio
    async def test_malicious_client_name_and_ua_are_sanitized_before_the_email(
        self, redis, deliverable, email
    ) -> None:
        await _notify(
            email,
            SecurityEvent.OAUTH_CLIENT_AUTHORIZED,
            client_name="Support\nClick https://evil.example/reset " + "x" * 200,
            user_agent="Mozilla\r\nBcc: victim@example.test " + "y" * 400,
        )
        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.client_name is not None and len(occurrence.client_name) <= 80
        assert "\n" not in occurrence.client_name and "://" not in occurrence.client_name
        assert occurrence.user_agent is not None and len(occurrence.user_agent) <= 200
        assert "\r" not in occurrence.user_agent and "\n" not in occurrence.user_agent

    def test_buffer_round_trip_resanitizes(self) -> None:
        raw = (
            '{"occurred_at": "2026-09-30T10:00:00 UTC", "client_name": "a\\nhttps://x.example",'
            ' "unknown": 1}'
        )
        occurrence = SecurityOccurrence.from_json(raw)
        assert occurrence.client_name == "a https[:]//x[.]example"
        with pytest.raises(ValueError):
            SecurityOccurrence.from_json("[1, 2]")


RESET_LINK = "https://app.example/password/reset?token=abc123"


class TestRender:
    def _occurrence(self, **kwargs) -> SecurityOccurrence:
        base = {
            "occurred_at": "2026-09-30T10:00:00 UTC",
            "ip": "192.0.2.1",
            "user_agent": "UA/1",
        }
        base.update(kwargs)
        return SecurityOccurrence(**base)

    @pytest.mark.parametrize("event", list(SecurityEvent))
    def test_every_event_renders(self, event) -> None:
        subject, text = render_security_notification(
            event,
            [self._occurrence()],
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert subject and "\n" not in subject
        assert "2026-09-30T10:00:00 UTC" in text
        assert "192.0.2.1" in text and "UA/1" in text
        assert "Wasn't you?" in text
        assert "https://app.example/profile" in text
        assert "Forgot password?" in text
        assert "token" not in text.lower()
        assert "?" not in text.split("https://app.example/profile")[1].split("\n")[0]

    def test_names_client_key_method_and_admin(self) -> None:
        _, text = render_security_notification(
            SecurityEvent.API_KEY_CREATED,
            [
                self._occurrence(
                    key_name="ci",
                    client_name="Cursor",
                    sign_in_method="Google",
                    actor="Ada Admin (ada@example.test)",
                )
            ],
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert '"ci"' in text
        assert '"Cursor"' in text and "registered itself with" in text
        assert "Google" in text
        assert "Ada Admin (ada@example.test)" in text and "administrator" in text

    def test_digest_wording(self) -> None:
        subject, text = render_security_notification(
            SecurityEvent.PASSWORD_CHANGED,
            [self._occurrence(), self._occurrence()],
            digest=True,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert "2 more times" in subject
        assert "2 more times, within 10 minutes of the first" in text
        assert text.count("Your password was changed.") == 2

    def test_new_device_sign_in_reads_as_a_sign_in_not_a_change(self) -> None:
        # #1769: the opening line and the digest say "sign-in"; the method
        # line names how the browser signed in.
        subject, text = render_security_notification(
            SecurityEvent.NEW_DEVICE_SIGN_IN,
            [self._occurrence(sign_in_method="GitHub")],
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert "unrecognized device" in subject
        assert "signed in to from a device we had not" in text
        assert "security-sensitive change was made" not in text
        assert "Via:" in text and "GitHub" in text
        assert "cookies were cleared" in text
        # Recovery guidance fits every account: a reset (not a change) when a
        # password exists, the provider account for Google / GitHub sign-ins.
        assert 'reset it with "Forgot password?"' in text
        assert "Google or GitHub, secure that" in text
        assert "Change your password now" not in text

        _, digest = render_security_notification(
            SecurityEvent.NEW_DEVICE_SIGN_IN,
            [self._occurrence(), self._occurrence()],
            digest=True,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert "the same sign-in happened 2 more times" in digest

    def test_long_digest_is_capped(self) -> None:
        _, text = render_security_notification(
            SecurityEvent.API_KEY_CREATED,
            [self._occurrence()] * 25,
            digest=True,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert "... and 5 more." in text

    @pytest.mark.asyncio
    async def test_resend_body_carries_no_link_from_untrusted_input(
        self, redis, deliverable, monkeypatch
    ) -> None:
        # A reset link smuggled in through the user agent comes out defanged;
        # the only URL in the body is the plain profile page.
        import services.email_providers.resend as resend_module
        from services.email_providers.resend import ResendEmailService

        captured: dict = {}

        def _send(params):
            captured.update(params)
            return {"id": "msg-1"}

        monkeypatch.setattr(resend_module.resend.Emails, "send", _send)
        service = ResendEmailService(api_key="re_test", from_email="noreply@example.test")
        await _notify(service, key_name="deploy-bot", user_agent=RESET_LINK)

        assert captured["to"] == [ADDRESS]
        body = captured["text"]
        assert '"deploy-bot"' in body
        assert RESET_LINK not in body
        assert "https://app.example" not in body
        assert body.count("://") == 1  # the profile page
        assert f"{sns.profile_url()}\n" in body


class TestLoggingEmailService:
    @pytest.mark.asyncio
    async def test_logs_event_and_digest_only(self, monkeypatch) -> None:
        import services.email_service as email_module

        logger = MagicMock()
        monkeypatch.setattr(email_module, "logger", logger)
        sent = await LoggingEmailService().send_security_notification(
            to_email="owner@example.test",
            event="password_changed",
            occurrences=[SecurityOccurrence(occurred_at="t", ip="192.0.2.1", user_agent="UA")],
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert sent is True
        args, kwargs = logger.info.call_args
        assert args[0] == "security_notification_email"
        assert kwargs["security_event"] == "password_changed"
        rendered = repr(kwargs)
        assert "owner@example.test" not in rendered
        assert "192.0.2.1" not in rendered


# ---------------------------------------------------------------------------
# Review follow-ups: sanitizer bypasses, buffer cap, digest retry
# ---------------------------------------------------------------------------


class TestSanitizeUnicode:
    @pytest.mark.parametrize("dot", ["。", "．", "｡", "﹒", "․"], ids=lambda d: f"U+{ord(d):04X}")
    def test_dot_lookalikes_are_folded_and_defanged(self, dot) -> None:
        text = sanitize_display_text(f"evil{dot}example", 80)
        assert text == "evil[.]example"

    def test_fullwidth_scheme_is_folded_and_defanged(self) -> None:
        text = sanitize_display_text("ｈｔｔｐｓ：／／evil．example", 80)
        assert text == "https[:]//evil[.]example"

    @pytest.mark.parametrize(
        "hidden",
        ["؜", "­", "᠎", "​", "⁦", "\U000e0041", "͏", ""],
        ids=lambda c: f"U+{ord(c):04X}",
    )
    def test_hidden_characters_are_removed_and_cannot_split_a_host(self, hidden) -> None:
        text = sanitize_display_text(f"evil{hidden}.exa{hidden}mple", 80)
        assert text == "evil[.]example"

    def test_line_separators_become_spaces(self) -> None:
        assert sanitize_display_text("a b c\x85d", 80) == "a b c d"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("pаypal.com", "pаypal[.]com"),  # Cyrillic a
            ("evil.рф", "evil[.]рф"),  # .рф
            ("例え.jp", "例え[.]jp"),  # 例え.jp
            ("evil.сom", "evil[.]сom"),  # Cyrillic es
            ("a.xn--p1ai", "a[.]xn--p1ai"),
            ("1.2.3.4/login", "1[.]2[.]3[.]4/login"),
        ],
    )
    def test_unicode_hosts_and_ipv4_are_defanged(self, raw, expected) -> None:
        assert sanitize_display_text(raw, 80) == expected

    @pytest.mark.parametrize(
        "raw",
        ["pаypal.com 1.2.3.4", "ｗｗｗ．evil。jp", "x​.y­.example"],
    )
    def test_still_idempotent(self, raw) -> None:
        once = sanitize_display_text(raw, 80)
        assert sanitize_display_text(once, 80) == once


class TestBufferCap:
    @pytest.mark.asyncio
    async def test_buffer_is_capped_and_the_total_counted(self, redis, deliverable, email) -> None:
        for i in range(sns._DIGEST_MAX_OCCURRENCES + 6):
            await _notify(email, key_name=f"k{i}")
        buffered = sns._DIGEST_MAX_OCCURRENCES + 5  # all but the first
        wid = await _wid(redis)
        assert await redis.llen(_k(sns._BUFFER_KEY, wid)) == sns._DIGEST_MAX_OCCURRENCES
        count_key = _k(sns._COUNT_KEY, wid)
        assert int(await redis.get(count_key)) == buffered
        assert await redis.ttl(count_key) > 0

        email.send_security_notification.reset_mock()
        await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        kwargs = email.send_security_notification.await_args.kwargs
        assert len(kwargs["occurrences"]) == sns._DIGEST_MAX_OCCURRENCES
        assert kwargs["total"] == buffered
        assert await _window_keys(redis) == []

    def test_render_reports_the_rest_from_the_total(self) -> None:
        occurrence = SecurityOccurrence(occurred_at="t")
        subject, text = render_security_notification(
            SecurityEvent.API_KEY_CREATED,
            [occurrence] * 20,
            digest=True,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
            total=57,
        )
        assert "57 more times" in subject
        assert "... and 37 more." in text


class TestDigestRetry:
    async def _open_window_with_two(self, redis, email) -> str:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        await _notify(email, key_name="third")
        email.send_security_notification.reset_mock()
        wid = await _wid(redis)
        assert wid is not None
        return wid

    @pytest.mark.asyncio
    async def test_failed_send_keeps_the_occurrences_and_retries(
        self, redis, deliverable, email
    ) -> None:
        wid = await self._open_window_with_two(redis, email)
        email.send_security_notification.return_value = False
        now = _window_end() + 1

        assert (
            await sns.flush_due_security_notifications(
                now_score=now, session_factory=_factory(), email_service=email
            )
            == 0
        )

        # The claim keeps them under the window's own keys, re-queued.
        assert await redis.llen(_k(sns._CLAIM_BUFFER_KEY, wid)) == 2
        assert int(await redis.get(_k(sns._CLAIM_COUNT_KEY, wid))) == 2
        assert await redis.exists(_k(sns._BUFFER_KEY, wid)) == 0
        retry_at = await redis.zscore(sns._DUE_KEY, _m(wid))
        assert retry_at == now + sns._DIGEST_RETRY_DELAY_SECONDS

        # Not retried before its time; retried (and sent) after it.
        email.send_security_notification.return_value = True
        assert (
            await sns.flush_due_security_notifications(
                now_score=now + 1, session_factory=_factory(), email_service=email
            )
            == 0
        )
        assert (
            await sns.flush_due_security_notifications(
                now_score=retry_at, session_factory=_factory(), email_service=email
            )
            == 1
        )
        kwargs = email.send_security_notification.await_args.kwargs
        assert [o.key_name for o in kwargs["occurrences"]] == ["second", "third"]
        assert kwargs["total"] == 2
        assert await _window_keys(redis) == []
        assert await redis.zcard(sns._DUE_KEY) == 0

    @pytest.mark.asyncio
    async def test_recipient_lookup_failure_is_retried(self, redis, deliverable, email) -> None:
        wid = await self._open_window_with_two(redis, email)
        deliverable.side_effect = OSError("db down")
        await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        assert await redis.llen(_k(sns._CLAIM_BUFFER_KEY, wid)) == 2
        assert await redis.zscore(sns._DUE_KEY, _m(wid)) is not None

    @pytest.mark.asyncio
    async def test_dropped_after_the_last_attempt(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await self._open_window_with_two(redis, email)
        email.send_security_notification.side_effect = RuntimeError("provider down")
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        now = _window_end() + 1
        for _ in range(sns._DIGEST_MAX_ATTEMPTS):
            await sns.flush_due_security_notifications(
                now_score=now, session_factory=_factory(), email_service=email
            )
            now += 10 * sns._DIGEST_RETRY_DELAY_SECONDS

        assert email.send_security_notification.await_count == sns._DIGEST_MAX_ATTEMPTS
        assert await redis.zcard(sns._DUE_KEY) == 0
        assert await _window_keys(redis) == []
        dropped = [
            c
            for c in logger.error.call_args_list
            if c.args[0] == "security_notification_digest_dropped"
        ]
        assert len(dropped) == 1

    @pytest.mark.asyncio
    async def test_timed_out_digest_counts_as_sent(self, redis, deliverable, email) -> None:
        # wait_for cannot stop a provider call already running in a thread,
        # so a timed-out digest may still arrive: never retried.
        await self._open_window_with_two(redis, email)
        email.send_security_notification.side_effect = TimeoutError()
        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        assert sent == 0
        email.send_security_notification.assert_awaited_once()
        assert await redis.zcard(sns._DUE_KEY) == 0
        assert await _window_keys(redis) == []

    @pytest.mark.asyncio
    async def test_late_occurrence_in_the_retried_window_joins_its_claim(
        self, redis, deliverable, email
    ) -> None:
        wid = await self._open_window_with_two(redis, email)
        email.send_security_notification.return_value = False
        now = _window_end() + 1
        await sns.flush_due_security_notifications(
            now_score=now, session_factory=_factory(), email_service=email
        )
        # A pusher that raced the claim left an occurrence in the window's buffer.
        late = SecurityOccurrence(occurred_at="t", key_name="late").to_json()
        await redis.rpush(_k(sns._BUFFER_KEY, wid), late)
        await redis.incr(_k(sns._COUNT_KEY, wid))

        email.send_security_notification.return_value = True
        await sns.flush_due_security_notifications(
            now_score=now + sns._DIGEST_RETRY_DELAY_SECONDS,
            session_factory=_factory(),
            email_service=email,
        )
        kwargs = email.send_security_notification.await_args.kwargs
        assert [o.key_name for o in kwargs["occurrences"]] == ["second", "third", "late"]
        assert kwargs["total"] == 3
        assert await _window_keys(redis) == []


class TestWindowGenerations:
    @pytest.mark.asyncio
    async def test_a_new_window_opened_during_a_flush_keeps_its_own_occurrences(
        self, redis, deliverable, email
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        old = await _wid(redis)
        email.send_security_notification.reset_mock()
        opened: list[tuple[str, str | None]] = []

        async def _send(**kwargs):
            # While the old window's digest is being sent (its pointer already
            # released), a new occurrence opens a new window and a repeat is
            # buffered in it.
            if kwargs["digest"]:
                for name in ("third", "fourth"):
                    opened.append(
                        await sns._buffer_if_window_open(
                            OWNER,
                            SecurityEvent.API_KEY_CREATED,
                            SecurityOccurrence(occurred_at="t", key_name=name),
                        )
                    )
            return True

        email.send_security_notification = AsyncMock(side_effect=_send)
        await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        digest = email.send_security_notification.await_args_list[0].kwargs
        assert [o.key_name for o in digest["occurrences"]] == ["second"]
        (open_outcome, new), (buffered_outcome, same) = opened
        assert open_outcome == sns._OPENED and buffered_outcome == sns._BUFFERED
        assert new == same and new != old
        # The new window's repeat is untouched by the old flush ...
        assert await redis.llen(_k(sns._BUFFER_KEY, new)) == 1
        assert await redis.zscore(sns._DUE_KEY, _m(new)) is not None
        # ... and goes out in the new window's own digest.
        email.send_security_notification = AsyncMock(return_value=True)
        await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        kwargs = email.send_security_notification.await_args.kwargs
        assert [o.key_name for o in kwargs["occurrences"]] == ["fourth"]


class TestFlushResilience:
    @pytest.mark.asyncio
    async def test_redis_failure_after_the_claim_requeues_the_window(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        wid = await _wid(redis)
        email.send_security_notification.reset_mock()
        # Redis fails right after the claim transaction (reading the claim).
        real_lrange = redis.lrange
        failed = False

        async def _lrange_once_broken(*args):
            nonlocal failed
            if not failed:
                failed = True
                raise ConnectionError("redis blip")
            return await real_lrange(*args)

        monkeypatch.setattr(redis, "lrange", _lrange_once_broken)
        now = _window_end() + 1
        assert (
            await sns.flush_due_security_notifications(
                now_score=now, session_factory=_factory(), email_service=email
            )
            == 0
        )
        retry_at = await redis.zscore(sns._DUE_KEY, _m(wid))
        assert retry_at == now + sns._DIGEST_RETRY_DELAY_SECONDS

        assert (
            await sns.flush_due_security_notifications(
                now_score=retry_at, session_factory=_factory(), email_service=email
            )
            == 1
        )
        kwargs = email.send_security_notification.await_args.kwargs
        assert [o.key_name for o in kwargs["occurrences"]] == ["second"]
        assert await _window_keys(redis) == []

    @pytest.mark.asyncio
    async def test_failed_requeue_is_logged(self, redis, deliverable, email, monkeypatch) -> None:
        await _notify(email)
        await _notify(email)
        email.send_security_notification.reset_mock()
        monkeypatch.setattr(redis, "lrange", AsyncMock(side_effect=ConnectionError("down")))
        monkeypatch.setattr(redis, "incr", AsyncMock(side_effect=ConnectionError("down")))
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)

        await sns.flush_due_security_notifications(  # does not raise
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        events = [c.args[0] for c in logger.error.call_args_list]
        assert "security_notification_flush_failed" in events
        assert "security_notification_requeue_failed" in events

    @pytest.mark.asyncio
    async def test_a_run_stops_claiming_when_over_its_time_budget(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        for user in ("u-a", "u-b", "u-c"):
            await _notify(email, user_id=user)
            await _notify(email, user_id=user)
        email.send_security_notification.reset_mock()
        # The first window fits the budget; then the clock is past it.
        clock = iter([0.0, 0.0, sns._FLUSH_TIME_BUDGET_SECONDS + 1])
        monkeypatch.setattr(sns, "_monotonic", lambda: next(clock, 10_000.0))

        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        assert sent == 1
        assert await redis.zcard(sns._DUE_KEY) == 2  # left for the next run

    def test_batch_is_bounded(self) -> None:
        assert sns._FLUSH_BATCH <= 50
        assert sns._FLUSH_TIME_BUDGET_SECONDS < 60  # the job runs every minute


# ---------------------------------------------------------------------------
# gate2 follow-ups: failed opening notice, orphaned claims, wording
# ---------------------------------------------------------------------------


class TestFailedOpeningNotice:
    @pytest.mark.parametrize(
        "failure",
        [{"return_value": False}, {"side_effect": RuntimeError("provider down")}],
        ids=["returned_false", "raised"],
    )
    @pytest.mark.asyncio
    async def test_window_is_closed_and_the_next_occurrence_sent_at_once(
        self, redis, deliverable, email, failure
    ) -> None:
        email.send_security_notification = AsyncMock(**failure)
        await _notify(email, key_name="first")

        # The window is gone; only the failed notice's retry is pending.
        assert await _wid(redis) is None
        assert await redis.zcard(sns._DUE_KEY) == 1

        email.send_security_notification = AsyncMock(return_value=True)
        await _notify(email, key_name="second")
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is False
        assert [o.key_name for o in kwargs["occurrences"]] == ["second"]
        assert await _wid(redis) is not None

    @pytest.mark.asyncio
    async def test_timed_out_opening_send_keeps_the_window(self, redis, deliverable, email) -> None:
        # The timed-out email may still arrive, so the window stays open, the
        # next occurrence is coalesced (no duplicate) and nothing is retried.
        email.send_security_notification = AsyncMock(side_effect=TimeoutError())
        await _notify(email, key_name="first")
        wid = await _wid(redis)
        assert wid is not None
        assert await redis.zcard(sns._DUE_KEY) == 0

        email.send_security_notification = AsyncMock(return_value=True)
        await _notify(email, key_name="second")
        email.send_security_notification.assert_not_awaited()
        assert await redis.llen(_k(sns._BUFFER_KEY, wid)) == 1
        assert await redis.zscore(sns._DUE_KEY, _m(wid)) is not None

    @pytest.mark.asyncio
    async def test_occurrence_buffered_during_failed_opening_send_is_retried_with_it(
        self, redis, deliverable
    ) -> None:
        # A second occurrence is buffered while the opening send is in flight;
        # that send fails, the window closes, and both wait for the retry —
        # nothing is sent again straight into the failing provider.
        calls: list[dict] = []

        async def _send(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                await sns._buffer_if_window_open(
                    OWNER,
                    SecurityEvent.API_KEY_CREATED,
                    SecurityOccurrence(occurred_at="t", key_name="in-flight"),
                )
                return False
            return True

        email = AsyncMock()
        email.send_security_notification = _send
        await _notify(email, key_name="first")
        assert len(calls) == 1
        assert await _wid(redis) is None

        sent = await sns.flush_due_security_notifications(
            now_score=sns._now_score() + sns._DIGEST_RETRY_DELAY_SECONDS + 1,
            session_factory=_factory(),
            email_service=email,
        )

        assert sent == 1
        retry = calls[1]
        assert retry["digest"] is False
        assert [o.key_name for o in retry["occurrences"]] == ["first", "in-flight"]
        assert retry["total"] == 2
        assert await _window_keys(redis) == []
        assert await redis.zcard(sns._DUE_KEY) == 0

    @pytest.mark.asyncio
    async def test_recipient_lookup_failure_opens_no_window(
        self, redis, email, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            sns, "resolve_deliverable_address", AsyncMock(side_effect=OSError("db down"))
        )
        await _notify(email)
        assert await redis.zcard(sns._DUE_KEY) == 0

    @pytest.mark.asyncio
    async def test_redis_down_failure_does_not_touch_windows(
        self, monkeypatch, deliverable
    ) -> None:
        broken = MagicMock()
        broken.pipeline = MagicMock(side_effect=ConnectionError("redis down"))
        monkeypatch.setattr(sns, "get_redis_client", lambda: broken)
        email = AsyncMock()
        email.send_security_notification = AsyncMock(return_value=False)
        await _notify(email)  # does not raise, does not try to close
        # The record transaction and the attempt to park the retry; no close
        # transaction.
        assert broken.pipeline.call_count == 2


class TestAuthorizedWording:
    def test_app_authorization_does_not_claim_the_app_is_new(self) -> None:
        subject, text = render_security_notification(
            SecurityEvent.OAUTH_CLIENT_AUTHORIZED,
            [SecurityOccurrence(occurred_at="t", client_name="kagura-cli")],
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert subject == "An app was authorized to access your Kagura account"
        assert "An app was authorized to access your account." in text
        assert "new app" not in (subject + text).lower()


# ---------------------------------------------------------------------------
# Copilot round 2: atomic transitions, expiry, stranded pointers, cleanup,
# erasure
# ---------------------------------------------------------------------------


def _conflict_on_read(redis, monkeypatch, write, *, times: int = 1, method: str = "zscore") -> dict:
    """Make ``write`` run (from another connection) after a transaction's
    watched read (``method``), ``times`` times; a write to a watched key makes
    its EXEC fail with WatchError. ``state["transactions"]`` counts attempts."""
    real_pipeline = redis.pipeline
    state = {"conflicts": 0, "transactions": 0}

    def _pipeline(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        state["transactions"] += 1
        real_read = getattr(pipe, method)

        async def _read(*rargs, **rkwargs):
            result = await real_read(*rargs, **rkwargs)
            if state["conflicts"] < times:
                state["conflicts"] += 1
                await write()
            return result

        setattr(pipe, method, _read)
        return pipe

    monkeypatch.setattr(redis, "pipeline", _pipeline)
    return state


class TestAtomicTransitions:
    @pytest.mark.asyncio
    async def test_record_retries_after_a_concurrent_claim(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        old = await _wid(redis)
        email.send_security_notification.reset_mock()

        async def _claim_elsewhere() -> None:
            # A flush claims the window between the read and the EXEC.
            await redis.zrem(sns._DUE_KEY, _m(old))
            await redis.delete(sns._open_key(OWNER, EVENT))

        state = _conflict_on_read(redis, monkeypatch, _claim_elsewhere, method="get")
        await _notify(email, key_name="late")

        assert state["conflicts"] == 1
        # The retry saw no window: it opened a new one and sent at once; the
        # claimed window's buffer was never written to.
        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.key_name == "late"
        new = await _wid(redis)
        assert new is not None and new != old
        assert await redis.exists(_k(sns._BUFFER_KEY, old)) == 0

    @pytest.mark.asyncio
    async def test_claim_retries_and_keeps_a_concurrent_repeat(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        wid = await _wid(redis)
        email.send_security_notification.reset_mock()

        async def _repeat_lands() -> None:
            # A producer buffers a repeat between the claim's read and EXEC.
            payload = SecurityOccurrence(occurred_at="t", key_name="racing").to_json()
            await redis.rpush(_k(sns._BUFFER_KEY, wid), payload)
            await redis.incr(_k(sns._COUNT_KEY, wid))

        state = _conflict_on_read(redis, monkeypatch, _repeat_lands)
        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        assert state["conflicts"] == 1
        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert [o.key_name for o in kwargs["occurrences"]] == ["second", "racing"]
        assert kwargs["total"] == 2
        assert await _window_keys(redis) == []

    @pytest.mark.asyncio
    async def test_record_that_keeps_conflicting_sends_at_once(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        wid = await _wid(redis)
        email.send_security_notification.reset_mock()
        deadlines = iter(range(1, 100))

        async def _rewrite_pointer() -> None:
            # Something rewrites this (user, event)'s pointer on every attempt.
            deadline = _window_end() + next(deadlines)
            await redis.set(sns._open_key(OWNER, EVENT), sns._pointer_value(wid, deadline))

        _conflict_on_read(redis, monkeypatch, _rewrite_pointer, times=sns._TX_RETRIES, method="get")
        await _notify(email, key_name="contended")

        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is False
        assert [o.key_name for o in kwargs["occurrences"]] == ["contended"]

    @pytest.mark.asyncio
    async def test_other_users_windows_never_force_a_retry(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        wid = await _wid(redis)
        email.send_security_notification.reset_mock()
        touches = iter(range(1, 100))

        async def _unrelated_activity() -> None:
            # Another user's window opens, and the flush claims yet another.
            n = next(touches)
            await redis.zadd(sns._DUE_KEY, {f"other-{n}|api_key_created|" + "0" * 32: n})
            await redis.set(sns._open_key(f"other-{n}", EVENT), sns._pointer_value("0" * 32, 1e12))

        state = _conflict_on_read(redis, monkeypatch, _unrelated_activity, method="get")
        await _notify(email, key_name="repeat")

        assert state["conflicts"] == 1
        assert state["transactions"] == 1  # no retry
        email.send_security_notification.assert_not_awaited()
        assert await redis.llen(_k(sns._BUFFER_KEY, wid)) == 1


class TestWindowExpiryAndRepair:
    @pytest.mark.asyncio
    async def test_repeat_after_the_deadline_opens_a_new_window(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        old = await _wid(redis)
        email.send_security_notification.reset_mock()

        # The flush has not run yet, but the old window's deadline has passed.
        later = _window_end() + 5
        monkeypatch.setattr(sns, "_now_score", lambda: later)
        await _notify(email, key_name="after-deadline")

        # Sent at once as a new window's opening notice ...
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is False
        assert [o.key_name for o in kwargs["occurrences"]] == ["after-deadline"]
        new = await _wid(redis)
        assert new != old
        # ... and the old window still holds only its own repeat.
        assert await redis.llen(_k(sns._BUFFER_KEY, old)) == 1

        email.send_security_notification.reset_mock()
        await sns.flush_due_security_notifications(
            now_score=later, session_factory=_factory(), email_service=email
        )
        (digest_call,) = email.send_security_notification.await_args_list
        assert digest_call.kwargs["digest"] is True
        assert [o.key_name for o in digest_call.kwargs["occurrences"]] == ["second"]
        # The old flush left the new window's pointer alone.
        assert await _wid(redis) == new

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stranded", ["f" * 32, "not-a-window", "f" * 32 + ":soon"])
    async def test_unreadable_pointer_is_replaced_by_the_next_occurrence(
        self, redis, deliverable, email, stranded
    ) -> None:
        # A pointer without a deadline (an older format, or garbage) names no
        # window a flush would ever close: the next occurrence opens one.
        await redis.set(sns._open_key(OWNER, EVENT), stranded)

        await _notify(email, key_name="next")

        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is False
        assert [o.key_name for o in kwargs["occurrences"]] == ["next"]
        wid = await _wid(redis)
        assert wid is not None and wid != stranded
        assert await redis.exists(_k(sns._BUFFER_KEY, stranded)) == 0


class TestPostSendCleanup:
    @pytest.mark.parametrize("result", [{"return_value": True}, {"side_effect": TimeoutError()}])
    @pytest.mark.asyncio
    async def test_cleanup_error_does_not_requeue(
        self, redis, deliverable, email, monkeypatch, result
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        email.send_security_notification = AsyncMock(**result)
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        monkeypatch.setattr(redis, "delete", AsyncMock(side_effect=ConnectionError("blip")))

        await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        email.send_security_notification.assert_awaited_once()
        assert await redis.zcard(sns._DUE_KEY) == 0  # not re-queued
        warnings = [c.args[0] for c in logger.warning.call_args_list]
        assert "security_notification_cleanup_failed" in warnings
        errors = [c.args[0] for c in logger.error.call_args_list]
        assert "security_notification_flush_failed" not in errors


class TestPurgeUserState:
    @pytest.mark.asyncio
    async def test_purge_removes_every_key_of_the_user_only(
        self, redis, deliverable, email
    ) -> None:
        # A pending window with a buffered repeat, a retried claim, and another
        # user whose id shares the prefix.
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        await _notify(email, SecurityEvent.PASSWORD_CHANGED)
        await _notify(email, user_id=f"{OWNER}:x", key_name="neighbour")
        await _notify(email, user_id=f"{OWNER}:x", key_name="neighbour-2")
        await redis.rpush(_k(sns._CLAIM_BUFFER_KEY, "a" * 32), "{}")
        await redis.set(_k(sns._ATTEMPTS_KEY, "a" * 32), 1)

        removed = await sns.purge_user_notification_state(OWNER)

        assert removed > 0
        remaining = await _window_keys(redis)
        assert remaining and all(f"{OWNER}:x" in k for k in remaining)
        members = await redis.zrange(sns._DUE_KEY, 0, -1)
        assert members and all(m.startswith(f"{OWNER}:x|") for m in members)

    @pytest.mark.asyncio
    async def test_purge_never_raises(self, monkeypatch) -> None:
        def _boom():
            raise ConnectionError("redis down")

        monkeypatch.setattr(sns, "get_redis_client", _boom)
        assert await sns.purge_user_notification_state(OWNER) == 0

    @pytest.mark.asyncio
    async def test_account_erasure_clears_a_pending_window(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        from services import account_erasure_service as erasure_module

        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        assert await redis.zcard(sns._DUE_KEY) == 1

        monkeypatch.setattr("api.routes.auth.get_session_manager", lambda: None)
        monkeypatch.setattr(erasure_module, "clear_co_activations", AsyncMock(return_value=0))
        monkeypatch.setattr(erasure_module, "clear_user_rate_limits", AsyncMock(return_value=0))
        service = erasure_module.AccountErasureService.__new__(erasure_module.AccountErasureService)

        summary = await service._clear_redis(OWNER)

        assert summary["security_notices"] > 0
        assert await redis.zcard(sns._DUE_KEY) == 0
        assert await _window_keys(redis) == []


# ---------------------------------------------------------------------------
# Copilot round 3: early claims of re-queued windows, stalled scheduler
# ---------------------------------------------------------------------------


class TestRequeuedWindowNotClaimedEarly:
    @pytest.mark.asyncio
    async def test_second_scheduler_cannot_claim_before_the_retry_time(
        self, redis, deliverable, email
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        wid = await _wid(redis)
        email.send_security_notification.reset_mock()
        now = _window_end() + 1

        # Scheduler B lists the window while it is due ...
        listed = await redis.zrangebyscore(sns._DUE_KEY, "-inf", now)
        assert listed == [_m(wid)]
        # ... scheduler A claims it, the send fails, it is re-queued for later.
        email.send_security_notification.return_value = False
        await sns.flush_due_security_notifications(
            now_score=now, session_factory=_factory(), email_service=email
        )
        retry_at = await redis.zscore(sns._DUE_KEY, _m(wid))
        assert retry_at is not None and retry_at > now

        # B now tries to claim what it listed earlier: not due, not claimed.
        assert await sns._claim_window(redis, OWNER, EVENT, wid, now=now) is False
        assert await redis.zscore(sns._DUE_KEY, _m(wid)) == retry_at
        # At the retry time it can.
        assert await sns._claim_window(redis, OWNER, EVENT, wid, now=retry_at) is True


class TestStalledScheduler:
    def test_pending_state_outlives_a_stalled_job(self) -> None:
        assert sns._STATE_RETENTION_SECONDS >= 7 * 24 * 60 * 60
        # Keys outlive the stale cutoff (deadline + retention) even when
        # written at the window's start (Copilot review).
        assert sns._buffer_ttl() >= sns._STATE_RETENTION_SECONDS + sns._window_seconds()

    @pytest.mark.asyncio
    async def test_buffer_ttl_is_the_retention(self, redis, deliverable, email) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        wid = await _wid(redis)
        ttl = await redis.ttl(_k(sns._BUFFER_KEY, wid))
        assert sns._buffer_ttl() - 5 < ttl <= sns._buffer_ttl()

    @pytest.mark.asyncio
    async def test_count_without_items_still_sends_a_digest(
        self, redis, deliverable, email
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        await _notify(email, key_name="third")
        wid = await _wid(redis)
        await redis.delete(_k(sns._BUFFER_KEY, wid))  # the details expired
        email.send_security_notification.reset_mock()

        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is True
        assert kwargs["occurrences"] == []
        assert kwargs["total"] == 2
        assert await _window_keys(redis) == []

    def test_render_says_occurrences_could_not_be_listed(self) -> None:
        subject, text = render_security_notification(
            SecurityEvent.API_KEY_CREATED,
            [],
            digest=True,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
            total=2,
        )
        assert "2 more times" in subject
        assert "2 occurrences of this change could not be listed" in text
        assert "Wasn't you?" in text

    @pytest.mark.asyncio
    async def test_entries_older_than_the_retention_are_dropped_loudly(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        email.send_security_notification.reset_mock()
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        much_later = _window_end() + sns._STATE_RETENTION_SECONDS + 60

        sent = await sns.flush_due_security_notifications(
            now_score=much_later, session_factory=_factory(), email_service=email
        )

        assert sent == 0
        email.send_security_notification.assert_not_awaited()
        assert await redis.zcard(sns._DUE_KEY) == 0
        assert await _window_keys(redis) == []
        expired = [
            c
            for c in logger.warning.call_args_list
            if c.args[0] == "security_notification_window_expired"
        ]
        assert len(expired) == 1
        assert expired[0].kwargs == {"user_id": OWNER, "security_event": EVENT}

    @pytest.mark.asyncio
    async def test_expiry_keeps_a_newer_window_of_the_same_user(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="old")
        await _notify(email, key_name="old-repeat")
        old = await _wid(redis)
        # Long after, with the old entry never flushed, a new window opens.
        much_later = _window_end() + sns._STATE_RETENTION_SECONDS + 60
        monkeypatch.setattr(sns, "_now_score", lambda: much_later)
        await _notify(email, key_name="new")
        new = await _wid(redis)
        assert new != old

        await sns.flush_due_security_notifications(
            now_score=much_later, session_factory=_factory(), email_service=email
        )

        assert await redis.zscore(sns._DUE_KEY, _m(old)) is None
        assert await _wid(redis) == new


# ---------------------------------------------------------------------------
# Copilot round 4: stale sweep vs a concurrent claim, bounded host matching,
# provider timeouts
# ---------------------------------------------------------------------------


class TestStaleSweepRace:
    @pytest.mark.asyncio
    async def test_sweep_leaves_a_window_claimed_meanwhile_by_another_replica(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        wid = await _wid(redis)
        assert wid is not None
        much_later = _window_end() + sns._STATE_RETENTION_SECONDS + 60

        async def _claim_elsewhere() -> None:
            # Another replica's claim: the due entry goes, the buffer moves.
            await redis.zrem(sns._DUE_KEY, _m(wid))
            await redis.rename(_k(sns._BUFFER_KEY, wid), _k(sns._CLAIM_BUFFER_KEY, wid))

        _conflict_on_read(redis, monkeypatch, _claim_elsewhere, method="get")
        await sns._expire_stale_windows(redis, now=much_later)

        assert await redis.lrange(_k(sns._CLAIM_BUFFER_KEY, wid), 0, -1)

    @pytest.mark.asyncio
    async def test_sweep_leaves_an_entry_requeued_meanwhile(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        wid = await _wid(redis)
        assert wid is not None
        much_later = _window_end() + sns._STATE_RETENTION_SECONDS + 60

        async def _requeue_elsewhere() -> None:
            await redis.zadd(sns._DUE_KEY, {_m(wid): much_later + 60})

        _conflict_on_read(redis, monkeypatch, _requeue_elsewhere, method="get")
        await sns._expire_stale_windows(redis, now=much_later)

        assert await redis.zscore(sns._DUE_KEY, _m(wid)) == much_later + 60
        assert await redis.lrange(_k(sns._BUFFER_KEY, wid), 0, -1)


class _LengthRecordingPattern:
    def __init__(self, pattern) -> None:
        self._pattern = pattern
        self.lengths: list[int] = []

    def sub(self, repl, text):
        self.lengths.append(len(text))
        return self._pattern.sub(repl, text)


class TestBoundedHostMatching:
    def test_host_patterns_only_see_a_bounded_prefix(self, monkeypatch) -> None:
        recorder = _LengthRecordingPattern(sns._HOST_RE)
        monkeypatch.setattr(sns, "_HOST_RE", recorder)
        text = sanitize_display_text("a." * 50_000 + "1", 200)
        assert text is not None and len(text) <= 200 and text.endswith("…")
        assert recorder.lengths and max(recorder.lengths) <= 200

    def test_host_at_the_cut_is_still_defanged(self) -> None:
        text = sanitize_display_text("x" * 170 + " evil.example.com/login " + "y" * 500, 200)
        assert text is not None and len(text) <= 200 and text.endswith("…")
        assert "evil.example" not in text
        assert "evil[.]example" in text

    def test_cut_input_that_shrinks_below_the_cap_keeps_the_ellipsis(self) -> None:
        text = sanitize_display_text("A" * 199 + "​" * 10 + "B" * 50, 200)
        assert text is not None and text.endswith("…")

    def test_still_idempotent_after_a_pre_cut(self) -> None:
        once = sanitize_display_text("go to a.example " * 40, 80)
        assert sanitize_display_text(once, 80) == once


def _sdk_http_error(inner: Exception):
    """What resend.Emails.send raises on a transport error: ResendError
    (HttpClientError) raised while handling RuntimeError from ``inner``."""
    import resend

    def _send(params):
        try:
            try:
                raise inner
            except Exception as exc:
                raise RuntimeError(f"Request failed: {exc}") from exc
        except Exception as exc:
            raise resend.exceptions.ResendError(
                code=500,
                message=str(exc),
                error_type="HttpClientError",
                suggested_action="Request failed, please try again.",
            ) from exc

    return _send


class TestProviderTimeout:
    def _send_with(self, monkeypatch, inner: Exception):
        import services.email_providers.resend as resend_module
        from services.email_providers.resend import ResendEmailService

        monkeypatch.setattr(resend_module.resend.Emails, "send", _sdk_http_error(inner))
        service = ResendEmailService(api_key="re_test", from_email="noreply@example.test")
        return _send_notice(service)

    @pytest.mark.asyncio
    async def test_read_timeout_is_uncertain(self, monkeypatch) -> None:
        import requests

        with pytest.raises(TimeoutError):
            await self._send_with(monkeypatch, requests.exceptions.ReadTimeout("read timed out"))

    @pytest.mark.asyncio
    async def test_connect_timeout_is_a_definite_failure(self, monkeypatch) -> None:
        import requests

        sent = await self._send_with(
            monkeypatch, requests.exceptions.ConnectTimeout("connect timed out")
        )
        assert sent is False

    @pytest.mark.asyncio
    async def test_other_emails_keep_returning_false_on_a_read_timeout(self, monkeypatch) -> None:
        import requests

        import services.email_providers.resend as resend_module
        from services.email_providers.resend import ResendEmailService

        monkeypatch.setattr(
            resend_module.resend.Emails,
            "send",
            _sdk_http_error(requests.exceptions.ReadTimeout("read timed out")),
        )
        service = ResendEmailService(api_key="re_test", from_email="noreply@example.test")
        assert await service.send_erasure_receipt(to_email=ADDRESS, request_id="r-1") is False

    @pytest.mark.asyncio
    async def test_digest_whose_provider_call_timed_out_is_not_resent(
        self, redis, deliverable, monkeypatch
    ) -> None:
        import requests

        import services.email_providers.resend as resend_module
        from services.email_providers.resend import ResendEmailService

        service = ResendEmailService(api_key="re_test", from_email="noreply@example.test")
        monkeypatch.setattr(resend_module.resend.Emails, "send", lambda params: {"id": "m-1"})
        await _notify(service, key_name="first")
        await _notify(service, key_name="second")
        calls = []

        def _timed_out(params):
            calls.append(params)
            return _sdk_http_error(requests.exceptions.ReadTimeout("read timed out"))(params)

        monkeypatch.setattr(resend_module.resend.Emails, "send", _timed_out)
        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=service
        )

        assert sent == 0
        assert len(calls) == 1
        assert await redis.zcard(sns._DUE_KEY) == 0


def _send_notice(service):
    return service.send_security_notification(
        to_email=ADDRESS,
        event=EVENT,
        occurrences=[SecurityOccurrence(occurred_at="2026-09-30T00:00:00 UTC")],
        digest=False,
        window_minutes=10,
        profile_page_url="https://app.example/profile",
    )


# ---------------------------------------------------------------------------
# Copilot round 5: per-window claim lock instead of watching the shared due
# set; a stale backlog larger than one batch
# ---------------------------------------------------------------------------


class TestPerWindowClaimLock:
    async def _due_window(self, redis, email) -> str:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        email.send_security_notification.reset_mock()
        wid = await _wid(redis)
        assert wid is not None
        return wid

    @pytest.mark.asyncio
    async def test_unrelated_due_writes_do_not_force_a_claim_retry(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        wid = await self._due_window(redis, email)

        async def _other_account_opens_a_window() -> None:
            await redis.zadd(sns._DUE_KEY, {sns._member("someone-else", EVENT, "f" * 32): 1.0})

        state = _conflict_on_read(redis, monkeypatch, _other_account_opens_a_window)
        claimed = await sns._claim_window(redis, OWNER, EVENT, wid, now=_window_end() + 1)

        assert claimed is True
        assert state["transactions"] == 2  # one claim attempt (no retry) + the lock release
        assert await redis.lrange(_k(sns._CLAIM_BUFFER_KEY, wid), 0, -1)

    @pytest.mark.asyncio
    async def test_a_held_lock_defers_the_claim(self, redis, deliverable, email) -> None:
        wid = await self._due_window(redis, email)
        await redis.set(_k(sns._LOCK_KEY, wid), "other-replica")

        claimed = await sns._claim_window(redis, OWNER, EVENT, wid, now=_window_end() + 1)

        assert claimed is False
        assert await redis.zscore(sns._DUE_KEY, _m(wid)) is not None
        assert await redis.get(_k(sns._LOCK_KEY, wid)) == "other-replica"

    @pytest.mark.asyncio
    async def test_the_lock_is_released_after_a_claim(self, redis, deliverable, email) -> None:
        wid = await self._due_window(redis, email)
        assert await sns._claim_window(redis, OWNER, EVENT, wid, now=_window_end() + 1)
        assert await redis.get(_k(sns._LOCK_KEY, wid)) is None

    @pytest.mark.asyncio
    async def test_sweep_skips_a_window_being_claimed(self, redis, deliverable, email) -> None:
        wid = await self._due_window(redis, email)
        await redis.set(_k(sns._LOCK_KEY, wid), "claiming-replica")
        much_later = _window_end() + sns._STATE_RETENTION_SECONDS + 60

        await sns._expire_stale_windows(redis, now=much_later)

        assert await redis.zscore(sns._DUE_KEY, _m(wid)) is not None
        assert await redis.lrange(_k(sns._BUFFER_KEY, wid), 0, -1)

    @pytest.mark.asyncio
    async def test_sweep_is_not_blocked_by_unrelated_due_writes(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        wid = await self._due_window(redis, email)
        much_later = _window_end() + sns._STATE_RETENTION_SECONDS + 60

        async def _other_account_opens_a_window() -> None:
            await redis.zadd(sns._DUE_KEY, {sns._member("someone-else", EVENT, "f" * 32): 1e12})

        _conflict_on_read(redis, monkeypatch, _other_account_opens_a_window, method="get")
        await sns._expire_stale_windows(redis, now=much_later)

        assert await redis.zscore(sns._DUE_KEY, _m(wid)) is None
        assert await redis.get(_k(sns._LOCK_KEY, wid)) is None


class TestStaleBacklog:
    @pytest.mark.asyncio
    async def test_backlog_beyond_one_batch_is_reported_not_delivered(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        users = [f"user-{i:03d}" for i in range(sns._FLUSH_BATCH + 3)]
        for user_id in users:
            await _notify(email, user_id=user_id, key_name="first")
            await _notify(email, user_id=user_id, key_name="second")
        email.send_security_notification.reset_mock()
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        much_later = _window_end() + sns._STATE_RETENTION_SECONDS + 60

        def _expired() -> int:
            return sum(
                1
                for c in logger.warning.call_args_list
                if c.args[0] == "security_notification_window_expired"
            )

        await sns.flush_due_security_notifications(
            now_score=much_later, session_factory=_factory(), email_service=email
        )
        assert _expired() == sns._FLUSH_BATCH
        assert await redis.zcard(sns._DUE_KEY) == 3
        email.send_security_notification.assert_not_awaited()

        await sns.flush_due_security_notifications(
            now_score=much_later, session_factory=_factory(), email_service=email
        )
        assert _expired() == len(users)
        assert await redis.zcard(sns._DUE_KEY) == 0
        email.send_security_notification.assert_not_awaited()


# ---------------------------------------------------------------------------
# Copilot round 6: response-read failures are uncertain; erasure scans once
# ---------------------------------------------------------------------------


def _requests_connection_error(context: Exception) -> Exception:
    """requests.ConnectionError raised while handling a urllib3 error."""
    import requests

    outer = requests.exceptions.ConnectionError(context)
    outer.__context__ = context
    return outer


def _uncertain_errors() -> list[Exception]:
    import httpx
    import requests
    import urllib3

    return [
        requests.exceptions.ChunkedEncodingError("response ended early"),
        _requests_connection_error(
            urllib3.exceptions.ProtocolError("Connection aborted.", ConnectionResetError())
        ),
        httpx.ReadError("read failed"),
        httpx.RemoteProtocolError("peer closed connection"),
    ]


def _definite_errors() -> list[Exception]:
    import httpx
    import urllib3

    return [
        _requests_connection_error(
            urllib3.exceptions.NewConnectionError(None, "connection refused")
        ),
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timed out"),
    ]


class TestProviderReadErrors:
    def _send_with(self, monkeypatch, inner: Exception):
        import services.email_providers.resend as resend_module
        from services.email_providers.resend import ResendEmailService

        monkeypatch.setattr(resend_module.resend.Emails, "send", _sdk_http_error(inner))
        service = ResendEmailService(api_key="re_test", from_email="noreply@example.test")
        return _send_notice(service)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", range(4))
    async def test_response_read_failures_are_uncertain(self, monkeypatch, index) -> None:
        with pytest.raises(TimeoutError):
            await self._send_with(monkeypatch, _uncertain_errors()[index])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", range(3))
    async def test_connection_failures_stay_definite(self, monkeypatch, index) -> None:
        assert await self._send_with(monkeypatch, _definite_errors()[index]) is False


class TestPurgeScansOnce:
    @pytest.mark.asyncio
    async def test_erasure_scans_the_keyspace_once(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        await _notify(email, SecurityEvent.PASSWORD_CHANGED)
        await redis.set(_k(sns._LOCK_KEY, "b" * 32), "held")
        real_scan = redis.scan_iter
        calls: list[object] = []

        def _counting_scan(*args, **kwargs):
            calls.append(kwargs.get("match"))
            return real_scan(*args, **kwargs)

        monkeypatch.setattr(redis, "scan_iter", _counting_scan)
        await sns.purge_user_notification_state(OWNER)

        assert len(calls) == 1
        assert await _window_keys(redis) == []


# ---------------------------------------------------------------------------
# Review round 7: immediate budget, lazy due entries, first-notice retry,
# email-change notice, version numbers, transport timeout
# ---------------------------------------------------------------------------


@pytest.fixture
def production_budget(monkeypatch) -> int:
    monkeypatch.setattr(sns, "_IMMEDIATE_PER_WINDOW", IMMEDIATE_BUDGET)
    return IMMEDIATE_BUDGET


class TestImmediateBudget:
    def test_more_than_one_notice_is_sent_at_once(self) -> None:
        # A second approval or key shortly after the owner's own must not
        # wait for the window to close.
        assert IMMEDIATE_BUDGET >= 2

    @pytest.mark.asyncio
    async def test_early_occurrences_are_each_sent_at_once_then_buffered(
        self, redis, deliverable, email, production_budget
    ) -> None:
        for index in range(production_budget):
            await _notify(email, key_name=f"key-{index}")
        assert email.send_security_notification.await_count == production_budget
        for index, call in enumerate(email.send_security_notification.await_args_list):
            assert call.kwargs["digest"] is False
            assert [o.key_name for o in call.kwargs["occurrences"]] == [f"key-{index}"]
        wid = await _wid(redis)
        assert wid is not None
        assert await redis.zcard(sns._DUE_KEY) == 0

        await _notify(email, key_name="over-budget")

        assert email.send_security_notification.await_count == production_budget
        assert await redis.llen(_k(sns._BUFFER_KEY, wid)) == 1
        assert await redis.zscore(sns._DUE_KEY, _m(wid)) is not None
        assert await _wid(redis) == wid  # one window throughout

    @pytest.mark.asyncio
    async def test_digest_lists_only_what_was_not_sent_at_once(
        self, redis, deliverable, email, production_budget
    ) -> None:
        for index in range(production_budget + 2):
            await _notify(email, key_name=f"key-{index}")
        email.send_security_notification.reset_mock()

        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is True
        assert [o.key_name for o in kwargs["occurrences"]] == [
            f"key-{production_budget}",
            f"key-{production_budget + 1}",
        ]

    @pytest.mark.asyncio
    async def test_a_failed_early_notice_is_retried_and_the_window_stays(
        self, redis, deliverable, email, production_budget
    ) -> None:
        await _notify(email, key_name="first")
        wid = await _wid(redis)
        email.send_security_notification = AsyncMock(return_value=False)
        await _notify(email, key_name="second")

        # The first notice did go out, so its window stays open.
        assert await _wid(redis) == wid
        assert await redis.zcard(sns._DUE_KEY) == 1  # the retry

        email.send_security_notification = AsyncMock(return_value=True)
        sent = await sns.flush_due_security_notifications(
            now_score=sns._now_score() + sns._DIGEST_RETRY_DELAY_SECONDS + 1,
            session_factory=_factory(),
            email_service=email,
        )
        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["digest"] is False
        assert [o.key_name for o in kwargs["occurrences"]] == ["second"]
        assert await _wid(redis) == wid

    def test_window_is_at_most_an_hour(self) -> None:
        from annotated_types import Le

        from config.settings import Settings

        field = Settings.model_fields["security_notification_window_seconds"]
        (upper,) = [m.le for m in field.metadata if isinstance(m, Le)]
        assert upper == 60 * 60


class TestWindowWithoutRepeats:
    @pytest.mark.asyncio
    async def test_flush_has_nothing_to_claim(self, redis, deliverable, email, monkeypatch) -> None:
        await _notify(email, key_name="only")
        email.send_security_notification.reset_mock()
        claim = AsyncMock()
        monkeypatch.setattr(sns, "_claim_window", claim)

        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )

        assert sent == 0
        claim.assert_not_awaited()
        email.send_security_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pointer_past_its_deadline_is_ignored(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        old = await _wid(redis)
        later = _window_end() + 5
        monkeypatch.setattr(sns, "_now_score", lambda: later)

        await _notify(email, key_name="next-window")

        assert email.send_security_notification.await_count == 2
        assert await _wid(redis) != old
        assert await redis.zcard(sns._DUE_KEY) == 0


class TestFirstNoticeRetry:
    async def _fail_first(self, redis, email) -> None:
        email.send_security_notification = AsyncMock(return_value=False)
        await _notify(email, key_name="first")
        assert await redis.zcard(sns._DUE_KEY) == 1

    def _later(self, attempts: int = 1) -> float:
        return sns._now_score() + sns._DIGEST_RETRY_DELAY_SECONDS * attempts + 1

    @pytest.mark.asyncio
    async def test_retry_is_a_notice_not_a_follow_up(self, redis, deliverable, email) -> None:
        await self._fail_first(redis, email)
        email.send_security_notification = AsyncMock(return_value=True)

        sent = await sns.flush_due_security_notifications(
            now_score=self._later(), session_factory=_factory(), email_service=email
        )

        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["to_email"] == ADDRESS
        assert kwargs["digest"] is False
        assert [o.key_name for o in kwargs["occurrences"]] == ["first"]
        assert kwargs["total"] == 1
        assert await _window_keys(redis) == []
        assert await redis.zcard(sns._DUE_KEY) == 0

    @pytest.mark.asyncio
    async def test_not_retried_before_its_time(self, redis, deliverable, email) -> None:
        await self._fail_first(redis, email)
        email.send_security_notification = AsyncMock(return_value=True)

        sent = await sns.flush_due_security_notifications(
            now_score=sns._now_score() + 1, session_factory=_factory(), email_service=email
        )

        assert sent == 0
        email.send_security_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dropped_after_as_many_attempts_as_a_digest(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await self._fail_first(redis, email)  # attempt 1
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        base = sns._now_score()
        for run in range(1, 6):
            await sns.flush_due_security_notifications(
                now_score=base + 3600 * run, session_factory=_factory(), email_service=email
            )

        assert email.send_security_notification.await_count == sns._DIGEST_MAX_ATTEMPTS
        dropped = [
            c
            for c in logger.error.call_args_list
            if c.args[0] == "security_notification_digest_dropped"
        ]
        assert len(dropped) == 1
        assert await _window_keys(redis) == []
        assert await redis.zcard(sns._DUE_KEY) == 0

    @pytest.mark.asyncio
    async def test_retry_rechecks_the_recipient(self, redis, deliverable, email) -> None:
        await self._fail_first(redis, email)
        email.send_security_notification = AsyncMock(return_value=True)
        deliverable.return_value = None  # the account became undeliverable

        sent = await sns.flush_due_security_notifications(
            now_score=self._later(), session_factory=_factory(), email_service=email
        )

        assert sent == 0
        email.send_security_notification.assert_not_awaited()
        assert await _window_keys(redis) == []

    @pytest.mark.asyncio
    async def test_redis_down_too_is_only_logged(self, monkeypatch, deliverable) -> None:
        broken = MagicMock()
        broken.pipeline = MagicMock(side_effect=ConnectionError("redis down"))
        monkeypatch.setattr(sns, "get_redis_client", lambda: broken)
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        email = AsyncMock()
        email.send_security_notification = AsyncMock(return_value=False)

        await _notify(email)  # does not raise

        assert "security_notification_retry_unavailable" in [
            c.args[0] for c in logger.error.call_args_list
        ]


OLD_ADDRESS = "owner-old@example.test"


class TestEmailChanged:
    @pytest.mark.asyncio
    async def test_previous_address_is_told_at_once(self, redis, deliverable, email) -> None:
        await sns.notify_email_changed(
            OWNER,
            OLD_ADDRESS,
            ip="192.0.2.10",
            user_agent="pytest-agent/1.0",
            auth_provider="google",
            email_service=email,
        )

        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["to_email"] == OLD_ADDRESS
        assert kwargs["event"] == "email_changed"
        assert kwargs["digest"] is False
        (occurrence,) = kwargs["occurrences"]
        assert occurrence.sign_in_method == "Google sign-in"
        deliverable.assert_not_awaited()  # never the address on the account now
        # Not coalesced: no window, and a second change is sent at once too.
        assert await redis.keys("security_notify:*") == []
        await sns.notify_email_changed(
            OWNER, OLD_ADDRESS, ip=None, user_agent=None, email_service=email
        )
        assert email.send_security_notification.await_count == 2

    @pytest.mark.asyncio
    async def test_pending_digest_goes_to_the_previous_address(
        self, redis, deliverable, email
    ) -> None:
        # Changes made before the email change are still buffered ...
        await _notify(email, key_name="first")
        await _notify(email, key_name="buffered")
        wid = await _wid(redis)
        email.send_security_notification.reset_mock()
        # ... when the account's address becomes someone else's.
        deliverable.return_value = "attacker@example.test"

        await sns.notify_email_changed(
            OWNER, OLD_ADDRESS, ip=None, user_agent=None, email_service=email
        )
        email.send_security_notification.reset_mock()
        # Due now: the next run sends it without waiting for the window's end.
        sent = await sns.flush_due_security_notifications(
            now_score=sns._now_score() + 1, session_factory=_factory(), email_service=email
        )

        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["to_email"] == OLD_ADDRESS
        assert kwargs["digest"] is True
        assert [o.key_name for o in kwargs["occurrences"]] == ["buffered"]
        assert await redis.exists(_k(sns._META_KEY, wid)) == 0

    @pytest.mark.asyncio
    async def test_other_users_windows_are_not_pinned(self, redis, deliverable, email) -> None:
        await _notify(email, user_id="neighbour", key_name="first")
        await _notify(email, user_id="neighbour", key_name="buffered")

        assert await sns._pin_pending_windows(OWNER, OLD_ADDRESS) == 0

    @pytest.mark.asyncio
    async def test_an_earlier_pin_is_kept(self, redis, deliverable, email) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="buffered")
        wid = await _wid(redis)

        assert await sns._pin_pending_windows(OWNER, OLD_ADDRESS) == 1
        assert await sns._pin_pending_windows(OWNER, "second-change@example.test") == 0

        meta = sns._parse_meta(await redis.get(_k(sns._META_KEY, wid)))
        assert meta == {"recipient": OLD_ADDRESS}

    @pytest.mark.asyncio
    async def test_a_window_being_claimed_is_skipped(self, redis, deliverable, email) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="buffered")
        wid = await _wid(redis)
        await redis.set(_k(sns._LOCK_KEY, wid), "claiming-replica")

        assert await sns._pin_pending_windows(OWNER, OLD_ADDRESS) == 0
        assert await redis.exists(_k(sns._META_KEY, wid)) == 0

    @pytest.mark.asyncio
    async def test_failed_notice_is_retried_to_the_previous_address(
        self, redis, deliverable, email
    ) -> None:
        email.send_security_notification = AsyncMock(return_value=False)
        await sns.notify_email_changed(
            OWNER, OLD_ADDRESS, ip=None, user_agent=None, email_service=email
        )
        email.send_security_notification = AsyncMock(return_value=True)
        deliverable.return_value = "attacker@example.test"

        sent = await sns.flush_due_security_notifications(
            now_score=sns._now_score() + sns._DIGEST_RETRY_DELAY_SECONDS + 1,
            session_factory=_factory(),
            email_service=email,
        )

        assert sent == 1
        kwargs = email.send_security_notification.await_args.kwargs
        assert kwargs["to_email"] == OLD_ADDRESS
        assert kwargs["event"] == "email_changed"
        assert kwargs["digest"] is False
        assert await _window_keys(redis) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("old", ["admin@local", "Admin@LOCAL", "no-at-sign"])
    async def test_undeliverable_previous_address_gets_nothing(self, redis, email, old) -> None:
        await sns.notify_email_changed(OWNER, old, ip=None, user_agent=None, email_service=email)
        email.send_security_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_raises(self, monkeypatch, email) -> None:
        monkeypatch.setattr(sns, "get_redis_client", MagicMock(side_effect=OSError("down")))
        email.send_security_notification = AsyncMock(side_effect=RuntimeError("provider down"))
        await sns.notify_email_changed(
            OWNER, OLD_ADDRESS, ip=None, user_agent=None, email_service=email
        )

    def test_spawn_without_a_running_loop_is_only_logged(self, monkeypatch) -> None:
        logger = MagicMock()
        monkeypatch.setattr(sns, "logger", logger)
        sns.spawn_email_change_notification(user_id=OWNER, old_email=OLD_ADDRESS)
        assert logger.error.call_args.args[0] == "security_notification_schedule_failed"

    @pytest.mark.asyncio
    async def test_spawn_runs_the_notice(self, monkeypatch) -> None:
        notify = AsyncMock()
        monkeypatch.setattr(sns, "notify_email_changed", notify)
        sns.spawn_email_change_notification(
            user_id=OWNER, old_email=OLD_ADDRESS, ip="192.0.2.1", auth_provider="github"
        )
        await asyncio.gather(*sns._spawned)
        assert notify.await_args.args == (OWNER, OLD_ADDRESS)
        assert notify.await_args.kwargs["auth_provider"] == "github"

    def test_render(self) -> None:
        subject, text = render_security_notification(
            SecurityEvent.EMAIL_CHANGED,
            [SecurityOccurrence(occurred_at="t", sign_in_method="Google sign-in")],
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        assert subject == "The email address of your Kagura account was changed"
        assert "Via:         Google sign-in" in text
        assert "Wasn't you?" in text

    @pytest.mark.asyncio
    async def test_erasure_removes_a_pinned_address(self, redis, deliverable, email) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="buffered")
        await sns._pin_pending_windows(OWNER, OLD_ADDRESS)

        await sns.purge_user_notification_state(OWNER)

        assert await redis.keys("security_notify:*") == []


CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)


class TestVersionNumbersAreKept:
    def test_browser_user_agent_is_unchanged(self) -> None:
        assert sanitize_display_text(CHROME_UA, 200) == CHROME_UA

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("see 93.184.216.34/login", "see 93[.]184[.]216[.]34/login"),
            ("http://93.184.216.34", "http[:]//93[.]184[.]216[.]34"),
            ("Go/93.184.216.34/login", "Go/93[.]184[.]216[.]34/login"),
            ("Go/93.184.216.34:8080", "Go/93[.]184[.]216[.]34:8080"),
            ("agent 1.2.3.4", "agent 1[.]2[.]3[.]4"),
        ],
    )
    def test_addresses_are_still_defanged(self, raw, expected) -> None:
        assert sanitize_display_text(raw, 200) == expected

    def test_still_idempotent(self) -> None:
        once = sanitize_display_text(CHROME_UA + " 93.184.216.34/x", 200)
        assert sanitize_display_text(once, 200) == once


class TestTransportTimeout:
    def test_provider_reports_before_the_callers_backstop(self) -> None:
        import services.email_providers.resend as resend_module

        # Connect and read are bounded separately: both together must finish
        # before wait_for gives up and has to call the outcome uncertain.
        assert 2 * resend_module._HTTP_TIMEOUT_SECONDS < sns._EMAIL_TIMEOUT_SECONDS

    def test_constructor_bounds_the_sdk_client(self, monkeypatch) -> None:
        import services.email_providers.resend as resend_module
        from services.email_providers.resend import ResendEmailService

        monkeypatch.setattr(resend_module.resend, "default_http_client", object())
        ResendEmailService(api_key="re_test", from_email="noreply@example.test")

        client = resend_module.resend.default_http_client
        assert client._timeout == resend_module._HTTP_TIMEOUT_SECONDS

    def test_classification_survives_a_missing_transport_library(self, monkeypatch) -> None:
        import sys

        import httpx

        import services.email_providers.resend as resend_module

        resend_module._uncertain_error_types.cache_clear()
        monkeypatch.setitem(sys.modules, "requests", None)
        monkeypatch.setitem(sys.modules, "urllib3", None)
        try:
            assert resend_module._may_have_been_delivered(httpx.ReadTimeout("t")) is True
            assert resend_module._may_have_been_delivered(RuntimeError("x")) is False
        finally:
            resend_module._uncertain_error_types.cache_clear()


class TestPasswordResetNamesLinks:
    """#1803: a reset signs the account out everywhere, but its identity links
    stay — a linked account keeps owning its private contexts. The reset
    notice says so when the account has links."""

    @pytest.mark.asyncio
    async def test_the_reset_notice_counts_the_linked_accounts(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        count = AsyncMock(return_value=2)
        monkeypatch.setattr(sns, "_linked_account_count", count)

        await _notify(email, SecurityEvent.PASSWORD_RESET)

        assert count.await_args.args[1] == OWNER
        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.linked_accounts == 2

    @pytest.mark.asyncio
    async def test_no_links_leaves_it_out(self, redis, deliverable, email, monkeypatch) -> None:
        monkeypatch.setattr(sns, "_linked_account_count", AsyncMock(return_value=0))

        await _notify(email, SecurityEvent.PASSWORD_RESET)

        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.linked_accounts is None

    @pytest.mark.asyncio
    async def test_other_notices_do_not_look_links_up(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        count = AsyncMock(return_value=2)
        monkeypatch.setattr(sns, "_linked_account_count", count)

        await _notify(email, SecurityEvent.PASSWORD_CHANGED)

        count.assert_not_awaited()
        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.linked_accounts is None

    def _render(self, *occurrences: SecurityOccurrence) -> str:
        _, text = render_security_notification(
            SecurityEvent.PASSWORD_RESET,
            list(occurrences),
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        return text

    def test_the_email_names_the_links_and_where_to_review_them(self) -> None:
        text = self._render(
            SecurityOccurrence(occurred_at="2026-10-02T10:00:00 UTC", linked_accounts=2)
        )

        assert "linked to 2 other accounts" in text
        assert "does not remove" in text
        assert "Linked accounts" in text

    def test_one_link_reads_in_the_singular(self) -> None:
        text = self._render(
            SecurityOccurrence(occurred_at="2026-10-02T10:00:00 UTC", linked_accounts=1)
        )

        assert "linked to 1 other account." in text

    def test_without_links_the_email_says_nothing_about_them(self) -> None:
        text = self._render(SecurityOccurrence(occurred_at="2026-10-02T10:00:00 UTC"))

        assert "linked to" not in text

    def test_the_count_survives_the_buffer_and_nothing_else_does(self) -> None:
        kept = SecurityOccurrence.from_json(
            SecurityOccurrence(occurred_at="2026-10-02T10:00:00 UTC", linked_accounts=3).to_json()
        )
        assert kept.linked_accounts == 3
        for bad in ('"3"', "true", "-1", "0", "4", "999", "1.5"):
            occurrence = SecurityOccurrence.from_json(
                f'{{"occurred_at": "2026-10-02T10:00:00 UTC", "linked_accounts": {bad}}}'
            )
            assert occurrence.linked_accounts is None
