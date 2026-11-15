"""The admin delete and erase routes against Postgres: a NUL answers 422 (#2857).

``api.main.app`` with ``get_db`` on the test database and a session admin.
A NUL in the path ``user_id`` (delete, erase) or in ``reason_detail`` (erase)
answers 422 and changes nothing; before, the driver refused the value and the
request ended as a server error. A ``user_id`` with another control character still
reaches the route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from models.auth import User
from models.erasure import ErasureRequest

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
ERASE_BODY = {"reason_code": "user_request_via_support"}


@pytest.fixture
async def world(async_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    marker = uuid4().hex[:8]
    # A control character other than a NUL, a space and a percent sign.
    target = f"local:nul-{marker}\x01 %"
    async with maker() as db:
        db.add(User(user_id=target, email=f"nul-{marker}@example.com", role="user"))
        await db.commit()

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as db:
            yield db

    async def _admin() -> dict:
        return dict(ADMIN_SESSION)

    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_optional] = _admin
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, maker, target
    finally:
        app.dependency_overrides.clear()
        async with maker() as db:
            await db.execute(delete(ErasureRequest).where(ErasureRequest.user_id == target))
            await db.execute(delete(User).where(User.user_id == target))
            await db.commit()


async def _state(maker, user_id: str) -> tuple[int, int]:
    async with maker() as db:
        users = await db.scalar(
            select(func.count()).select_from(User).where(User.user_id == user_id)
        )
        requests = await db.scalar(
            select(func.count())
            .select_from(ErasureRequest)
            .where(ErasureRequest.user_id == user_id)
        )
    return users, requests


async def test_a_nul_in_the_path_of_the_admin_delete_is_422_and_deletes_nothing(world) -> None:
    client, maker, target = world
    response = await client.delete(f"/api/v1/admin/users/{quote(target, safe='')}%00")
    assert response.status_code == 422, response.text
    assert await _state(maker, target) == (1, 0)


async def test_a_nul_in_the_path_of_the_admin_erase_is_422_and_erases_nothing(world) -> None:
    client, maker, target = world
    response = await client.post(
        f"/api/v1/admin/users/{quote(target, safe='')}%00/erase", json=ERASE_BODY
    )
    assert response.status_code == 422, response.text
    assert await _state(maker, target) == (1, 0)


async def test_a_nul_in_reason_detail_is_422_and_creates_no_request(world) -> None:
    client, maker, target = world
    response = await client.post(
        f"/api/v1/admin/users/{quote(target, safe='')}/erase",
        json={**ERASE_BODY, "reason_detail": "support ticket\x00"},
    )
    assert response.status_code == 422, response.text
    assert await _state(maker, target) == (1, 0)


async def test_a_user_id_with_another_control_character_is_still_deleted(world) -> None:
    client, maker, target = world
    response = await client.delete(f"/api/v1/admin/users/{quote(target, safe='')}")
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == target
    assert await _state(maker, target) == (0, 0)
