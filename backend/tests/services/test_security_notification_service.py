"""Security-change notification emails (Issue #1752).

Covers the coalescing window (first occurrence sent at once, repeats buffered,
one digest when the window closes, the ``ZREM`` claim, Redis down → send at
once), failure isolation, the deliverable-address rule against real Postgres,
sanitizing of untrusted names / user agents, and the rendered body (never a
secret, token or action link).
"""

from __future__ import annotations

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
        assert await redis.zscore(sns._DUE_KEY, f"{OWNER}|api_key_created") is not None

    @pytest.mark.asyncio
    async def test_repeats_in_the_window_are_buffered(self, redis, deliverable, email) -> None:
        await _notify(email, key_name="a")
        await _notify(email, key_name="b")
        await _notify(email, key_name="c")

        assert email.send_security_notification.await_count == 1
        assert await redis.llen(sns._buffer_key(OWNER, "api_key_created")) == 2

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
        assert await redis.exists(sns._buffer_key(OWNER, "api_key_created")) == 0

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
    async def test_zrem_claim_sends_each_digest_once(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        await _notify(email, key_name="second")
        email.send_security_notification.reset_mock()

        # Another process claims the window between our ZRANGEBYSCORE and ZREM.
        real_zrem = redis.zrem

        async def _lost_race(key, *members):
            await real_zrem(key, *members)  # the other process
            return await real_zrem(key, *members)  # ours removes nothing

        monkeypatch.setattr(redis, "zrem", _lost_race)
        sent = await sns.flush_due_security_notifications(
            now_score=_window_end() + 1, session_factory=_factory(), email_service=email
        )
        assert sent == 0
        email.send_security_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_occurrence_after_a_concurrent_claim_is_sent_at_once(
        self, redis, deliverable, email, monkeypatch
    ) -> None:
        await _notify(email, key_name="first")
        email.send_security_notification.reset_mock()

        # The window is claimed right after our ZADD saw it open.
        real_zadd = redis.zadd

        async def _zadd_then_claim(*args, **kwargs):
            result = await real_zadd(*args, **kwargs)
            await redis.zrem(sns._DUE_KEY, f"{OWNER}|api_key_created")
            return result

        monkeypatch.setattr(redis, "zadd", _zadd_then_claim)
        await _notify(email, key_name="late")

        email.send_security_notification.assert_awaited_once()
        (occurrence,) = email.send_security_notification.await_args.kwargs["occurrences"]
        assert occurrence.key_name == "late"
        assert await redis.llen(sns._buffer_key(OWNER, "api_key_created")) == 0

    @pytest.mark.asyncio
    async def test_redis_down_sends_at_once(self, monkeypatch, deliverable, email) -> None:
        broken = MagicMock()
        broken.zadd = AsyncMock(side_effect=ConnectionError("redis down"))
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

    async def test_oauth_only_user_without_verified_at(
        self, db_session: AsyncSession, made_users
    ) -> None:
        user = await _make_user(db_session, made_users, verified=False, provider=True)
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
        assert "2 more times in the 10 minutes" in text
        assert text.count("Your password was changed.") == 2

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
