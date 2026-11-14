"""How an e-mail account was admitted is recorded once, on real rows (#2803).

The real ``EmailSignupService.complete`` against Postgres stores each e-mail
admission on the new ``users`` row: open registration, a workspace
invitation, a beta invite (user-minted: issuer ``user`` and the inviter;
admin-minted: issuer ``admin``, no inviter) and the first user (private
schema). An inviter whose account is gone before the claim refuses the
sign-up at the inviter pre-check (not as a lost claim) and writes nothing.
While the inviter's erasure holds their ``users`` row, the completion waits
on that row, before touching any ``beta_invites`` row (erasure's lock order),
then refuses once the inviter is deleted, without a deadlock.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import services.email_signup_service as svc_module
from config.settings import get_settings
from db.base import Base
from models.auth import (
    AuditLog,
    EmailSignupRequest,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.beta_invite_service import BetaInviteService
from services.email_signup_service import EmailSignupService, SignupConsents
from services.signup_gate_service import SignupGateService
from tests.integration.test_first_account_race_db import _tables
from utils.datetime import utcnow
from utils.exceptions import SignupNotAllowedError
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "Correct-Horse-Battery-42"
LABEL = "SENTINEL-LABEL-2803-email"


def _addr(prefix: str = "mail") -> str:
    return f"{prefix}-{uuid4().hex[:10]}@example.test"


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


async def _link(maker, email: str, beta_hash: str | None = None) -> str:
    raw = uuid4().hex + uuid4().hex
    async with maker() as s:
        s.add(
            EmailSignupRequest(
                email=email,
                token_hash=sha256_hex(raw),
                beta_invite_token_hash=beta_hash,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        await s.commit()
    return raw


async def _complete(maker, raw: str):
    async with maker() as s:
        return await EmailSignupService(s).complete(
            raw_token=raw,
            new_password=PASSWORD,
            name="N",
            consents=SignupConsents(terms_version=None),
            ip_address="203.0.113.5",
            user_agent="pytest",
        )


async def _record(maker, user_id: str) -> tuple:
    async with maker() as s:
        row = (
            await s.execute(
                select(
                    User.admitted_via,
                    User.admitted_beta_invite_id,
                    User.admitted_invite_issuer,
                    User.admitted_by_user_id,
                ).where(User.user_id == user_id)
            )
        ).one()
        return tuple(row)


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession, maker, env) -> AsyncIterator[SimpleNamespace]:
    people: list[str] = []

    async def person(role: str = "user") -> str:
        user_id = f"p-{uuid4().hex[:10]}"
        db_session.add(
            User(
                email=_addr("person"),
                user_id=user_id,
                role=role,
                auth_method="oauth",
                auth_provider="google",
            )
        )
        await db_session.commit()
        people.append(user_id)
        return user_id

    owner = await person()
    ws = Workspace(id=uuid4(), name=f"ws-{uuid4().hex[:8]}", owner_user_id=owner, plan_name="pro")
    db_session.add(ws)
    await db_session.flush()
    ws_id = ws.id
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role="owner"))
    await db_session.commit()

    async def invite(inviter: str) -> tuple[UUID, str]:
        token = uuid4().hex + uuid4().hex
        row = BetaInvite(
            token_hash=sha256_hex(token),
            inviter_user_id=inviter,
            expires_at=utcnow() + timedelta(days=7),
            label=LABEL,
        )
        db_session.add(row)
        await db_session.commit()
        return row.id, token

    async def invitation(email: str) -> None:
        db_session.add(
            WorkspaceInvitation(
                workspace_id=ws.id,
                token=uuid4().hex + uuid4().hex,
                email=email,
                role="member",
                invited_by=owner,
                expires_at=utcnow() + timedelta(days=7),
            )
        )
        await db_session.commit()

    gate = SignupGateService(db_session)
    config = await gate.get_config()
    previous = (config.enabled, config.mode)
    yield SimpleNamespace(
        db=db_session,
        person=person,
        invite=invite,
        invitation=invitation,
        gate=gate,
        people=people,
    )
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.execute(delete(Workspace).where(Workspace.id == ws_id))
    await db_session.execute(delete(User).where(User.user_id.in_(people)))
    await db_session.commit()


async def test_open_registration(world, maker, env) -> None:
    env.allow_registration = True
    await world.gate.update_config(enabled=False, mode="manual")

    created = await _complete(maker, await _link(maker, _addr()))

    assert created.admission == "open_registration"
    assert await _record(maker, created.user_id) == ("open_registration", None, None, None)


@pytest.mark.parametrize("enabled", [True, False])
async def test_workspace_invitation(world, maker, enabled) -> None:
    await world.gate.update_config(enabled=enabled, mode="manual")
    email = _addr()
    await world.invitation(email)

    created = await _complete(maker, await _link(maker, email))

    assert created.admission == "workspace_invitation"
    assert await _record(maker, created.user_id) == ("workspace_invitation", None, None, None)


@pytest.mark.parametrize("mode", ["session", "service", "off"])
async def test_user_minted_beta_invite(world, maker, env, mode) -> None:
    env.system_admin_mode = mode
    await world.gate.update_config(enabled=True, mode="manual")
    inviter = await world.person()
    invite_id, token = await world.invite(inviter)

    created = await _complete(maker, await _link(maker, _addr(), sha256_hex(token)))

    assert created.admission == "beta_invite"
    assert await _record(maker, created.user_id) == ("beta_invite", invite_id, "user", inviter)


@pytest.mark.parametrize("mode", ["session", "service", "off"])
async def test_admin_minted_beta_invite(world, maker, env, mode) -> None:
    """The issuer is the inviter's users.role, in every SYSTEM_ADMIN_MODE."""
    env.system_admin_mode = mode
    await world.gate.update_config(enabled=True, mode="manual")
    admin = await world.person(role="admin")
    invite_id, token = await world.invite(admin)

    created = await _complete(maker, await _link(maker, _addr(), sha256_hex(token)))

    assert await _record(maker, created.user_id) == ("beta_invite", invite_id, "admin", None)


