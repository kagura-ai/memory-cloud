"""``GET /api/v1/admin/users?user_id=`` against Postgres (#2843).

Over HTTP (the real query parsing), as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds):

- the one account whose ``user_id`` is the value, with ``total`` 1, and
  ``{"users": [], "total": 0}`` for an id that is no account;
- an exact match: a prefix, another letter case and padded values find nothing;
- ANDed with ``role``, ``admitted_via``, ``workspace_id`` and ``search``;
- ``include_workspaces=true`` carries the account's live workspaces;
- an unknown id costs the count and the select; a known id adds the memory
  count and the cap query, and nothing about contexts or permissions.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import admin as admin_routes
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.auth import User, Workspace, WorkspaceMember
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: principal
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """Three accounts whose ids differ by case and by a prefix, one in two workspaces."""
    tag = uuid4().hex[:8]
    # Mixed case on purpose: the match is case-sensitive.
    target = f"Acct-{tag}"
    longer = f"Acct-{tag}-2"
    lower = target.lower()
    for handle, user_id, role, via in (
        ("target", target, "user", "allowlist"),
        ("longer", longer, "admin", "unknown"),
        ("lower", lower, "user", "unknown"),
    ):
        db_session.add(
            User(
                email=f"{handle}-{tag}@lookup.example",
                user_id=user_id,
                name=f"Person {user_id}",
                role=role,
                admitted_via=via,
                auth_method="oauth",
                auth_provider="google",
            )
        )
    live = Workspace(id=uuid4(), name=f"live-{tag}", owner_user_id=target, plan_name="pro")
    gone = Workspace(
        id=uuid4(),
        name=f"gone-{tag}",
        owner_user_id=longer,
        plan_name="free",
        deleted_at=utcnow(),
    )
    db_session.add_all([live, gone])
    await db_session.flush()
    live_id, gone_id = live.id, gone.id
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=live_id, user_id=target, role="owner"),
            WorkspaceMember(workspace_id=gone_id, user_id=target, role="member"),
        ]
    )
    await db_session.commit()

    yield SimpleNamespace(
        tag=tag, target=target, longer=longer, lower=lower, live_id=live_id, gone_id=gone_id
    )

    await db_session.rollback()
    await db_session.execute(delete(Workspace).where(Workspace.id.in_([live_id, gone_id])))
    await db_session.execute(delete(User).where(User.user_id.in_([target, longer, lower])))
    await db_session.commit()


async def _list(client: httpx.AsyncClient, **params) -> tuple[int, dict]:
    response = await client.get("/api/v1/admin/users", params=params)
    return response.status_code, response.json()


@pytest.mark.parametrize("mode", list(PRINCIPALS))
class TestUserIdFilter:
    async def test_it_returns_the_one_account_with_total_1(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            status, body = await _list(client, user_id=world.target)

        assert status == 200
        assert body["total"] == 1
        [row] = body["users"]
        assert row["id"] == world.target
        assert row["email"] == f"target-{world.tag}@lookup.example"
        assert row["name"] == f"Person {world.target}"
        assert row["role"] == "user"
        assert row["admitted_via"] == "allowlist"
        assert row["created_at"]
        assert row["workspaces"] == []

    async def test_an_id_that_is_no_account_is_an_empty_list(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            status, body = await _list(client, user_id=f"nobody-{world.tag}")

        assert (status, body) == (200, {"users": [], "total": 0})

    @pytest.mark.parametrize(
        "variant",
        ["prefix", "upper", "swapcase", "leading_space", "trailing_space", "wildcard"],
    )
    async def test_the_match_is_exact(self, world, maker, mode, variant) -> None:
        value = {
            "prefix": world.target[:-1],
            "upper": world.target.upper(),
            "swapcase": world.target.swapcase(),
            "leading_space": f" {world.target}",
            "trailing_space": f"{world.target} ",
            "wildcard": f"{world.target}%",
        }[variant]
        async with _client(maker, PRINCIPALS[mode]) as client:
            status, body = await _list(client, user_id=value)

        assert (status, body) == (200, {"users": [], "total": 0})

    async def test_another_letter_case_finds_only_the_account_stored_that_way(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            _, body = await _list(client, user_id=world.lower)

        assert [row["id"] for row in body["users"]] == [world.lower]
        assert body["total"] == 1

    @pytest.mark.parametrize(
        ("extra", "found"),
        [
            ({"role": "user"}, True),
            ({"role": "admin"}, False),
            ({"admitted_via": "allowlist"}, True),
            ({"admitted_via": "beta_invite"}, False),
            ({"search": "lookup.example"}, True),
            ({"search": "no-such-text"}, False),
            ({"plan": "pro"}, True),
            ({"plan": "free"}, False),
        ],
    )
    async def test_it_combines_with_the_other_filters(
        self, world, maker, mode, extra, found
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            status, body = await _list(client, user_id=world.target, **extra)

        assert status == 200
        assert [row["id"] for row in body["users"]] == ([world.target] if found else [])
        assert body["total"] == (1 if found else 0)

    async def test_it_combines_with_the_workspace_filter(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            _, member = await _list(client, user_id=world.target, workspace_id=str(world.live_id))
            _, other = await _list(client, user_id=world.longer, workspace_id=str(world.live_id))
            # A soft-deleted workspace's membership does not count.
            _, gone = await _list(client, user_id=world.target, workspace_id=str(world.gone_id))

        assert ([row["id"] for row in member["users"]], member["total"]) == ([world.target], 1)
        assert other == {"users": [], "total": 0}
        assert gone == {"users": [], "total": 0}

    async def test_include_workspaces_carries_the_live_workspaces(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            status, body = await _list(client, user_id=world.target, include_workspaces="true")

        assert status == 200 and body["total"] == 1
        [row] = body["users"]
        assert [(w["workspace_id"], w["role"], w["plan_name"]) for w in row["workspaces"]] == [
            (str(world.live_id), "owner", "pro")
        ]

    async def test_without_the_parameter_the_list_is_unfiltered(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            _, body = await _list(client, search=world.tag, limit=500)

        assert sorted(row["id"] for row in body["users"]) == sorted(
            [world.target, world.longer, world.lower]
        )
        assert body["total"] == 3

    @pytest.mark.parametrize("value", ["", "x" * 256])
    async def test_an_empty_or_over_long_user_id_is_a_422(self, world, maker, mode, value) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            response = await client.get("/api/v1/admin/users", params={"user_id": value})

        assert response.status_code == 422


async def _statements(async_engine, call) -> list[str]:
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(async_engine.sync_engine, "before_cursor_execute", _record)
    try:
        await call()
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _record)
    return [statement for statement in seen if statement.upper().startswith("SELECT")]


async def test_an_unknown_id_costs_the_count_and_the_select(world, maker, async_engine) -> None:
    async with _client(maker, PRINCIPALS["service"]) as client:
        statements = await _statements(
            async_engine, lambda: _list(client, user_id=f"nobody-{world.tag}")
        )

    assert len(statements) == 2


async def test_a_known_id_reads_no_context_or_permission_table(world, maker, async_engine) -> None:
    async with _client(maker, PRINCIPALS["service"]) as client:
        plain = await _statements(async_engine, lambda: _list(client, user_id=world.target))
        with_workspaces = await _statements(
            async_engine,
            lambda: _list(client, user_id=world.target, include_workspaces="true"),
        )

    # The count, the row, one memory count, one cap query.
    assert len(plain) == 4
    # include_workspaces adds one query.
    assert len(with_workspaces) == 5
    for statement in with_workspaces:
        assert "FROM contexts" not in statement
        assert "context_members" not in statement
        assert "api_keys" not in statement
