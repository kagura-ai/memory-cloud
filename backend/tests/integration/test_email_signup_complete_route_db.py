"""``POST /api/v1/auth/signup/email/complete`` with registration closed, on Postgres (#1734).

The route hands over to ``EmailSignupService.complete``; this pins the HTTP
answer end to end. A live link minted while registration was open, then:

- the gate turned on and no beta invite or invitation admits the address, or
- the gate off with ``ALLOW_REGISTRATION=false`` and no invitation,

answers ``403 AUTH-308``, sets no session cookie and creates no ``users`` row.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.email_signup_service as svc_module
from api.main import app
from api.routes import email_signup as routes
from config.settings import get_settings
from models.auth import User
from models.signup_gate import SignupGateConfig
from services.email_signup_service import EmailSignupService
from services.signup_gate_service import SignupGateService

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "Correct-Horse-Battery-42"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def open_then_closed(db_session: AsyncSession, monkeypatch):
    """Not the first account; gate off and registration open for the request."""
    db_session.add(
        User(
            email=f"seed-{uuid4().hex[:10]}@example.test",
            user_id=f"seed-{uuid4().hex[:10]}",
            role="user",
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_email_signup", True)
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "terms_version", "")
    monkeypatch.setattr(svc_module, "_claim_notice_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(svc_module, "_over_daily_invite_limit", AsyncMock(return_value=False))

    async def _no_limit(key: str, ttl: int | None = None) -> int:
        return 1

    monkeypatch.setattr(routes, "increment_counter", _no_limit)
    gate = SignupGateService(db_session)
    config = await gate.get_config()
    previous = (config.enabled, config.mode)
    await gate.update_config(enabled=False, mode="manual")
    yield settings
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.commit()


@pytest.mark.parametrize("closed_by", ["gate_on_without_invite", "registration_off"])
async def test_closed_registration_creates_no_user(
    open_then_closed, maker, db_session, monkeypatch, closed_by
) -> None:
    email = f"closed-{uuid4().hex[:10]}@example.test"
    async with maker() as s:
        pending = await EmailSignupService(s).request(
            email=email, beta_invite_token_hash=None, locale=None, timezone=None
        )
    token = pending.verify_url.split("token=", 1)[1]
    if closed_by == "gate_on_without_invite":
        await SignupGateService(db_session).update_config(enabled=True, mode="manual")
    else:
        monkeypatch.setattr(open_then_closed, "allow_registration", False)

    async def _db():
        async with maker() as s:
            yield s

    app.dependency_overrides[routes.get_db] = _db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.post(
                "/api/v1/auth/signup/email/complete",
                json={"token": token, "new_password": PASSWORD},
            )
    finally:
        app.dependency_overrides.pop(routes.get_db, None)

    assert response.status_code == 403
    assert response.json()["error"] == "AUTH-308"
    assert "set-cookie" not in response.headers
    db_session.expire_all()
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(func.lower(User.email) == email)
        )
    ) == 0
