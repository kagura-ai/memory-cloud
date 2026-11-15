"""The paged admin workspace list and the one-workspace read on real rows (#2841).

Other tests leave workspaces in the session's database, so every test but the
first scopes its rows with a unique ``owner_user_id``; the first one seeds rows
older than anything else can be.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace
from models.memory import Memory

from ._admin_helpers import make_context, make_user
from ._admin_http import admin_http

pytestmark = pytest.mark.asyncio

LIST = "/api/v1/admin/plans/workspaces"
ROW_KEYS = {
    "id",
    "name",
    "plan_name",
    "owner_user_id",
    "owner_name",
    "owner_email",
    "total_memories",
    "memory_limit",
    "mcp_calls_per_day",
    "mcp_calls_per_week",
    "entitlement_source",
}


async def _owner(db_session: AsyncSession) -> User:
    user = make_user(name="List Owner")
    db_session.add(user)
    await db_session.flush()
    return user


def _workspace(
    owner: User | str,
    name: str,
    *,
    created_at: datetime | None = None,
    plan_name: str = "pro",
    deleted: bool = False,
    description: str | None = None,
) -> Workspace:
    workspace = Workspace(
        id=uuid4(),
        name=name,
        description=description,
        plan_name=plan_name,
        owner_user_id=owner if isinstance(owner, str) else owner.user_id,
        daily_api_limit=500,
        weekly_api_limit=2500,
        deleted_at=func.now() if deleted else None,
    )
    if created_at is not None:
        workspace.created_at = created_at
    return workspace


async def test_without_parameters_answers_the_first_50_live_workspaces_oldest_first(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    start = datetime(2001, 1, 1)
    live = [
        _workspace(owner, f"old-{index:02d}", created_at=start + timedelta(minutes=index))
        for index in range(55)
    ]
    gone = _workspace(owner, "old-gone", created_at=start - timedelta(days=1), deleted=True)
    db_session.add_all([*live, gone])
    await db_session.commit()
    live_total = (
        await db_session.execute(
            select(func.count()).select_from(Workspace).where(Workspace.deleted_at.is_(None))
        )
    ).scalar_one()

    async with admin_http(db_session) as client:
        response = await client.get(LIST)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert (body["limit"], body["offset"]) == (50, 0)
    assert body["total"] == live_total >= 55
    assert [item["id"] for item in body["items"]] == [str(w.id) for w in live[:50]]
    assert set(body["items"][0]) == ROW_KEYS
    assert str(gone.id) not in response.text


async def test_walking_all_pages_returns_every_workspace_once_when_created_at_is_shared(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    same = datetime(2020, 5, 5, 12, 0, 0)
    rows = [_workspace(owner, f"tie-{index}", created_at=same) for index in range(5)]
    rows += [
        _workspace(owner, "before", created_at=same - timedelta(hours=1)),
        _workspace(owner, "after", created_at=same + timedelta(hours=1)),
    ]
    gone = _workspace(owner, "tie-gone", created_at=same, deleted=True)
    db_session.add_all([*rows, gone])
    await db_session.commit()

    seen: list[str] = []
    async with admin_http(db_session) as client:
        for offset in range(0, 10, 2):
            response = await client.get(
                LIST, params={"owner_user_id": owner.user_id, "limit": 2, "offset": offset}
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 7
            assert (body["limit"], body["offset"]) == (2, offset)
            seen += [item["id"] for item in body["items"]]

    assert len(seen) == 7
    assert sorted(seen) == sorted(str(w.id) for w in rows)
    ties = sorted(str(w.id) for w in rows[:5])
    # created_at ascending, then id: the ties come back in id order (UUID order
    # in PostgreSQL is the order of the canonical text form).
    assert seen == [str(rows[5].id), *ties, str(rows[6].id)]


async def test_search_matches_wildcards_literally_and_ignores_case(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    names = ["Sale 50% Off", "Sale 500 Off", "snake_case", "snakeXcase", "back\\slash"]
    db_session.add_all([_workspace(owner, name) for name in names])
    await db_session.commit()

    async def found(search: str) -> tuple[list[str], int]:
        response = await client.get(LIST, params={"owner_user_id": owner.user_id, "search": search})
        assert response.status_code == 200, response.text
        body = response.json()
        return sorted(item["name"] for item in body["items"]), body["total"]

    async with admin_http(db_session) as client:
        assert await found("50%") == (["Sale 50% Off"], 1)
        assert await found("_") == (["snake_case"], 1)
        assert await found("\\") == (["back\\slash"], 1)
        assert await found("sALE") == (["Sale 50% Off", "Sale 500 Off"], 2)
        assert await found("  snake  ") == (["snakeXcase", "snake_case"], 2)
        # Empty after the trim: no filter.
        assert (await found("   "))[1] == 5
        assert (await found(""))[1] == 5


async def test_plan_and_owner_filters_are_exact_and_combine_with_search(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    other = await _owner(db_session)
    db_session.add_all(
        [
            _workspace(owner, "alpha one", plan_name="pro"),
            _workspace(owner, "alpha two", plan_name="free"),
            _workspace(owner, "beta three", plan_name="pro"),
            _workspace(owner, "alpha gone", plan_name="pro", deleted=True),
            _workspace(other, "alpha other", plan_name="pro"),
            # An owner id that only contains the filter value must not match.
            _workspace(f"{owner.user_id}x", "alpha suffix", plan_name="pro"),
        ]
    )
    await db_session.commit()

    async def found(**params: str) -> tuple[list[str], int]:
        response = await client.get(LIST, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        return sorted(item["name"] for item in body["items"]), body["total"]

    async with admin_http(db_session) as client:
        assert await found(owner_user_id=owner.user_id) == (
            ["alpha one", "alpha two", "beta three"],
            3,
        )
        assert await found(owner_user_id=owner.user_id, plan="pro") == (
            ["alpha one", "beta three"],
            2,
        )
        assert await found(owner_user_id=owner.user_id, plan="pro", search="ALPHA") == (
            ["alpha one"],
            1,
        )
        assert await found(owner_user_id=owner.user_id, plan="promax") == ([], 0)
        # total counts the filtered rows, not the page.
        response = await client.get(LIST, params={"owner_user_id": owner.user_id, "limit": 1})
        assert len(response.json()["items"]) == 1
        assert response.json()["total"] == 3


async def test_page_lookups_bind_at_most_limit_ids(db_session: AsyncSession) -> None:
    owners = [await _owner(db_session) for _ in range(5)]
    token = uuid4().hex
    db_session.add_all(
        [_workspace(owner, f"{token}-{index}") for index, owner in enumerate(owners)]
    )
    await db_session.commit()

    statements: list[tuple[str, int]] = []

    def _record(_conn, _cursor, statement, parameters, _context, _executemany) -> None:
        statements.append((statement, len(parameters or ())))

    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", _record)
    try:
        async with admin_http(db_session) as client:
            response = await client.get(LIST, params={"search": token, "limit": 2})
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2
    assert response.json()["total"] == 5
    owner_lookups = [count for sql, count in statements if "FROM users" in sql]
    memory_counts = [count for sql, count in statements if "FROM memories" in sql]
    assert owner_lookups and memory_counts
    assert max(owner_lookups) <= 2
    assert max(memory_counts) <= 2


async def test_one_owner_of_a_whole_page_is_looked_up_once(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    db_session.add_all([_workspace(owner, f"same-owner-{index}") for index in range(4)])
    await db_session.commit()

    statements: list[tuple[str, int]] = []

    def _record(_conn, _cursor, statement, parameters, _context, _executemany) -> None:
        statements.append((statement, len(parameters or ())))

    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", _record)
    try:
        async with admin_http(db_session) as client:
            response = await client.get(LIST, params={"owner_user_id": owner.user_id})
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    items = response.json()["items"]
    assert len(items) == 4
    assert {item["owner_email"] for item in items} == {owner.email}
    assert [count for sql, count in statements if "FROM users" in sql] == [1]


async def test_one_workspace_read_answers_the_list_item(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    workspace = _workspace(owner, "single read", plan_name="basic")
    db_session.add(workspace)
    await db_session.flush()
    context = make_context(workspace_id=workspace.id, created_by=owner.user_id)
    db_session.add(context)
    await db_session.flush()
    db_session.add(
        Memory(
            workspace_id=workspace.id,
            context_id=context.id,
            user_id=owner.user_id,
            summary="one",
            content="one",
            type="note",
            client="pytest",
            embedding_status="success",
        )
    )
    await db_session.commit()

    async with admin_http(db_session) as client:
        listing = await client.get(LIST, params={"owner_user_id": owner.user_id})
        single = await client.get(f"{LIST}/{workspace.id}")

    assert single.status_code == 200, single.text
    (item,) = listing.json()["items"]
    assert single.json() == item
    assert set(item) == ROW_KEYS
    assert item["owner_name"] == "List Owner"
    assert item["owner_email"] == owner.email
    assert item["plan_name"] == "basic"
    assert item["entitlement_source"] == "admin_grant"
    assert item["total_memories"] == 1


async def test_one_workspace_read_is_404_for_unknown_and_soft_deleted(
    db_session: AsyncSession,
) -> None:
    owner = await _owner(db_session)
    gone = _workspace(owner, "gone", deleted=True)
    db_session.add(gone)
    await db_session.commit()
    # The 404 rolls the session back, which expires its rows: read the ids first.
    gone_id, owner_id = gone.id, owner.user_id

    async with admin_http(db_session) as client:
        for workspace_id in (uuid4(), gone_id):
            response = await client.get(f"{LIST}/{workspace_id}")
            assert response.status_code == 404, response.text
            assert response.json()["message"] == "Workspace not found"
        listing = await client.get(LIST, params={"owner_user_id": owner_id})

    assert listing.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_description_is_in_neither_answer(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    sentinel = f"description-sentinel-{uuid4().hex}"
    workspace = _workspace(owner, "described", description=sentinel)
    db_session.add(workspace)
    await db_session.commit()

    async with admin_http(db_session) as client:
        listing = await client.get(LIST, params={"owner_user_id": owner.user_id})
        single = await client.get(f"{LIST}/{workspace.id}")

    for response in (listing, single):
        assert response.status_code == 200
        assert str(workspace.id) in response.text
        assert sentinel not in response.text
        assert "description" not in response.text


async def test_search_text_is_not_logged_and_no_row_is_written(db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    sentinel = f"search-sentinel-{uuid4().hex}"
    db_session.add(_workspace(owner, f"named {sentinel}"))
    await db_session.commit()

    with patch("api.routes.admin_plans.logger") as logger:
        async with admin_http(db_session) as client:
            response = await client.get(LIST, params={"search": sentinel})

    assert response.json()["total"] == 1
    calls = [*logger.info.call_args_list, *logger.warning.call_args_list]
    assert calls
    assert sentinel not in repr(calls)
    (call,) = logger.info.call_args_list
    assert call.kwargs["search_given"] is True
    assert call.kwargs["search_length"] == len(sentinel)
    # A read: the session holds nothing to write.
    assert not db_session.new and not db_session.dirty and not db_session.deleted
