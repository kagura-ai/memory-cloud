"""Email sign-up accounts meet the real OAuth callbacks, on Postgres (#2786 with #1734).

Through ``github_callback`` / ``google_callback`` (the provider exchange is
stubbed, the gate, ``ensure_user`` and the database are real), with an
account created by the email sign-up flow:

- a GitHub sign-in from the browser that started it answers exactly like
  ``email_in_use``, offers the confirmation by email, and completing the
  mailed link with that browser's nonce connects GitHub to the account: one
  ``users`` row, one GitHub provider row;
- an account with two-step verification on gets the plain ``email_in_use``
  answer and no offer;
- "This wasn't me" on the mailed link sets ``provider_auto_link_blocked_at``;
  a later Google sign-in with the same (Gmail) address is no longer linked
  automatically, while an account that was never blocked still is.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_routes
from auth import oauth_browser
from auth.roles import RoleManager
from models.auth import User, UserOAuthProvider
from services import provider_link_confirmation_service as link_svc
from tests.integration.test_email_signup_db import (  # noqa: F401
    _email_account,
    _gate,
    _users_for,
    env,
    maker,
    someone,
)
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


class _FakeRequest:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers = {"user-agent": "pytest"}
        self.client = MagicMock(host="203.0.113.7")


@pytest_asyncio.fixture(loop_scope="session")
async def gate_off(env, db_session) -> None:  # noqa: F811
    await _gate(db_session, enabled=False)


@pytest.fixture
def callbacks(monkeypatch, maker):  # noqa: F811
    """The callbacks with the provider exchange stubbed and the DB real."""
    manager = MagicMock()
    manager._redis = _FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_terms_refusal", AsyncMock(return_value=None))
    role_manager = RoleManager(use_postgres=True)
    monkeypatch.setattr(auth_routes, "get_role_manager", lambda: role_manager)
    offers: list[dict] = []
    monkeypatch.setattr(auth_routes, "spawn_link_offer", lambda **kw: offers.append(kw))

    async def _fresh_get_db():
        async with maker() as s:
            yield s

    patches = [
        patch("db.base.get_db", new=_fresh_get_db),
        patch("services.signup_gate_service.get_db", new=_fresh_get_db),
        patch("services.security_notification_service.spawn_security_notification"),
    ]
    for p in patches:
        p.start()

    def _browser() -> dict[str, str]:
        """``/login`` in this browser: binds state ``st1`` and sets the nonce."""
        manager._redis.store["oauth2_state:st1"] = "pending"
        nonce = auth_routes._bind_oauth_flow(_FakeRequest(), "st1")
        assert nonce is not None
        return {oauth_browser.cookie_name(): nonce}

    async def github(email: str, sub: str):
        cookies = _browser()
        monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="at"))
        monkeypatch.setattr(
            auth_routes,
            "_github_get_user_info",
            AsyncMock(
                return_value={
                    "sub": sub,
                    "email": email,
                    "email_verified": True,
                    "login": "octo-cat",
                    "name": "Octo",
                }
            ),
        )
        response = await auth_routes.github_callback(
            _FakeRequest(cookies), code="c", state="st1", error=None, error_description=None
        )
        return response, cookies[oauth_browser.cookie_name()]

    async def google(email: str, sub: str):
        cookies = _browser()
        idp = MagicMock()
        idp.get_user_info_web.return_value = {
            "sub": sub,
            "email": email,
            "email_verified": True,
            "name": "From Google",
        }
        monkeypatch.setattr(auth_routes, "_oauth2_manager", idp)
        return await auth_routes.google_callback(
            _FakeRequest(cookies), code="c", state="st1", error=None, error_description=None
        )

    yield SimpleNamespace(github=github, google=google, offers=offers)
    for p in reversed(patches):
        p.stop()


def _same_as_email_in_use(response) -> None:
    expected = auth_routes._email_in_use_redirect()
    assert response.status_code == expected.status_code
    assert response.headers["location"] == expected.headers["location"]
    assert "set-cookie" not in response.headers


async def _mailed_token(maker, redis, offer: dict) -> str:  # noqa: F811
    mail = MagicMock()
    mail.send_provider_link_confirmation = AsyncMock(return_value=True)
    await link_svc.process_link_offer(
        **offer, session_factory=maker, email_service=mail, redis=redis
    )
    mail.send_provider_link_confirmation.assert_awaited_once()
    return mail.send_provider_link_confirmation.await_args.kwargs["confirm_url"].split("token=", 1)[
        1
    ]


async def _provider_rows(db: AsyncSession, user_id: str, provider: str) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count())
            .select_from(UserOAuthProvider)
            .where(UserOAuthProvider.user_id == user_id, UserOAuthProvider.provider == provider)
        )
        or 0
    )


def _sub() -> str:
    return str(uuid4().int)[:9]


class TestGitHubOfferThroughTheCallback:
    async def test_same_browser_completion_connects_github(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        callbacks,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        sub = _sub()
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

        response, nonce = await callbacks.github(email, sub)

        _same_as_email_in_use(response)
        (offer,) = callbacks.offers
        assert offer["account_user_id"] == user_id
        assert offer["browser_hash"] == sha256_hex(nonce)
        token = await _mailed_token(maker, redis, offer)
        async with maker() as s:
            confirmed = await link_svc.ProviderLinkConfirmationService(s, redis=redis).complete(
                raw_token=token, browser_nonce=nonce, ip_address="203.0.113.7", user_agent="pytest"
            )
        assert (confirmed.user_id, confirmed.provider) == (user_id, "github")
        assert await _users_for(db_session, email) == 1
        assert await _provider_rows(db_session, user_id, "github") == 1

    async def test_two_step_account_gets_no_offer(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        callbacks,
    ) -> None:
        email, user_id = await _email_account(maker, domain="example.test")
        await db_session.execute(
            update(User).where(User.user_id == user_id).values(totp_enabled=True)
        )
        await db_session.commit()

        response, _ = await callbacks.github(email, _sub())

        _same_as_email_in_use(response)
        assert callbacks.offers == []
        assert await _users_for(db_session, email) == 1
        assert await _provider_rows(db_session, user_id, "github") == 0


class TestThisWasntMeStopsGoogleAutoLink:
    async def test_declined_account_is_not_linked_by_google(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        callbacks,
    ) -> None:
        email, user_id = await _email_account(maker, domain="gmail.com")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await callbacks.github(email, _sub())
        token = await _mailed_token(maker, redis, callbacks.offers.pop())
        async with maker() as s:
            await link_svc.ProviderLinkConfirmationService(s, redis=redis).cancel(
                raw_token=token, ip_address="203.0.113.7", user_agent="pytest"
            )
        db_session.expire_all()
        user = (await db_session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        assert user.provider_auto_link_blocked_at is not None

        response = await callbacks.google(email, f"g-{uuid4().hex[:10]}")

        _same_as_email_in_use(response)
        assert callbacks.offers == []
        assert await _users_for(db_session, email) == 1
        assert await _provider_rows(db_session, user_id, "google") == 0

    async def test_an_account_never_blocked_is_still_linked_by_google(
        self,
        gate_off,
        maker,  # noqa: F811
        db_session,
        callbacks,
        monkeypatch,
    ) -> None:
        # Control: the same callback links a Gmail address automatically.
        email, user_id = await _email_account(maker, domain="gmail.com")
        # Past ensure_user the rest of the sign-in is not this test's subject.
        monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
        monkeypatch.setattr(
            auth_routes, "_session_owner", AsyncMock(side_effect=RuntimeError("stop here"))
        )

        await callbacks.google(email, f"g-{uuid4().hex[:10]}")

        assert await _users_for(db_session, email) == 1
        assert await _provider_rows(db_session, user_id, "google") == 1
