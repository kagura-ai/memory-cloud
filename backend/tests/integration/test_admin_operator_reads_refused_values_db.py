"""Values Postgres would refuse, and the largest ones it takes (#2828, #2843, #2833, #2832, #2846).

Over HTTP against the whole application and a real database: a NUL in a
string filter or in the account id of a role change, and an ``offset`` past
64 bits, answer 422; the largest ``offset`` and a 255-character filter are
answered by the statement.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.auth import AuditLog

pytestmark = pytest.mark.asyncio(loop_scope="session")

USERS = "/api/v1/admin/users"
DELETED_CONTEXTS = "/api/v1/admin/contexts/deleted"
EMBEDDING_FAILURES = "/api/v1/admin/embedding/failures"
FORCE_TRANSFER_REQUESTS = "/api/v1/admin/force-transfer-requests"
SYSTEM_ADMINS = "/api/v1/admin/system-admins"
MAX_OFFSET = 2**63 - 1


@pytest_asyncio.fixture(loop_scope="session")
async def client(async_engine) -> AsyncIterator[httpx.AsyncClient]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: build_operator_principal("ops@example.com")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"{USERS}?user_id=a%00b"),
        ("GET", f"{USERS}?search=a%00b"),
        ("GET", f"{USERS}?role=a%00b"),
        ("GET", f"{USERS}?plan=a%00b"),
        ("GET", f"{USERS}?invited_by=a%00b"),
        ("GET", f"{USERS}?offset={MAX_OFFSET + 1}"),
        ("GET", f"{USERS}?workspace_id=not-a-uuid"),
        ("GET", f"{DELETED_CONTEXTS}?created_by=a%00b"),
        ("GET", f"{DELETED_CONTEXTS}?deleted_by=a%00b"),
        ("GET", f"{DELETED_CONTEXTS}?offset={MAX_OFFSET + 1}"),
        ("GET", f"{EMBEDDING_FAILURES}?offset={MAX_OFFSET + 1}"),
        ("GET", f"{FORCE_TRANSFER_REQUESTS}?offset={MAX_OFFSET + 1}"),
        ("PUT", f"{USERS}/a%00b/role"),
        ("DELETE", f"{SYSTEM_ADMINS}/a%00b"),
    ],
)
async def test_a_value_postgres_would_refuse_is_a_422(client, method, url) -> None:
    response = await client.request(method, url, json={"role": "user"} if method == "PUT" else None)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"


@pytest.mark.parametrize(
    ("url", "rows"),
    [
        (f"{DELETED_CONTEXTS}?offset={MAX_OFFSET}", "items"),
        (f"{EMBEDDING_FAILURES}?offset={MAX_OFFSET}", "groups"),
        (f"{FORCE_TRANSFER_REQUESTS}?offset={MAX_OFFSET}", "items"),
        (f"{DELETED_CONTEXTS}?created_by={'c' * 255}&deleted_by={'d' * 255}", "items"),
        (f"{USERS}?user_id={'u' * 255}", "users"),
        # Characters a LIKE would read as wildcards, a line break and a space.
        (f"{USERS}?user_id=%25", "users"),
        (f"{USERS}?user_id=a%0Ab%20_", "users"),
        # The parameters the user list already had: the same values as before.
        (f"{USERS}?offset={MAX_OFFSET}", "users"),
        (f"{USERS}?search={'s' * 255}&role={'r' * 255}&plan={'p' * 255}", "users"),
        (f"{USERS}?search=a%0Ab%20c&role=a%0Ab&plan=a%0Ab&invited_by={'i' * 255}", "users"),
        (f"{USERS}?workspace_id=11111111-2222-4333-8444-555555555555", "users"),
        # An empty value is no filter, as it was before the id was typed.
        (f"{USERS}?workspace_id=&role=&plan=&search=&user_id=no-such-account", "users"),
        (f"{DELETED_CONTEXTS}?created_by=%25&deleted_by=_", "items"),
    ],
)
async def test_the_largest_accepted_values_are_answered_by_the_statement(client, url, rows) -> None:
    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()[rows] == []


@pytest.mark.parametrize("user_id", ["u" * 255, "a%0Ab%25%20c"])
async def test_a_role_change_for_a_long_or_unusual_unknown_id_is_404_and_writes_nothing(
    client, db_session, user_id
) -> None:
    await db_session.rollback()
    before = await db_session.scalar(select(func.count()).select_from(AuditLog))
    put = await client.put(f"{USERS}/{user_id}/role", json={"role": "user"})
    demote = await client.delete(f"{SYSTEM_ADMINS}/{user_id}")
    assert (put.status_code, demote.status_code) == (404, 404), (put.text, demote.text)
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(AuditLog)) == before
