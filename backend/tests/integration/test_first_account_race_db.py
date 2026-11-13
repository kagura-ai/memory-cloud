"""First sign-ups that complete at the same moment: one initial admin (#2788).

Two real connections per test, in a random private schema of
TEST_DATABASE_URL (the shared test tables always hold accounts, so no test
there can be "first"). Advisory locks are per database, not per schema, so the
choreography filters ``pg_locks`` on the production lock key and the current
database.

Choreography (no sleeps): a holder connection takes the first-account lock;
the two sign-ups start, count an empty users table and queue on the lock; the
test waits until ``pg_locks`` shows both waiting, releases the holder and
gathers the results.

REQUIRE_TEST_DATABASE=1 makes missing infrastructure fail rather than skip.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import models  # noqa: F401  (every table registered for the foreign-key closure)
import services.email_signup_service as svc_module
from auth.roles import FIRST_ACCOUNT_LOCK_KEY, Role, RoleManager
from config.settings import get_settings
from db.base import Base
from models.announcement_consent import AnnouncementEmailConsent
from models.auth import (
    AuditLog,
    EmailSignupRequest,
    User,
    UserOAuthProvider,
    WorkspaceInvitation,
)
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from models.terms import TermsAcceptance
from services.email_signup_service import (
    CreatedEmailAccount,
    EmailSignupService,
    SignupConsents,
)
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    ProviderLinkConfirmable,
    SignupAccountExistsError,
    SignupNotAllowedError,
)
from utils.hashing import sha256_hex

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

PASSWORD = "Correct-Horse-Battery-42"
WAIT_SECONDS = 10.0

# The first-account lock's waiters in THIS database (pg_locks is cluster-wide).
_WAITERS = text(
    """
    SELECT l.pid
    FROM pg_locks l, (SELECT hashtextextended(:key, 0) AS h) k
    WHERE l.locktype = 'advisory'
      AND NOT l.granted
      AND l.objsubid = 1
      AND l.classid::bigint = ((k.h >> 32) & 4294967295)
      AND l.objid::bigint = (k.h & 4294967295)
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
    """
)
_GRANTED_ADVISORY = text(
    """
    SELECT count(*) FROM pg_locks
    WHERE locktype = 'advisory' AND granted AND pid = ANY(:pids)
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
    """
)


def _tables() -> set[Any]:
    tables = {
        m.__table__
        for m in (
            User,
            UserOAuthProvider,
            AuditLog,
            EmailSignupRequest,
            SignupGateConfig,
            SignupAllowlistEntry,
            BetaInvite,
            WorkspaceInvitation,
            TermsAcceptance,
            AnnouncementEmailConsent,
        )
    }
    while True:
        dependencies = {fk.column.table for table in tables for fk in table.foreign_keys}
        if dependencies <= tables:
            return tables
        tables.update(dependencies)


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = "first_account_" + uuid4().hex
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
    )
    # NullPool: the holder, both sign-ups and the poller each need their own connection.
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {"search_path": schema, "timezone": "UTC"},
            "timeout": 5,
        },
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
            await conn.execute(
                SignupGateConfig.__table__.insert().values(id=1, enabled=False, mode="manual")
            )
    except (OSError, ConnectionError, TimeoutError) as exc:
        await engine.dispose()
        if os.environ.get("REQUIRE_TEST_DATABASE") == "1":
            pytest.fail(f"PostgreSQL required: {type(exc).__name__}")
        pytest.skip("PostgreSQL test server unavailable")
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "session")
    monkeypatch.setattr(settings, "enable_email_signup", True)
    monkeypatch.setattr(settings, "enable_beta_invites", True)
    monkeypatch.setattr(settings, "allow_registration", False)
    monkeypatch.setattr(settings, "terms_version", "")
    monkeypatch.setattr(svc_module, "_claim_notice_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(svc_module, "_over_daily_invite_limit", AsyncMock(return_value=False))
    return settings


@contextmanager
def _get_db_on(maker: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    """Point ``db.base.get_db`` at the private schema ONCE for every task."""

    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    async def _fake() -> AsyncIterator[AsyncSession]:
        async with _scope() as s:
            yield s

    with patch("db.base.get_db", new=_fake):
        yield


def _addr(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}@example.test"


async def _link(maker: async_sessionmaker[AsyncSession], email: str) -> str:
    """A live sign-up link for ``email`` (the row ``request()`` would store)."""
    raw = uuid4().hex + uuid4().hex
    async with maker() as s:
        s.add(
            EmailSignupRequest(
                email=email.strip().lower(),
                token_hash=sha256_hex(raw),
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        await s.commit()
    return raw


async def _complete(maker: async_sessionmaker[AsyncSession], raw: str) -> CreatedEmailAccount:
    async with maker() as s:
        return await EmailSignupService(s).complete(
            raw_token=raw,
            new_password=PASSWORD,
            name="N",
            consents=SignupConsents(terms_version=None),
            ip_address="203.0.113.5",
            user_agent="pytest",
        )


async def _oauth(email: str) -> Role:
    return await RoleManager(use_postgres=True).ensure_user(
        email=email,
        user_id=f"g-{uuid4().hex[:12]}",
        auth_provider="google",
        email_verified=True,
        email_authoritative=False,
    )


async def _waiting_pids(conn: AsyncConnection) -> list[int]:
    rows = await conn.execute(_WAITERS, {"key": FIRST_ACCOUNT_LOCK_KEY})
    return [r[0] for r in rows]


async def _race(
    maker: async_sessionmaker[AsyncSession],
    *sign_ups: Awaitable[Any],
    check_waiters: Any = None,
) -> list[Any]:
    """Queue every sign-up on a held first-account lock, then release it."""
    engine = maker.kw["bind"]
    tasks: list[asyncio.Task[Any]] = []
    async with engine.connect() as holder, engine.connect() as poller:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": FIRST_ACCOUNT_LOCK_KEY},
        )
        try:
            with _get_db_on(maker):
                tasks = [asyncio.ensure_future(s) for s in sign_ups]
                deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
                while True:
                    pids = await _waiting_pids(poller)
                    if len(pids) == len(tasks):
                        break
                    done = [t for t in tasks if t.done()]
                    assert not done, f"a sign-up finished without the lock: {done[0].result()!r}"
                    assert asyncio.get_running_loop().time() < deadline, (
                        "the sign-ups never waited on the first-account lock"
                    )
                    await poller.rollback()
                    await asyncio.sleep(0.05)
                if check_waiters is not None:
                    await check_waiters(poller, pids)
                await holder.rollback()  # releases the lock
                return list(
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), WAIT_SECONDS
                    )
                )
        finally:
            await holder.rollback()
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


async def _accounts(maker: async_sessionmaker[AsyncSession]) -> list[tuple[str, bool]]:
    async with maker() as s:
        rows = await s.execute(select(User.role, User.is_initial_admin).order_by(User.role))
        return [(r[0], r[1]) for r in rows]


def _no_deadlock(results: list[Any]) -> None:
    for r in results:
        assert not isinstance(r, DBAPIError), f"database error: {r!r}"


async def test_two_first_oauth_sign_ups_make_one_admin(env, maker) -> None:
    results = await _race(maker, _oauth(_addr("a")), _oauth(_addr("b")))

    _no_deadlock(results)
    assert sorted(results) == sorted([Role.ADMIN, Role.USER])
    assert sorted(await _accounts(maker)) == [("admin", True), ("user", False)]


async def test_two_first_email_completions_on_a_closed_gate_admit_one(env, maker) -> None:
    first, second = await _link(maker, _addr("e1")), await _link(maker, _addr("e2"))

    results = await _race(maker, _complete(maker, first), _complete(maker, second))

    _no_deadlock(results)
    created = [r for r in results if isinstance(r, CreatedEmailAccount)]
    refused = [r for r in results if isinstance(r, SignupNotAllowedError)]
    assert len(created) == 1 and len(refused) == 1, results
    assert (created[0].role, created[0].admission) == (Role.ADMIN, "first_user")
    assert await _accounts(maker) == [("admin", True)]


@pytest.mark.parametrize("mode", ["off", "service"])
async def test_two_first_email_completions_admit_one_in_every_mode(env, maker, mode) -> None:
    env.system_admin_mode = mode
    first, second = await _link(maker, _addr("e1")), await _link(maker, _addr("e2"))

    results = await _race(maker, _complete(maker, first), _complete(maker, second))

    _no_deadlock(results)
    created = [r for r in results if isinstance(r, CreatedEmailAccount)]
    refused = [r for r in results if isinstance(r, SignupNotAllowedError)]
    assert len(created) == 1 and len(refused) == 1, results
    assert (created[0].role, created[0].admission) == (Role.USER, "first_user")
    assert await _accounts(maker) == [("user", False)]


async def test_oauth_and_email_first_sign_ups_make_one_admin(env, maker) -> None:
    env.allow_registration = True
    raw = await _link(maker, _addr("mixed-email"))

    results = await _race(maker, _oauth(_addr("mixed-oauth")), _complete(maker, raw))

    _no_deadlock(results)
    roles = [r.role if isinstance(r, CreatedEmailAccount) else r for r in results]
    assert sorted(roles) == sorted([Role.ADMIN, Role.USER])
    assert sorted(await _accounts(maker)) == [("admin", True), ("user", False)]


async def test_first_account_lock_comes_before_the_address_lock(env, maker) -> None:
    """Same address by email and OAuth: no waiter holds an advisory lock, no deadlock."""
    email = _addr("same")
    raw = await _link(maker, email)

    async def no_advisory_lock_held(poller: AsyncConnection, pids: list[int]) -> None:
        held = (await poller.execute(_GRANTED_ADVISORY, {"pids": pids})).scalar()
        assert held == 0, "a sign-up waiting on the first-account lock holds another lock"

    results = await _race(
        maker,
        _complete(maker, raw),
        _oauth(email.upper()),
        check_waiters=no_advisory_lock_held,
    )

    _no_deadlock(results)
    losers = [r for r in results if isinstance(r, BaseException)]
    for loser in losers:
        assert isinstance(
            loser, ConflictError | ProviderLinkConfirmable | SignupAccountExistsError
        ), repr(loser)
    async with maker() as s:
        rows = (
            await s.execute(
                select(func.count()).select_from(User).where(func.lower(User.email) == email)
            )
        ).scalar()
    assert rows == 1


async def test_existing_accounts_never_wait_on_the_lock(env, maker) -> None:
    """Once any account exists, a sign-up runs while another holds the lock."""
    env.allow_registration = True
    async with maker() as s:
        s.add(
            User(
                email=_addr("seed"),
                user_id=f"seed-{uuid4().hex[:10]}",
                role="user",
                auth_method="oauth",
                auth_provider="google",
            )
        )
        await s.commit()
    raw = await _link(maker, _addr("fast-email"))
    engine = maker.kw["bind"]

    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": FIRST_ACCOUNT_LOCK_KEY},
        )
        try:
            with _get_db_on(maker):
                role = await asyncio.wait_for(_oauth(_addr("fast-oauth")), 5)
                created = await asyncio.wait_for(_complete(maker, raw), 5)
        finally:
            await holder.rollback()

    assert role == Role.USER
    assert (created.role, created.admission) == (Role.USER, "open_registration")


async def test_off_mode_first_sign_ups_are_users(env, maker) -> None:
    env.system_admin_mode = "off"
    with _get_db_on(maker):
        results = await asyncio.wait_for(
            asyncio.gather(_oauth(_addr("o1")), _oauth(_addr("o2")), return_exceptions=True),
            WAIT_SECONDS,
        )

    assert results == [Role.USER, Role.USER]
    assert await _accounts(maker) == [("user", False), ("user", False)]
