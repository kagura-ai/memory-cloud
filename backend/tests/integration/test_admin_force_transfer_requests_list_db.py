"""``GET /api/v1/admin/force-transfer-requests`` against Postgres (#2846).

Over HTTP (the real query parsing), as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds):

- a request in each of the four states is listed with the eleven fields of
  the read by id, and the item equals what the read by id answers;
- ``status`` and ``workspace_id`` filter alone and together; ``total`` counts
  the matches, and for ``status=pending`` it is the count the operations
  backlog ``force_transfer_requests_pending`` reads from the same rows;
- a pending request whose workspace is soft-deleted, or has no row, is listed;
- newest first, ties broken by ``id``; paging returns every row exactly once;
- no ``decided_by_email`` and nothing looked up for the workspace, its owner
  or the target user; the read writes nothing.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import admin as admin_routes
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.auth import (
    AuditLog,
    User,
    Workspace,
    WorkspaceMember,
)
from models.auth import (
    WorkspaceOwnershipForceTransferRequest as Request,
)
from services.operations.backlogs import _force_transfer_requests
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}
URL = "/api/v1/admin/force-transfer-requests"
ITEM_KEYS = {
    "request_id",
    "workspace_id",
    "target_user_id",
    "reason",
    "status",
    "initiated_by_user_id",
    "initiated_by_email",
    "ownership_epoch_at_initiation",
    "created_at",
    "decided_by_user_id",
    "decided_at",
}
T0 = datetime(2026, 11, 10, 9, 0, 0)


def _sentinel(label: str) -> str:
    return f"SENTINEL-{label}-{secrets.token_hex(8)}"


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
    """Requests in every state over a live, a soft-deleted and a missing workspace."""
    tag = uuid4().hex[:8]
    owner, target, initiator = f"owner-{tag}", f"target-{tag}", f"initiator-{tag}"
    sentinels = {
        key: _sentinel(key)
        for key in (
            "decided_by_email",
            "workspace_name",
            "target_email",
            "owner_email",
            "target_name",
        )
    }
    db_session.add_all(
        [
            User(
                email=f"{sentinels['owner_email']}@people.example".lower(),
                user_id=owner,
                name="Owner",
                role="user",
            ),
            User(
                email=f"{sentinels['target_email']}@people.example".lower(),
                user_id=target,
                name=sentinels["target_name"],
                role="user",
            ),
        ]
    )
    live = Workspace(id=uuid4(), name=sentinels["workspace_name"], owner_user_id=owner)
    dead = Workspace(
        id=uuid4(),
        name=f"dead-{sentinels['workspace_name']}",
        owner_user_id=owner,
        deleted_at=utcnow(),
    )
    db_session.add_all([live, dead])
    await db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=live.id, user_id=owner, role="owner"))
    missing_workspace = uuid4()
    requests: dict[str, Request] = {}

    def request(key: str, workspace_id: UUID, status: str, minutes: int, **cols) -> None:
        decided = status != "pending"
        row = Request(
            id=uuid4(),
            workspace_id=workspace_id,
            target_user_id=target,
            reason=f"Reason for {key} {tag}",
            ownership_epoch_at_initiation=cols.pop("epoch", 1),
            initiated_by_user_id=cols.pop("initiated_by_user_id", initiator),
            initiated_by_email=cols.pop("initiated_by_email", f"{initiator}@admins.example"),
            status=status,
            decided_by_user_id=f"decider-{tag}" if decided else None,
            decided_by_email=(
                f"{sentinels['decided_by_email']}@admins.example".lower() if decided else None
            ),
            decided_at=T0 + timedelta(minutes=minutes + 1) if decided else None,
            created_at=T0 + timedelta(minutes=minutes),
        )
        requests[key] = row
        db_session.add(row)

    request("superseded", live.id, "superseded", 0)
    request("cancelled", live.id, "cancelled", 10)
    request("approved", live.id, "approved", 20, epoch=7)
    request(
        "pending",
        live.id,
        "pending",
        30,
        initiated_by_user_id="operator:ann@example.com",
        initiated_by_email="operator:ann@example.com",
    )
    # A pending request outlives its workspace: nothing can close it.
    request("pending_dead", dead.id, "pending", 40)
    request("pending_missing", missing_workspace, "pending", 50)
    # Two requests filed at the same instant: the tie is broken by id.
    request("tie_a", dead.id, "cancelled", 5)
    request("tie_b", dead.id, "superseded", 5)
    await db_session.commit()

    ids = {key: row.id for key, row in requests.items()}
    live_id, dead_id = live.id, dead.id
    yield SimpleNamespace(
        tag=tag,
        ids=ids,
        live=live_id,
        dead=dead_id,
        missing=missing_workspace,
        owner=owner,
        target=target,
        initiator=initiator,
        sentinels=sentinels,
    )

    await db_session.rollback()
    await db_session.execute(delete(Request).where(Request.id.in_(list(ids.values()))))
    await db_session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_([live_id, dead_id]))
    )
    await db_session.execute(delete(Workspace).where(Workspace.id.in_([live_id, dead_id])))
    await db_session.execute(delete(User).where(User.user_id.in_([owner, target])))
    await db_session.commit()


async def _list(client: httpx.AsyncClient, **params) -> dict:
    response = await client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _mine(world, body: dict) -> list[str]:
    """This test's requests, in the order answered (other tests may have left rows)."""
    mine = {str(request_id): key for key, request_id in world.ids.items()}
    return [mine[item["request_id"]] for item in body["items"] if item["request_id"] in mine]


