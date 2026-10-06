"""An identity whose provider link was removed cannot sign in to the account it created.

An account created by a Google or GitHub sign-in has ``users.user_id`` equal to
the provider's subject. ``AccountLinkingService.unlink`` deletes the
``user_oauth_providers`` row but the id stays, so three places used to accept
the identity again by that ``users`` row alone: the new-user path's
``IntegrityError`` retry in ``RoleManager.ensure_user``, the ``users.user_id``
fallback in ``_owning_user``, and the sub fallback in ``_session_owner``. All
three now refuse: the callback redirects to ``/login?error=provider_unlinked``
and opens no session.

Driven through the whole callback against the real PostgreSQL test DB
(``conftest.async_engine`` skips when unreachable), with the IdP exchange
stubbed the way ``test_linked_provider_session.py`` does. Every test seeds
uuid-suffixed identifiers; the ``db_session`` fixture is session-scoped.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import auth as auth_routes
from auth.roles import Role, RoleManager
from models.auth import User, UserOAuthProvider
from services.account_linking_service import AccountLinkingService
from utils.exceptions import UnlinkedProviderSignInError

PROVIDERS = ["google", "github"]


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


class FakeRequest:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers = {"user-agent": "pytest"}
        self.client = MagicMock(host="203.0.113.7")


def _fresh_sessions(engine):
    """``get_db`` replacement yielding a NEW session per call.

    ``ensure_user`` commits and rolls back inside its own ``get_db`` session;
    sharing the test's outer session would poison it on the rolled-back insert.
    """
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _scope():
        async with maker() as s:
            yield s

    async def _fake():
        async with _scope() as s:
            yield s

    return _fake


async def _oauth_account_with_password(
    db: AsyncSession, *, provider: str, suffix: str
) -> tuple[User, str]:
    """An account created by ``provider`` (link row sub == user_id) that later
    set a password: the state in which its original provider may be unlinked."""
    sub = f"{provider}-sub-{suffix}"
    user = User(
        email=f"unlinked-{suffix}@example.com",
        user_id=sub,
        name="Created By Provider",
        role="admin",
        auth_method="oauth",
        auth_provider=provider,
        password_hash="x",
        last_login_at=datetime(2020, 1, 1),
    )
    db.add(user)
    db.add(UserOAuthProvider(user_id=sub, provider=provider, oauth_sub=sub))
    await db.commit()
    return user, sub


async def _unlinked_account(db: AsyncSession, *, provider: str) -> tuple[User, str]:
    suffix = uuid4().hex[:8]
    user, sub = await _oauth_account_with_password(db, provider=provider, suffix=suffix)
    await AccountLinkingService(db).unlink(user_id=sub, provider=provider)
    link = (
        await db.execute(select(UserOAuthProvider).filter_by(provider=provider, oauth_sub=sub))
    ).scalar_one_or_none()
    assert link is None
    return user, sub


async def _reload(db: AsyncSession, user_id: str) -> User:
    return (
        await db.execute(
            select(User).filter_by(user_id=user_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar() or 0


# --- RoleManager.ensure_user --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_ensure_user_refuses_the_unlinked_identity(db_session: AsyncSession, provider):
    """No role comes back, no duplicate account and no provider row are created,
    and the account's ``last_login_at`` is untouched."""
    user, sub = await _unlinked_account(db_session, provider=provider)
    before = await _user_count(db_session)

    with patch("db.base.get_db", new=_fresh_sessions(db_session.bind)):
        with pytest.raises(UnlinkedProviderSignInError):
            await RoleManager(use_postgres=True).ensure_user(
                email=user.email,
                user_id=sub,
                name="Created By Provider",
                auth_provider=provider,
                email_verified=True,
            )

    assert await _user_count(db_session) == before
    link = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider=provider, oauth_sub=sub)
        )
    ).scalar_one_or_none()
    assert link is None
    assert (await _reload(db_session, sub)).last_login_at == datetime(2020, 1, 1)


@pytest.mark.asyncio
async def test_ensure_user_still_signs_in_through_a_link_row(db_session: AsyncSession):
    """The genuine case the retry exists for: the identity's link row is there
    and points at the account found by ``user_id`` — a concurrent first sign-in
    committed both together. Modelled by a link row the first lookup misses."""
    suffix = uuid4().hex[:8]
    user, sub = await _oauth_account_with_password(db_session, provider="google", suffix=suffix)
    real_execute = AsyncSession.execute
    calls = {"n": 0}

    async def _miss_first_link_lookup(self, statement, *args, **kwargs):
        # The very first SELECT of the sign-in is the link lookup; make it miss
        # the way a not-yet-committed concurrent insert would.
        calls["n"] += 1
        result = await real_execute(self, statement, *args, **kwargs)
        if calls["n"] == 1:
            return MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        return result

    with (
        patch("db.base.get_db", new=_fresh_sessions(db_session.bind)),
        patch.object(AsyncSession, "execute", _miss_first_link_lookup),
    ):
        role = await RoleManager(use_postgres=True).ensure_user(
            email=user.email,
            user_id=sub,
            name="Created By Provider",
            auth_provider="google",
            email_verified=True,
        )

    assert role == Role.ADMIN
    assert (await _reload(db_session, sub)).last_login_at > datetime(2020, 1, 1)


