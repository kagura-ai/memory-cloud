"""The admin role PUT against Postgres (#2854).

``PUT /api/v1/admin/users/{user_id}/role`` accepts the two system roles the
``valid_role`` CHECK of ``users`` allows, ``admin`` and ``user``. Any other
name, ``read_only`` included, answers 422 at request validation: no statement
runs, the stored role and the audit log stay as they were.

``api.main.app`` with ``get_db`` on the test database and a session admin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog, User

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    client: httpx.AsyncClient
    target: str
    other_admin: str
    statements: list[str]

    async def put_role(self, role: object) -> httpx.Response:
        return await self.client.put(f"/api/v1/admin/users/{self.target}/role", json={"role": role})

    async def stored_role(self) -> str | None:
        async with self.factory() as db:
            return await db.scalar(select(User.role).where(User.user_id == self.target))

    async def role_audit_rows(self) -> list[tuple[str | None, str | None]]:
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(AuditLog.old_value_hash, AuditLog.new_value_hash)
                    .where(
                        AuditLog.action == "system_role_update",
                        AuditLog.resource == f"user:{self.target}",
                    )
                    .order_by(AuditLog.id)
                )
            ).all()
        return [tuple(row) for row in rows]


@pytest.fixture
async def world(async_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    marker = uuid4().hex[:8]
    target, other_admin = f"role-put-{marker}-user", f"role-put-{marker}-admin"
    async with maker() as db:
        db.add(User(user_id=target, email=f"{target}@example.com", role="user"))
        # A second admin, so the demotion back to ``user`` is not the last admin's.
        db.add(User(user_id=other_admin, email=f"{other_admin}@example.com", role="admin"))
        await db.commit()

    statements: list[str] = []

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as db:
            original = db.execute

            async def _recorded(statement, *args, **kwargs):
                statements.append(str(statement))
                return await original(statement, *args, **kwargs)

            db.execute = _recorded  # type: ignore[method-assign]
            yield db

    async def _admin() -> dict:
        return dict(ADMIN_SESSION)

    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_optional] = _admin
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield World(maker, client, target, other_admin, statements)
    finally:
        app.dependency_overrides.clear()
        async with maker() as db:
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.action == "system_role_update",
                    AuditLog.resource.in_([f"user:{target}", f"user:{other_admin}"]),
                )
            )
            await db.execute(delete(User).where(User.user_id.in_([target, other_admin])))
            await db.commit()


@pytest.mark.parametrize("role", ["read_only", "owner", "ADMIN", ""])
async def test_put_role_read_only_answers_422_and_stores_nothing(world: World, role: str) -> None:
    response = await world.put_role(role)

    assert response.status_code == 422, response.text
    assert world.statements == []
    assert await world.stored_role() == "user"
    assert await world.role_audit_rows() == []


async def test_put_role_read_only_on_an_admin_answers_422_and_keeps_the_admin(
    world: World,
) -> None:
    world.target = world.other_admin
    response = await world.put_role("read_only")

    assert response.status_code == 422, response.text
    assert world.statements == []
    assert await world.stored_role() == "admin"
    assert await world.role_audit_rows() == []


async def test_put_role_user_to_admin_to_user_is_stored_with_its_audit_rows(
    world: World,
) -> None:
    promoted = await world.put_role("admin")
    assert promoted.status_code == 200, promoted.text
    assert promoted.json() == {
        "message": "User role updated to admin",
        "user_id": world.target,
        "new_role": "admin",
    }
    assert await world.stored_role() == "admin"

    demoted = await world.put_role("user")
    assert demoted.status_code == 200, demoted.text
    assert demoted.json() == {
        "message": "User role updated to user",
        "user_id": world.target,
        "new_role": "user",
    }
    assert await world.stored_role() == "user"
    assert await world.role_audit_rows() == [("user", "admin"), ("admin", "user")]


async def test_the_openapi_document_lists_admin_and_user_as_the_only_roles() -> None:
    schema = app.openapi()["components"]["schemas"]["RoleUpdateRequest"]
    assert schema["properties"]["role"]["enum"] == ["admin", "user"]
