"""Password sign-in by verified email (Issue #1678).

- ``resolve_password_login_user`` against real Postgres: admin login ids are
  unchanged; an email signs in only when verified, with a password, not
  ``@local``; a case-insensitive collision fails closed;
- ``password_login``: email sign-in (with and without MFA), one failure
  counter per resolved account (per normalized identifier when nothing
  resolves), no per-client lockout, and the same bcrypt work — off the event
  loop — on a miss.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_routes
from auth.password import hash_password
from models.auth import User
from utils.datetime import utcnow
from utils.exceptions import InvalidCredentialsError

PASSWORD = "Correct-Horse-1!"

# ---------------------------------------------------------------------------
# Resolver — real Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def people(db_session: AsyncSession) -> AsyncIterator[dict[str, User]]:
    tag = uuid4().hex[:8]
    pw = hash_password(PASSWORD)

    def _user(key: str, **kw) -> User:
        return User(
            user_id=f"u_{key}_{tag}",
            name=key,
            role=kw.pop("role", "user"),
            is_initial_admin=False,
            **kw,
        )

    users = {
        "admin": _user(
            "admin",
            email=f"admin-{tag}@local",
            login_id=f"admin-{tag}",
            password_hash=pw,
            auth_method="password",
            role="admin",
        ),
        "verified": _user(
            "verified",
            email=f"Verified-{tag}@Example.test",
            password_hash=pw,
            auth_method="oauth",
            email_verified_at=utcnow(),
        ),
        "unverified": _user(
            "unverified",
            email=f"unverified-{tag}@example.test",
            password_hash=pw,
            auth_method="oauth",
        ),
        "no_password": _user(
            "nopw",
            email=f"nopw-{tag}@example.test",
            auth_method="oauth",
            email_verified_at=utcnow(),
        ),
        "local": _user(
            "local",
            email=f"local-{tag}@local",
            password_hash=pw,
            auth_method="password",
            email_verified_at=utcnow(),
        ),
        "dup_a": _user(
            "dup_a",
            email=f"dup-{tag}@example.test",
            password_hash=pw,
            auth_method="oauth",
            email_verified_at=utcnow(),
        ),
        "dup_b": _user(
            "dup_b",
            email=f"DUP-{tag}@example.test",
            password_hash=pw,
            auth_method="oauth",
            email_verified_at=utcnow(),
        ),
        "login_id_no_pw": _user(
            "lidnopw",
            email=f"lidnopw-{tag}@example.test",
            login_id=f"nopw-{tag}",
            auth_method="password",
        ),
    }
    db_session.add_all(users.values())
    await db_session.commit()
    user_ids = [u.user_id for u in users.values()]
    users["tag"] = tag  # type: ignore[assignment]
    yield users
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.user_id.in_(user_ids)))
    await db_session.commit()


@pytest.mark.asyncio(loop_scope="session")
class TestResolvePasswordLoginUser:
    async def test_admin_login_id_is_unchanged(self, db_session, people) -> None:
        tag = people["tag"]
        user = await auth_routes.resolve_password_login_user(db_session, f"admin-{tag}")
        assert user is not None and user.user_id == people["admin"].user_id

    async def test_login_id_without_password_does_not_match(self, db_session, people) -> None:
        tag = people["tag"]
        assert await auth_routes.resolve_password_login_user(db_session, f"nopw-{tag}") is None

    async def test_verified_email_matches_case_insensitively(self, db_session, people) -> None:
        tag = people["tag"]
        user = await auth_routes.resolve_password_login_user(
            db_session, f"  verified-{tag}@example.TEST "
        )
        assert user is not None and user.user_id == people["verified"].user_id

    async def test_unverified_email_does_not_match(self, db_session, people) -> None:
        tag = people["tag"]
        assert (
            await auth_routes.resolve_password_login_user(
                db_session, f"unverified-{tag}@example.test"
            )
            is None
        )

    async def test_email_without_password_does_not_match(self, db_session, people) -> None:
        tag = people["tag"]
        assert (
            await auth_routes.resolve_password_login_user(db_session, f"nopw-{tag}@example.test")
            is None
        )

    async def test_local_address_does_not_match(self, db_session, people) -> None:
        tag = people["tag"]
        assert (
            await auth_routes.resolve_password_login_user(db_session, f"local-{tag}@local") is None
        )

    async def test_case_insensitive_collision_fails_closed(self, db_session, people) -> None:
        tag = people["tag"]
        assert (
            await auth_routes.resolve_password_login_user(db_session, f"dup-{tag}@example.test")
            is None
        )

    async def test_non_email_identifier_skips_the_email_lookup(self, db_session, people) -> None:
        assert await auth_routes.resolve_password_login_user(db_session, "nobody") is None


# ---------------------------------------------------------------------------
# Endpoint — mocked session / Redis
# ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def get(self, key: str):
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def setex(self, key: str, ttl: int, value) -> None:
        self.store[key] = value

    def pipeline(self):
        redis = self
        pipe = MagicMock()
        pipe.incr = MagicMock(
            side_effect=lambda key: redis.store.__setitem__(key, int(redis.store.get(key, 0)) + 1)
        )
        pipe.expire = MagicMock()
        pipe.execute = MagicMock()
        return pipe


def _request(ip: str = "198.51.100.9") -> SimpleNamespace:
    return SimpleNamespace(
        cookies={}, headers={"user-agent": "pytest"}, client=SimpleNamespace(host=ip)
    )


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    return manager._redis


@pytest.fixture
def resolver(monkeypatch) -> AsyncMock:
    async def _fake_db():
        yield MagicMock()

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    monkeypatch.setattr(
        auth_routes, "_create_session_and_workspace", AsyncMock(return_value="sess-1")
    )
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_routes, "resolve_password_login_user", mock)
    return mock


def _email_user(**kw) -> SimpleNamespace:
    base = {
        "user_id": "u-email",
        "email": "person@example.test",
        "name": "Person",
        "role": "user",
        "password_hash": hash_password(PASSWORD),
        "totp_enabled": False,
        "totp_secret": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_email_sign_in_creates_a_session(redis, resolver) -> None:
    resolver.return_value = _email_user()
    body = auth_routes.PasswordLoginRequest(login_id="Person@Example.test", password=PASSWORD)

    response = await auth_routes.password_login(body, _request(), return_to=None)

    assert response.status_code == 200
    resolver.assert_awaited_once()
    assert resolver.await_args.args[1] == "Person@Example.test"


@pytest.mark.asyncio
async def test_email_sign_in_with_mfa_stops_at_the_second_factor(redis, resolver) -> None:
    resolver.return_value = _email_user(totp_enabled=True, totp_secret="enc")
    body = auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)

    result = await auth_routes.password_login(body, _request(), return_to=None)

    assert result.mfa_required is True
    assert result.mfa_session_token
    assert redis.store[f"mfa_pending:{result.mfa_session_token}"] == "u-email"
    auth_routes._create_session_and_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_identifier_counter_is_normalized(redis, resolver) -> None:
    for identifier in ("Person@Example.test", " person@example.test", "PERSON@EXAMPLE.TEST"):
        body = auth_routes.PasswordLoginRequest(login_id=identifier, password="wrong")
        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(body, _request(), return_to=None)

    assert redis.store["login_attempts:id:person@example.test"] == 3


@pytest.mark.asyncio
async def test_login_id_and_email_share_the_account_budget(redis, resolver) -> None:
    # Both identifiers resolve to one account: they draw on one counter.
    resolver.return_value = _email_user(user_id="u-shared")
    identifiers = ["admin-x", "person@example.test"] * auth_routes._MAX_LOGIN_ATTEMPTS
    for identifier in identifiers[: auth_routes._MAX_LOGIN_ATTEMPTS]:
        body = auth_routes.PasswordLoginRequest(login_id=identifier, password="wrong")
        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(body, _request(), return_to=None)

    assert redis.store["login_attempts:user:u-shared"] == auth_routes._MAX_LOGIN_ATTEMPTS
    for identifier in ("admin-x", "person@example.test"):
        body = auth_routes.PasswordLoginRequest(login_id=identifier, password=PASSWORD)
        with pytest.raises(auth_routes.HTTPException) as exc_info:
            await auth_routes.password_login(body, _request(), return_to=None)
        assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_success_clears_the_account_budget(redis, resolver) -> None:
    resolver.return_value = _email_user(user_id="u-shared")
    wrong = auth_routes.PasswordLoginRequest(login_id="admin-x", password="wrong")
    with pytest.raises(InvalidCredentialsError):
        await auth_routes.password_login(wrong, _request(), return_to=None)
    assert redis.store["login_attempts:user:u-shared"] == 1

    right = auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)
    await auth_routes.password_login(right, _request(), return_to=None)

    assert "login_attempts:user:u-shared" not in redis.store


@pytest.mark.asyncio
async def test_an_identifier_cannot_collide_with_an_account_key(redis, resolver) -> None:
    # "user:u-1" typed as a login id is an unknown identifier, not account u-1.
    body = auth_routes.PasswordLoginRequest(login_id="user:u-1", password="wrong")
    with pytest.raises(InvalidCredentialsError):
        await auth_routes.password_login(body, _request(), return_to=None)
    assert "login_attempts:user:u-1" not in redis.store


@pytest.mark.asyncio
async def test_no_per_client_lockout(redis, resolver) -> None:
    # Behind a proxy without forwarded headers every request shares one
    # address; failures across identifiers must not lock everyone out.
    for i in range(auth_routes._MAX_LOGIN_ATTEMPTS * 10):
        body = auth_routes.PasswordLoginRequest(login_id=f"user{i}@example.test", password="x")
        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(body, _request("203.0.113.50"), return_to=None)

    resolver.return_value = _email_user()
    ok = auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)
    response = await auth_routes.password_login(ok, _request("203.0.113.50"), return_to=None)
    assert response.status_code == 200
    assert not [k for k in redis.store if "203.0.113.50" in k]


@pytest.mark.asyncio
async def test_bcrypt_runs_off_the_event_loop(redis, resolver, monkeypatch) -> None:
    offloaded: list[object] = []

    async def _to_thread(func, /, *args, **kwargs):
        offloaded.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(auth_routes.asyncio, "to_thread", _to_thread)
    body = auth_routes.PasswordLoginRequest(login_id="ghost@example.test", password="guess")
    with pytest.raises(InvalidCredentialsError):
        await auth_routes.password_login(body, _request(), return_to=None)

    resolver.return_value = _email_user()
    ok = auth_routes.PasswordLoginRequest(login_id="person@example.test", password=PASSWORD)
    await auth_routes.password_login(ok, _request(), return_to=None)

    assert offloaded.count(auth_routes.verify_password) == 2


@pytest.mark.asyncio
async def test_a_miss_still_runs_bcrypt(redis, resolver) -> None:
    body = auth_routes.PasswordLoginRequest(login_id="ghost@example.test", password="guess")
    with patch.object(auth_routes, "verify_password", return_value=False) as verify:
        with pytest.raises(InvalidCredentialsError):
            await auth_routes.password_login(body, _request(), return_to=None)

    verify.assert_called_once()
    assert verify.call_args.args[1] == auth_routes._dummy_password_hash()


@pytest.mark.asyncio
async def test_error_is_generic(redis, resolver) -> None:
    body = auth_routes.PasswordLoginRequest(login_id="ghost@example.test", password="guess")
    with pytest.raises(InvalidCredentialsError) as exc_info:
        await auth_routes.password_login(body, _request(), return_to=None)
    assert "ghost" not in str(exc_info.value)


@pytest.fixture(autouse=True)
def _password_unchanged(monkeypatch):
    """The #1809 re-check reads Postgres; these tests stub the database."""
    monkeypatch.setattr(auth_routes, "_password_still_current", AsyncMock(return_value=True))