# --- _owning_user / _session_owner / _identity_exists -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_owning_user_does_not_fall_back_to_the_users_row(db_session: AsyncSession, provider):
    _, sub = await _unlinked_account(db_session, provider=provider)

    assert await auth_routes._owning_user(db_session, provider, sub) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_session_owner_fails_closed(db_session: AsyncSession, provider, monkeypatch):
    user, sub = await _unlinked_account(db_session, provider=provider)
    monkeypatch.setattr(auth_routes, "get_db", _fresh_sessions(db_session.bind))

    with pytest.raises(UnlinkedProviderSignInError):
        await auth_routes._session_owner(provider, sub, user.email)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_identity_exists_still_counts_the_refused_identity(
    db_session: AsyncSession, provider, monkeypatch
):
    """The terms gate asks whether ``ensure_user`` would create an account.
    It would not — it refuses — so the gate must not mask that with
    ``terms_required`` (and no beta invite is spent on it)."""
    user, sub = await _unlinked_account(db_session, provider=provider)
    monkeypatch.setattr(auth_routes, "get_db", _fresh_sessions(db_session.bind))

    assert await auth_routes._identity_exists(provider, sub, user.email) is True


# --- the whole callback -------------------------------------------------------


@pytest.fixture
def manager(monkeypatch) -> MagicMock:
    m = MagicMock()
    m._redis = FakeRedis()
    m._redis.store["oauth2_state:st1"] = "pending"
    m.delete_user_sessions.return_value = 0
    m.create_session.return_value = "sess-1"
    monkeypatch.setattr(auth_routes, "_session_manager", m)
    return m


@pytest.fixture
def real_resolution(monkeypatch, db_session: AsyncSession) -> None:
    """The real role manager and owner lookups on fresh DB sessions; the
    gates and the IdP stubbed."""
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_note_provider_sign_in", AsyncMock())
    monkeypatch.setattr(auth_routes, "get_role_manager", lambda: RoleManager(use_postgres=True))
    monkeypatch.setattr(auth_routes, "get_db", _fresh_sessions(db_session.bind))
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")


def _stub_idp(monkeypatch, provider: str, *, sub: str, email: str) -> None:
    info = {
        "sub": sub,
        "email": email,
        "email_verified": True,
        "login": "octo",
        "name": "Created By Provider",
        "picture": None,
    }
    if provider == "google":
        m = MagicMock()
        m.get_user_info_web.return_value = info
        monkeypatch.setattr(auth_routes, "_oauth2_manager", m)
    else:
        monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
        monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="at"))
        monkeypatch.setattr(auth_routes, "_github_get_user_info", AsyncMock(return_value=info))


async def _callback(provider: str):
    handler = auth_routes.google_callback if provider == "google" else auth_routes.github_callback
    return await handler(FakeRequest(), code="c", state="st1", error=None, error_description=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_callback_refuses_the_unlinked_identity_with_no_session(
    db_session: AsyncSession, manager, real_resolution, monkeypatch, provider
):
    user, sub = await _unlinked_account(db_session, provider=provider)
    _stub_idp(monkeypatch, provider, sub=sub, email=user.email)

    with patch("db.base.get_db", new=_fresh_sessions(db_session.bind)):
        response = await _callback(provider)

    assert response.status_code == 303
    assert f"/login?error=provider_unlinked&provider={provider}" in response.headers["location"]
    assert "set-cookie" not in response.headers
    manager.create_session.assert_not_called()
    manager.add_account.assert_not_called()
    manager.delete_user_sessions.assert_not_called()
    assert (await _reload(db_session, sub)).last_login_at == datetime(2020, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_callback_still_signs_in_a_linked_identity(
    db_session: AsyncSession, manager, real_resolution, monkeypatch, provider
):
    """The same account with its provider row in place signs in as before."""
    suffix = uuid4().hex[:8]
    user, sub = await _oauth_account_with_password(db_session, provider=provider, suffix=suffix)
    _stub_idp(monkeypatch, provider, sub=sub, email=user.email)

    with patch("db.base.get_db", new=_fresh_sessions(db_session.bind)):
        response = await _callback(provider)

    assert response.status_code == 303
    assert "kagura_session=sess-1" in response.headers["set-cookie"]
    assert manager.create_session.call_args.args[0]["user_id"] == sub


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_callback_fails_closed_when_the_owner_lookup_finds_nothing(
    manager, monkeypatch, provider
):
    """``_session_owner`` after a successful ``ensure_user``: no owner row for a
    Google or GitHub identity opens no session (it used to keep the sub)."""
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_terms_refusal", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(auth_routes, "_note_provider_sign_in", AsyncMock())
    monkeypatch.setattr(
        auth_routes,
        "get_role_manager",
        lambda: SimpleNamespace(ensure_user=AsyncMock(return_value=MagicMock(value="user"))),
    )

    async def _fake_db():
        yield MagicMock()

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(auth_routes, "_owning_user", AsyncMock(return_value=None))
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
    _stub_idp(monkeypatch, provider, sub="sub-1", email="someone@example.test")

    response = await _callback(provider)

    assert f"/login?error=provider_unlinked&provider={provider}" in response.headers["location"]
    manager.create_session.assert_not_called()
    manager.delete_user_sessions.assert_not_called()
