"""The FastAPI app over ``httpx.ASGITransport`` for admin route DB tests.

``get_db`` is the test session and ``require_admin`` answers a fixed admin
principal, so a test reads the raw response body of an admin route (query
validation and response models included) against real rows.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db

ADMIN_PRINCIPAL = {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}


@asynccontextmanager
async def admin_http(
    db_session: AsyncSession, principal: dict | None = None
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """An HTTP client whose admin requests run on ``db_session``.

    Args:
        db_session: The test database session every request uses.
        principal: The caller ``require_admin`` answers (default: a session admin).
    """

    async def _db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _admin() -> dict:
        return dict(principal or ADMIN_PRINCIPAL)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = _admin
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)