async def _nothing_written(maker, email: str, invite_id: UUID) -> None:
    async with maker() as s:
        assert (
            await s.scalar(select(func.count()).select_from(User).where(User.email == email))
        ) == 0
        assert (
            await s.scalar(
                select(func.count())
                .select_from(SignupAllowlistEntry)
                .where(SignupAllowlistEntry.subject_label == email)
            )
        ) == 0
        assert (
            await s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.resource == f"beta_invite:{invite_id}")
            )
        ) == 0


def _refused_at_the_inviter_check(events: list[dict], invite_id: UUID) -> bool:
    """The inviter pre-check refused, not a lost claim (the cascade of the
    inviter's deletion also removes the invite, so a claim would fail too)."""
    names = [e.get("event") for e in events]
    return "beta_invite_redeem_lost_race" not in names and any(
        e.get("event") == "beta_invite_redeem_rejected" and e.get("invite_id") == str(invite_id)
        for e in events
    )


async def test_inviter_gone_before_the_claim_refuses_at_the_inviter_check(
    world, maker, monkeypatch
) -> None:
    await world.gate.update_config(enabled=True, mode="manual")
    inviter = await world.person()
    invite_id, token = await world.invite(inviter)
    email = _addr("gone")
    real_find = BetaInviteService.find_redeemable

    async def _find_then_erase(self, token_hash, now):
        found = await real_find(self, token_hash, now)
        # The inviter's account goes away between the pre-check and the claim.
        async with maker() as other:
            await other.execute(delete(User).where(User.user_id == inviter))
            await other.commit()
        return found

    monkeypatch.setattr(BetaInviteService, "find_redeemable", _find_then_erase)

    with structlog.testing.capture_logs() as events:
        with pytest.raises(SignupNotAllowedError):
            await _complete(maker, await _link(maker, email, sha256_hex(token)))

    assert _refused_at_the_inviter_check(events, invite_id)
    await _nothing_written(maker, email, invite_id)


async def _blocked_by(
    maker, holder_pid: int, task: asyncio.Task, timeout: float = 15
) -> str | None:
    """The statement of the backend ``holder_pid`` blocks, or None once
    ``task`` finished without waiting on it."""

    async def poll() -> str | None:
        while not task.done():
            async with maker() as observer:
                query = await observer.scalar(
                    text(
                        "SELECT query FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid)) LIMIT 1"
                    ),
                    {"pid": holder_pid},
                )
            if query is not None:
                return query
            await asyncio.sleep(0.02)
        return None

    return await asyncio.wait_for(poll(), timeout)


@pytest.mark.timeout(90)
async def test_inviter_erasure_in_flight_the_completion_waits_on_the_users_row(
    world, maker
) -> None:
    """Lock order (#2803): the inviter's ``users`` row (KEY SHARE) before any
    ``beta_invites`` row, the order erasure takes (the row, then the cascade
    into the invites). An erasure holding the row makes the completion wait
    on it; the deletion then refuses the sign-up, and nobody deadlocks."""
    await world.gate.update_config(enabled=True, mode="manual")
    inviter = await world.person()
    invite_id, token = await world.invite(inviter)
    email = _addr("held")
    raw = await _link(maker, email, sha256_hex(token))

    with structlog.testing.capture_logs() as events:
        async with maker() as eraser:
            await eraser.execute(select(User.id).where(User.user_id == inviter).with_for_update())
            eraser_pid = await eraser.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(_complete(maker, raw))
            try:
                waiting = await _blocked_by(maker, eraser_pid, task)
                assert waiting is not None, "the completion never waited on the inviter's row"
                assert "FOR KEY SHARE" in waiting and "FROM users" in waiting, waiting
                await eraser.execute(delete(User).where(User.user_id == inviter))
                await eraser.commit()
                with pytest.raises(SignupNotAllowedError):
                    await asyncio.wait_for(task, 30)
            finally:
                if not task.done():
                    task.cancel()

    assert _refused_at_the_inviter_check(events, invite_id)
    await _nothing_written(maker, email, invite_id)


# ---------------------------------------------------------------------------
# First user: an empty users table, in a private schema.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def empty_maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = "admission_mail_" + uuid4().hex
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
    )
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema, "timezone": "UTC"}},
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
            await conn.execute(
                SignupGateConfig.__table__.insert().values(id=1, enabled=True, mode="manual")
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


async def test_first_user(empty_maker, env) -> None:
    created = await _complete(empty_maker, await _link(empty_maker, _addr("first")))

    assert created.admission == "first_user"
    assert await _record(empty_maker, created.user_id) == ("first_user", None, None, None)
