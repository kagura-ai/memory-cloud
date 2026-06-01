"""Integration test: binding introspection is owner-scoped (#629, real Postgres).

The IDOR / cross-user isolation of list_my_bindings / describe_binding rests on
every query ANDing ``APIKey.user_id == caller``. A mocked ``db.execute`` cannot
validate that WHERE clause (it returns a fixed row list regardless of the query),
so this runs the handlers against a real DB with two users' bindings seeded and
asserts each user sees only their own — and that user B describing user A's
``key_id`` / ``context_id`` returns the uniform ``binding_not_found``.

The handlers use ``db.base.get_db`` internally, which the integration conftest
steers at ``TEST_DATABASE_URL``; committed fixture rows are visible to that
session.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.tools.api_keys import handle_describe_binding, handle_list_my_bindings
from models.auth import APIKey, Context, User, Workspace


@pytest_asyncio.fixture(loop_scope="session")
async def two_users_with_bindings(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """User A and User B each own one public-bound API key.

    A's key binds ctx_a; B's key binds ctx_b (both contexts live in A's
    workspace — workspace membership is irrelevant to the owner-scoping under
    test, which keys on user_id). Yields ids for the assertions.
    """
    user_a = f"a_{uuid4().hex[:8]}"
    user_b = f"b_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    ctx_a = uuid4()
    ctx_b = uuid4()

    for uid in (user_a, user_b):
        db_session.add(
            User(
                email=f"{uid}@binding-scope.invalid",
                user_id=uid,
                name="Binding Scope User",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
            )
        )
    # Flush in dependency tiers: APIKey.bound_context_id / Context.workspace_id are
    # plain FK columns (no ORM relationships), so the unit-of-work can't infer the
    # insert order. Flush each tier's parents before inserting children.
    await db_session.flush()  # users
    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"bind-scope-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=user_a,
            daily_api_limit=500,
            weekly_api_limit=2500,
            deleted_at=None,
        )
    )
    await db_session.flush()  # workspace
    db_session.add(Context(id=ctx_a, workspace_id=workspace_id, name="ctx-a", created_by=user_a))
    db_session.add(Context(id=ctx_b, workspace_id=workspace_id, name="ctx-b", created_by=user_b))
    await db_session.flush()  # contexts
    key_a = APIKey(
        user_id=user_a,
        name="a-bound",
        key_hash=f"hash_{uuid4().hex}",
        key_prefix="kagura_pub_a",
        bound_context_id=ctx_a,
        workspace_id=None,
    )
    key_b = APIKey(
        user_id=user_b,
        name="b-bound",
        key_hash=f"hash_{uuid4().hex}",
        key_prefix="kagura_pub_b",
        bound_context_id=ctx_b,
        workspace_id=None,
    )
    db_session.add(key_a)
    db_session.add(key_b)
    await db_session.commit()

    yield SimpleNamespace(
        user_a=user_a,
        user_b=user_b,
        key_a_id=key_a.id,
        key_b_id=key_b.id,
        ctx_a=ctx_a,
        ctx_b=ctx_b,
    )

    await db_session.execute(APIKey.__table__.delete().where(APIKey.user_id.in_([user_a, user_b])))
    await db_session.execute(Context.__table__.delete().where(Context.workspace_id == workspace_id))
    await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
    await db_session.execute(User.__table__.delete().where(User.user_id.in_([user_a, user_b])))
    await db_session.commit()


def _data(result):
    return json.loads(result[0].text)


class TestBindingOwnerScope:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_list_shows_only_own_bindings(self, two_users_with_bindings):
        s = two_users_with_bindings

        a = _data(await handle_list_my_bindings({}, s.user_a, None))
        assert a["count"] == 1
        assert a["bindings"][0]["key_id"] == s.key_a_id

        b = _data(await handle_list_my_bindings({}, s.user_b, None))
        assert b["count"] == 1
        assert b["bindings"][0]["key_id"] == s.key_b_id  # B never sees A's binding

    @pytest.mark.asyncio(loop_scope="session")
    async def test_describe_cross_user_key_id_is_not_found(self, two_users_with_bindings):
        s = two_users_with_bindings
        # User B tries to describe User A's key by its (guessable, sequential) id.
        result = await handle_describe_binding({"key_id": s.key_a_id}, s.user_b, None)
        assert _data(result)["error"] == "binding_not_found"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_describe_cross_user_context_id_is_not_found(self, two_users_with_bindings):
        s = two_users_with_bindings
        # User B tries to describe via A's bound context_id — also owner-scoped.
        result = await handle_describe_binding({"context_id": str(s.ctx_a)}, s.user_b, None)
        assert _data(result)["error"] == "binding_not_found"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_owner_describe_succeeds_with_prefix(self, two_users_with_bindings):
        s = two_users_with_bindings
        result = await handle_describe_binding({"key_id": s.key_a_id}, s.user_a, None)
        data = _data(result)
        assert data["status"] == "success"
        assert data["binding"]["key_id"] == s.key_a_id
        assert data["binding"]["key_prefix"] == "kagura_pub_a"
        assert data["binding"]["context_id"] == str(s.ctx_a)