def _tie_order(world) -> list[str]:
    return sorted(["tie_a", "tie_b"], key=lambda key: world.ids[key])


@pytest.mark.parametrize("mode", list(PRINCIPALS))
class TestForceTransferRequests:
    async def test_a_request_in_each_state_is_listed_newest_first(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, limit=500)

        assert _mine(world, body) == [
            "pending_missing",
            "pending_dead",
            "pending",
            "approved",
            "cancelled",
            *_tie_order(world),
            "superseded",
        ]
        assert set(body) == {"items", "total", "limit", "offset"}
        assert body["total"] == len(body["items"])
        assert (body["limit"], body["offset"]) == (500, 0)
        assert {item["status"] for item in body["items"]} >= {
            "pending",
            "approved",
            "cancelled",
            "superseded",
        }

    async def test_an_item_carries_the_eleven_fields(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, workspace_id=str(world.live))

        rows = {item["request_id"]: item for item in body["items"]}
        assert rows[str(world.ids["approved"])] == {
            "request_id": str(world.ids["approved"]),
            "workspace_id": str(world.live),
            "target_user_id": world.target,
            "reason": f"Reason for approved {world.tag}",
            "status": "approved",
            "initiated_by_user_id": world.initiator,
            "initiated_by_email": f"{world.initiator}@admins.example",
            "ownership_epoch_at_initiation": 7,
            "created_at": "2026-11-10T09:20:00Z",
            "decided_by_user_id": f"decider-{world.tag}",
            "decided_at": "2026-11-10T09:21:00Z",
        }
        pending = rows[str(world.ids["pending"])]
        assert (pending["decided_by_user_id"], pending["decided_at"]) == (None, None)
        # The initiator is answered as stored: ``operator:<address>`` in service mode.
        assert pending["initiated_by_email"] == "operator:ann@example.com"
        for item in body["items"]:
            assert set(item) == ITEM_KEYS

    async def test_an_item_equals_the_read_by_id(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, limit=500)
            mine = {str(request_id) for request_id in world.ids.values()}
            items = [item for item in body["items"] if item["request_id"] in mine]
            assert len(items) == len(mine)
            for item in items:
                by_id = await client.get(f"{URL}/{item['request_id']}")
                assert by_id.status_code == 200, by_id.text
                assert item == by_id.json()
                assert set(by_id.json()) == ITEM_KEYS

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({"status": "pending"}, ["pending_missing", "pending_dead", "pending"]),
            ({"status": "approved"}, ["approved"]),
            ({"status": "cancelled"}, ["cancelled", "tie_a"]),
            ({"status": "superseded"}, ["tie_b", "superseded"]),
            ({"workspace_id": "live"}, ["pending", "approved", "cancelled", "superseded"]),
            ({"workspace_id": "dead"}, ["pending_dead", "TIES"]),
            ({"workspace_id": "missing"}, ["pending_missing"]),
            ({"status": "pending", "workspace_id": "live"}, ["pending"]),
            ({"status": "pending", "workspace_id": "dead"}, ["pending_dead"]),
            ({"status": "cancelled", "workspace_id": "dead"}, ["tie_a"]),
            ({"status": "approved", "workspace_id": "dead"}, []),
        ],
    )
    async def test_status_and_workspace_filter_alone_and_together(
        self, world, maker, mode, filters, expected
    ) -> None:
        params = dict(filters)
        if "workspace_id" in params:
            params["workspace_id"] = str(getattr(world, params["workspace_id"]))
        keys = [
            key for name in expected for key in (_tie_order(world) if name == "TIES" else [name])
        ]
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, limit=500, **params)

        assert _mine(world, body) == keys
        if "workspace_id" in params:
            # A workspace of this test: every match is one of its rows.
            assert body["total"] == len(keys) == len(body["items"])
        else:
            assert body["total"] == len(body["items"]) >= len(keys)
            assert {item["status"] for item in body["items"]} == {params["status"]}

    async def test_pending_lists_every_pending_request_once_and_matches_the_backlog_count(
        self, world, maker, db_session, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, status="pending", limit=500)

        await db_session.rollback()
        backlog_count, oldest = await _force_transfer_requests(db_session, utcnow())
        stored = set(
            (await db_session.execute(select(Request.id).where(Request.status == "pending")))
            .scalars()
            .all()
        )
        listed = [item["request_id"] for item in body["items"]]

        assert body["total"] == backlog_count == len(stored)
        assert len(listed) == len(set(listed))
        assert set(listed) == {str(request_id) for request_id in stored}
        assert oldest is not None

    async def test_a_pending_request_of_a_soft_deleted_or_missing_workspace_is_listed(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _list(client, status="pending", limit=500)

        listed = {item["request_id"]: item for item in body["items"]}
        assert listed[str(world.ids["pending_dead"])]["workspace_id"] == str(world.dead)
        assert listed[str(world.ids["pending_missing"])]["workspace_id"] == str(world.missing)

    async def test_paging_returns_every_row_exactly_once_with_ties_broken_by_id(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            whole = await _list(client, workspace_id=str(world.dead))
            pages = [
                await _list(client, workspace_id=str(world.dead), limit=1, offset=offset)
                for offset in range(4)
            ]
            everything = await _list(client, limit=500)
            total = everything["total"]
            walked = [
                item["request_id"]
                for offset in range(0, total, 3)
                for item in (await _list(client, limit=3, offset=offset))["items"]
            ]

        assert [len(page["items"]) for page in pages] == [1, 1, 1, 0]
        assert {page["total"] for page in pages} == {3}
        assert [(page["limit"], page["offset"]) for page in pages[:2]] == [(1, 0), (1, 1)]
        assert [item for page in pages for item in page["items"]] == whole["items"]
        assert _mine(world, whole) == ["pending_dead", *_tie_order(world)]
        # The same order on a second read, and no row twice across pages.
        assert walked == [item["request_id"] for item in everything["items"]]
        assert len(walked) == len(set(walked)) == total

    @pytest.mark.parametrize(
        "params",
        [
            {"status": "open"},
            {"status": "PENDING"},
            {"workspace_id": "not-a-uuid"},
            {"limit": 0},
            {"limit": 501},
            {"offset": -1},
        ],
    )
    async def test_a_bad_value_is_a_422(self, world, maker, mode, params) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            response = await client.get(URL, params=params)

        assert response.status_code == 422

    async def test_no_decider_address_and_nothing_of_the_workspace_or_its_people_is_answered(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            responses = [
                await client.get(URL, params={"limit": 500}),
                await client.get(URL, params={"status": "approved"}),
                await client.get(URL, params={"workspace_id": str(world.live)}),
            ]

        for response in responses:
            assert response.status_code == 200
            raw = response.text
            assert str(world.ids["approved"]) in raw
            for label, sentinel in world.sentinels.items():
                assert sentinel not in raw, label
                assert sentinel.lower() not in raw.lower(), label
            assert "decided_by_email" not in raw
            assert "people.example" not in raw
            assert world.owner not in raw
            for item in response.json()["items"]:
                assert set(item) == ITEM_KEYS


@pytest.mark.parametrize("mode", list(PRINCIPALS))
async def test_the_read_writes_no_audit_row_and_changes_no_request(
    world, maker, db_session, mode
) -> None:
    async def _state() -> tuple[int, list[tuple]]:
        await db_session.rollback()
        audits = await db_session.scalar(select(func.count()).select_from(AuditLog))
        rows = (
            await db_session.execute(
                select(Request.id, Request.status, Request.decided_at, Request.decided_by_email)
                .where(Request.id.in_(list(world.ids.values())))
                .order_by(Request.id)
            )
        ).all()
        return audits, [tuple(row) for row in rows]

    before = await _state()
    async with _client(maker, PRINCIPALS[mode]) as client:
        await _list(client, limit=500)
        await _list(client, status="pending", workspace_id=str(world.dead))
    assert await _state() == before
