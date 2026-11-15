"""``GET /api/v1/admin/contexts/deleted`` against Postgres (#2833).

Over HTTP (the real query parsing), as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds):

- a soft-deleted context of a live workspace is listed; a live context and a
  context of a soft-deleted workspace are not;
- ``workspace_id``, ``created_by`` and ``deleted_by`` filter, ANDed;
- newest deletion first, then ``id``; ``total`` counts the filtered rows and
  paging returns every row once;
- ``deleted_memories`` is the restore dry run's
  ``memories_restored + memories_left_deleted``, 0 once the tombstone sweep
  removed the rows;
- ``name_taken`` / ``resource_taken`` say what the dry run answers (200 with
  both false, 409 with either true);
- the answer holds the ten named fields and nothing of the context's text,
  of a memory or of a ``users`` row; the read writes nothing.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import memory_cloud_exception_handler
from api.routes import admin as admin_routes
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}
URL = "/api/v1/admin/contexts/deleted"
ITEM_KEYS = {
    "context_id",
    "name",
    "workspace_id",
    "workspace_name",
    "created_by",
    "deleted_at",
    "deleted_by",
    "deleted_memories",
    "name_taken",
    "resource_taken",
}
CAPACITY_PURGE = "system:capacity-purge"


def _sentinel(label: str) -> str:
    return f"SENTINEL-{label}-{secrets.token_hex(8)}"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: principal
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """One live workspace with deleted contexts of every shape, one deleted workspace."""
    tag = uuid4().hex[:8]
    creator, deleter, owner = f"creator-{tag}", f"deleter-{tag}", f"owner-{tag}"
    sentinels = {
        key: _sentinel(key)
        for key in (
            "display_name",
            "description",
            "summary",
            "usage_guide",
            "resource_id",
            "memory_content",
            "memory_summary",
            "creator_email",
            "deleter_email",
            "owner_email",
            "creator_name",
        )
    }
    for user_id, email, name in (
        (creator, f"{sentinels['creator_email']}@people.example", sentinels["creator_name"]),
        (deleter, f"{sentinels['deleter_email']}@people.example", "Deleter"),
        (owner, f"{sentinels['owner_email']}@people.example", "Owner"),
    ):
        db_session.add(User(email=email.lower(), user_id=user_id, name=name, role="user"))

    live_ws = Workspace(id=uuid4(), name=f"live-{tag}", owner_user_id=owner)
    other_ws = Workspace(id=uuid4(), name=f"other-{tag}", owner_user_id=owner)
    dead_ws = Workspace(id=uuid4(), name=f"dead-{tag}", owner_user_id=owner, deleted_at=utcnow())
    db_session.add_all([live_ws, other_ws, dead_ws])
    await db_session.flush()
    for ws in (live_ws, other_ws, dead_ws):
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role="owner"))

    now = utcnow().replace(microsecond=0)
    contexts: dict[str, Context] = {}

    def context(key: str, workspace: Workspace, **cols) -> Context:
        row = Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=cols.pop("name", f"{key.replace('_', '-')}-{tag}"),
            created_by=cols.pop("created_by", creator),
            is_private=False,
            **cols,
        )
        contexts[key] = row
        db_session.add(row)
        return row

    # Deleted three hours ago, with every text column set and three tombstones.
    full = context(
        "full",
        live_ws,
        display_name=sentinels["display_name"],
        description=sentinels["description"],
        summary=sentinels["summary"],
        usage_guide=sentinels["usage_guide"],
        resource_id=sentinels["resource_id"],
        deleted_at=now - timedelta(hours=3),
        deleted_by=deleter,
    )
    # The tombstone sweep removed its memory rows: listed with a count of 0.
    context("swept", live_ws, deleted_at=now - timedelta(hours=2), deleted_by=deleter)
    # A live context of the workspace took the name.
    context(
        "name_taken",
        live_ws,
        name=f"shared-name-{tag}",
        deleted_at=now - timedelta(hours=1),
        deleted_by=deleter,
    )
    context("name_holder", live_ws, name=f"shared-name-{tag}")
    # A live context (of another workspace) serves the resource.
    context(
        "resource_taken",
        live_ws,
        resource_id=f"resource-{tag}",
        deleted_at=now - timedelta(minutes=30),
        deleted_by=CAPACITY_PURGE,
        created_by=None,
    )
    context("resource_holder", other_ws, resource_id=f"resource-{tag}")
    # Two contexts deleted at the same instant: the tie is broken by id.
    context("tie_a", live_ws, deleted_at=now - timedelta(hours=5), deleted_by=None)
    context("tie_b", live_ws, deleted_at=now - timedelta(hours=5), deleted_by=None)
    context("live", live_ws)
    context("in_other", other_ws, deleted_at=now - timedelta(minutes=5), deleted_by=deleter)
    context("in_dead", dead_ws, deleted_at=now - timedelta(minutes=1), deleted_by=deleter)
    await db_session.flush()

    def memory(target: Context, **cols) -> None:
        db_session.add(
            Memory(
                id=uuid4(),
                user_id=creator,
                workspace_id=target.workspace_id,
                context_id=target.id,
                summary=cols.pop("summary", f"{sentinels['memory_summary']} summary"),
                content=cols.pop("content", sentinels["memory_content"]),
                type="note",
                client="pytest",
                embedding_status="success",
                **cols,
            )
        )

    # Two rows the deletion tombstoned, one forgotten long before it.
    memory(full, deleted_at=full.deleted_at, deleted_by=deleter)
    memory(full, deleted_at=full.deleted_at, deleted_by=deleter)
    memory(full, deleted_at=full.deleted_at - timedelta(days=2), deleted_by=creator)
    memory(contexts["name_taken"], deleted_at=contexts["name_taken"].deleted_at, deleted_by=deleter)
    # A live memory is not a tombstone.
    memory(contexts["live"])
    await db_session.commit()

    ids = {key: row.id for key, row in contexts.items()}
    workspace_ids = [live_ws.id, other_ws.id, dead_ws.id]
    yield SimpleNamespace(
        tag=tag,
        ids=ids,
        live_ws=live_ws.id,
        live_ws_name=live_ws.name,
        other_ws=other_ws.id,
        dead_ws=dead_ws.id,
        creator=creator,
        deleter=deleter,
        owner=owner,
        sentinels=sentinels,
        deleted_at={key: row.deleted_at for key, row in contexts.items()},
    )

    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.workspace_id.in_(workspace_ids)))
    await db_session.execute(delete(Context).where(Context.workspace_id.in_(workspace_ids)))
    await db_session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspace_ids))
    )
    await db_session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
    await db_session.execute(delete(User).where(User.user_id.in_([creator, deleter, owner])))
    await db_session.commit()


async def _list(client: httpx.AsyncClient, **params) -> dict:
    response = await client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict) -> list[str]:
    return [item["context_id"] for item in body["items"]]


def _expected(world, *keys: str) -> list[str]:
    return [str(world.ids[key]) for key in keys]


def _tie_order(world) -> list[str]:
    """The two contexts deleted at the same instant, in ``id`` order."""
    return sorted(["tie_a", "tie_b"], key=lambda key: world.ids[key])


@pytest.mark.parametrize("mode", list(PRINCIPALS))
class TestDeletedContexts:
    async def test_deleted_contexts_of_a_live_workspace_are_listed_newest_first(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws))

        assert _ids(body) == _expected(
            world, "resource_taken", "name_taken", "swept", "full", *_tie_order(world)
        )
        assert (body["total"], body["limit"], body["offset"]) == (6, 50, 0)
        assert set(body) == {"items", "total", "limit", "offset"}

    async def test_a_live_context_and_a_context_of_a_deleted_workspace_are_left_out(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            everything = await _list(client, limit=500)
            dead = await _list(client, workspace_id=str(world.dead_ws))

        listed = set(_ids(everything))
        assert set(_expected(world, "full", "swept", "name_taken", "in_other")) <= listed
        for key in ("live", "name_holder", "resource_holder", "in_dead"):
            assert str(world.ids[key]) not in listed
        assert dead == {"items": [], "total": 0, "limit": 50, "offset": 0}

    async def test_a_row_carries_the_named_fields(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws), deleted_by=world.deleter)

        rows = {item["context_id"]: item for item in body["items"]}
        full = rows[str(world.ids["full"])]
        assert full == {
            "context_id": str(world.ids["full"]),
            "name": f"full-{world.tag}",
            "workspace_id": str(world.live_ws),
            "workspace_name": world.live_ws_name,
            "created_by": world.creator,
            "deleted_at": world.deleted_at["full"].isoformat() + "Z",
            "deleted_by": world.deleter,
            "deleted_memories": 3,
            "name_taken": False,
            "resource_taken": False,
        }
        for item in body["items"]:
            assert set(item) == ITEM_KEYS

    async def test_created_by_and_deleted_by_are_answered_as_stored_or_null(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws))

        rows = {item["context_id"]: item for item in body["items"]}
        purged = rows[str(world.ids["resource_taken"])]
        assert (purged["created_by"], purged["deleted_by"]) == (None, CAPACITY_PURGE)
        assert rows[str(world.ids["tie_a"])]["deleted_by"] is None

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({"deleted_by": "deleter"}, ["name_taken", "swept", "full"]),
            ({"deleted_by": CAPACITY_PURGE}, ["resource_taken"]),
            ({"created_by": "creator"}, ["name_taken", "swept", "full", "TIES"]),
            ({"created_by": "creator", "deleted_by": "deleter"}, ["name_taken", "swept", "full"]),
            ({"created_by": "deleter"}, []),
            ({"created_by": "creator", "deleted_by": CAPACITY_PURGE}, []),
        ],
    )
    async def test_the_filters_are_anded(self, world, maker, mode, filters, expected) -> None:
        params = {
            key: getattr(world, value) if value in ("creator", "deleter") else value
            for key, value in filters.items()
        }
        keys = [
            key for name in expected for key in (_tie_order(world) if name == "TIES" else [name])
        ]
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws), **params)

        assert _ids(body) == _expected(world, *keys)
        assert body["total"] == len(keys)

    async def test_the_workspace_filter_alone_and_a_filter_without_it(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            other = await _list(client, workspace_id=str(world.other_ws))
            by_deleter = await _list(client, deleted_by=world.deleter)
            unknown = await _list(client, workspace_id=str(uuid4()))

        assert _ids(other) == _expected(world, "in_other")
        # Across workspaces, without the context of the deleted workspace.
        assert _ids(by_deleter) == _expected(world, "in_other", "name_taken", "swept", "full")
        assert by_deleter["total"] == 4
        assert unknown == {"items": [], "total": 0, "limit": 50, "offset": 0}

    async def test_a_filter_is_an_exact_match(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            prefix = await _list(client, deleted_by=world.deleter[:-1])
            upper = await _list(client, created_by=world.creator.upper())
            pattern = await _list(client, deleted_by="%")

        assert prefix["total"] == upper["total"] == pattern["total"] == 0

    async def test_paging_returns_every_row_once_in_order(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            whole = await _list(client, workspace_id=str(world.live_ws))
            pages = [
                await _list(client, workspace_id=str(world.live_ws), limit=4, offset=offset)
                for offset in (0, 4, 8)
            ]

        assert [len(page["items"]) for page in pages] == [4, 2, 0]
        assert [page["total"] for page in pages] == [6, 6, 6]
        assert [(page["limit"], page["offset"]) for page in pages] == [(4, 0), (4, 4), (4, 8)]
        assert [cid for page in pages for cid in _ids(page)] == _ids(whole)
        # The tie on deleted_at is broken by id on every read.
        assert _ids(whole)[-2:] == _expected(world, *_tie_order(world))

    @pytest.mark.parametrize(
        "params",
        [
            {"workspace_id": "not-a-uuid"},
            {"limit": 0},
            {"limit": 501},
            {"offset": -1},
            {"created_by": ""},
            {"deleted_by": "x" * 256},
        ],
    )
    async def test_a_bad_value_is_a_422(self, world, maker, mode, params) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            response = await client.get(URL, params=params)

        assert response.status_code == 422

    async def test_limit_500_is_accepted(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws), limit=500)

        assert (body["limit"], body["total"]) == (500, 6)

    async def test_deleted_memories_equals_the_dry_runs_two_counts(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws))
            rows = {item["context_id"]: item for item in body["items"]}
            for key in ("full", "swept", "tie_a"):
                dry_run = await client.post(
                    f"/api/v1/admin/contexts/{world.ids[key]}/restore", json={"dry_run": True}
                )
                assert dry_run.status_code == 200, dry_run.text
                plan = dry_run.json()
                assert (
                    rows[str(world.ids[key])]["deleted_memories"]
                    == plan["memories_restored"] + plan["memories_left_deleted"]
                ), key

        # Two tombstoned by the deletion, one forgotten before it.
        assert rows[str(world.ids["full"])]["deleted_memories"] == 3

    async def test_a_context_whose_memories_were_swept_is_listed_with_0(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws))

        rows = {item["context_id"]: item for item in body["items"]}
        assert rows[str(world.ids["swept"])]["deleted_memories"] == 0

    async def test_the_two_flags_say_what_the_dry_run_answers(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live_ws))
            flags = {
                item["context_id"]: (item["name_taken"], item["resource_taken"])
                for item in body["items"]
            }
            statuses = {}
            for context_id in flags:
                dry_run = await client.post(
                    f"/api/v1/admin/contexts/{context_id}/restore", json={"dry_run": True}
                )
                statuses[context_id] = dry_run.status_code

        assert flags[str(world.ids["name_taken"])] == (True, False)
        assert flags[str(world.ids["resource_taken"])] == (False, True)
        assert flags[str(world.ids["full"])] == (False, False)
        for context_id, (name_taken, resource_taken) in flags.items():
            expected = 409 if (name_taken or resource_taken) else 200
            assert statuses[context_id] == expected, context_id

    async def test_no_text_of_the_context_a_memory_or_a_user_is_answered(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            responses = [
                await client.get(URL, params={"limit": 500}),
                await client.get(URL, params={"workspace_id": str(world.live_ws)}),
                await client.get(URL, params={"created_by": world.creator}),
            ]

        for response in responses:
            assert response.status_code == 200
            raw = response.text
            assert str(world.ids["full"]) in raw
            for label, sentinel in world.sentinels.items():
                assert sentinel not in raw, label
                assert sentinel.lower() not in raw.lower(), label
            assert "@people" not in raw
            for item in response.json()["items"]:
                assert set(item) == ITEM_KEYS


async def _counts(db_session: AsyncSession, workspace_id: UUID) -> tuple[int, int, int]:
    await db_session.rollback()
    audits = await db_session.scalar(select(func.count()).select_from(AuditLog))
    live = await db_session.scalar(
        select(func.count())
        .select_from(Context)
        .where(Context.workspace_id == workspace_id, Context.deleted_at.is_(None))
    )
    tombstones = await db_session.scalar(
        select(func.count())
        .select_from(Memory)
        .where(Memory.workspace_id == workspace_id, Memory.deleted_at.is_not(None))
    )
    return audits, live, tombstones


@pytest.mark.parametrize("mode", list(PRINCIPALS))
async def test_the_read_writes_no_audit_row_and_changes_no_row(
    world, maker, db_session, mode
) -> None:
    before = await _counts(db_session, world.live_ws)
    async with _client(maker, PRINCIPALS[mode]) as client:
        await _list(client, workspace_id=str(world.live_ws))
        await _list(client, limit=500)
    assert await _counts(db_session, world.live_ws) == before


@pytest_asyncio.fixture(loop_scope="session")
async def lookalikes(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """Deleted contexts beside rows that look like a refusal of the restore and are none.

    - ``twin_a`` / ``twin_b``: two deleted contexts with one name and one
      ``resource_id``; a deleted context takes neither.
    - ``elsewhere``: its name is held by a live context of another workspace.
    - ``blank``: an empty ``resource_id``, which a live context has too; the
      restore reads an empty value as no resource.
    - ``twin_a`` holds one tombstone and one live memory.
    """
    tag = uuid4().hex[:8]
    owner = f"owner-{tag}"
    home = Workspace(id=uuid4(), name=f"home-{tag}", owner_user_id=owner)
    away = Workspace(id=uuid4(), name=f"away-{tag}", owner_user_id=owner)
    db_session.add_all([home, away])
    await db_session.flush()
    now = utcnow().replace(microsecond=0)

    def context(workspace: Workspace, name: str, *, hours: int | None = None, **cols) -> Context:
        row = Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=name,
            created_by=owner,
            is_private=False,
            deleted_at=now - timedelta(hours=hours) if hours is not None else None,
            deleted_by=owner if hours is not None else None,
            **cols,
        )
        db_session.add(row)
        return row

    rows = {
        "twin_a": context(home, f"twin-{tag}", hours=1, resource_id=f"twin-resource-{tag}"),
        "twin_b": context(home, f"twin-{tag}", hours=2, resource_id=f"twin-resource-{tag}"),
        "elsewhere": context(home, f"elsewhere-{tag}", hours=3),
        "blank": context(home, f"blank-{tag}", hours=4, resource_id=""),
    }
    context(away, f"elsewhere-{tag}")
    context(away, f"blank-holder-{tag}", resource_id="")
    await db_session.flush()
    for deleted in (True, False):
        db_session.add(
            Memory(
                id=uuid4(),
                user_id=owner,
                workspace_id=home.id,
                context_id=rows["twin_a"].id,
                summary="summary",
                content="content",
                type="note",
                client="pytest",
                embedding_status="success",
                deleted_at=rows["twin_a"].deleted_at if deleted else None,
                deleted_by=owner if deleted else None,
            )
        )
    await db_session.commit()
    ids = {key: row.id for key, row in rows.items()}
    workspace_ids = [home.id, away.id]

    yield SimpleNamespace(home=home.id, ids=ids)

    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.workspace_id.in_(workspace_ids)))
    await db_session.execute(delete(Context).where(Context.workspace_id.in_(workspace_ids)))
    await db_session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
    await db_session.commit()


async def test_rows_that_only_look_like_a_refusal_set_no_flag_and_add_no_memory(
    lookalikes, maker
) -> None:
    async with _client(maker, PRINCIPALS["service"]) as client:
        body = await _list(client, workspace_id=str(lookalikes.home))
        rows = {item["context_id"]: item for item in body["items"]}
        plans = {}
        for key, context_id in lookalikes.ids.items():
            dry_run = await client.post(
                f"/api/v1/admin/contexts/{context_id}/restore", json={"dry_run": True}
            )
            assert dry_run.status_code == 200, (key, dry_run.text)
            plans[key] = dry_run.json()

    assert body["total"] == 4
    for key, context_id in lookalikes.ids.items():
        row = rows[str(context_id)]
        assert (row["name_taken"], row["resource_taken"]) == (False, False), key
        assert row["deleted_memories"] == (
            plans[key]["memories_restored"] + plans[key]["memories_left_deleted"]
        ), key
    # The tombstone, not the live memory beside it.
    assert rows[str(lookalikes.ids["twin_a"])]["deleted_memories"] == 1
