"""Integration tests for the resource-events read service (Issue #316).

Exercises ``services.resource_events.list_resource_events`` against a real
database: keyset cursor pagination, the fixed filters (op / doc_id / version
/ since), newest-first ordering, and the cross-workspace fail-safe.

Seeds a Workspace + Resource + ResourceEvent rows directly (the service only
resolves ``(workspace_id, resource_id) → resources.id`` and filters events by
``resource_pk``, so no Context / membership rows are required).
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from models.resource import Resource, ResourceEvent
from services.resource_events import list_resource_events

SLUG = "ec-products"
BASE = datetime(2026, 6, 7, 12, 0, 0)


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession):
    """Seed a workspace + resource + 5 events; return ids for assertions.

    Event insertion order (so id ascending == this order):
      e1 upsert sku-1 v1   @ +1m
      e2 upsert sku-2 v1   @ +2m
      e3 upsert sku-1 v2   @ +3m
      e4 delete sku-2 None @ +4m  (payload None)
      e5 upsert sku-3 v1   @ +5m
    """
    owner = f"owner_{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    other_ws = Workspace(
        id=uuid4(),
        name=f"ws-other-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=f"owner_{uuid4().hex[:8]}",
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    resource = Resource(
        id=uuid4(),
        workspace_id=ws.id,
        resource_id=SLUG,
        name="EC Products",
        created_by=owner,
    )
    db_session.add_all([ws, other_ws])
    await db_session.flush()
    db_session.add(resource)
    await db_session.flush()

    specs = [
        ("upsert", "sku-1", 1, {"n": 1}, 1),
        ("upsert", "sku-2", 1, {"n": 2}, 2),
        ("upsert", "sku-1", 2, {"n": 3}, 3),
        ("delete", "sku-2", None, None, 4),
        ("upsert", "sku-3", 1, {"n": 5}, 5),
    ]
    events = []
    for op, doc_id, version, payload, minute in specs:
        ev = ResourceEvent(
            resource_pk=resource.id,
            resource_id=SLUG,
            op=op,
            doc_id=doc_id,
            version=version,
            payload=payload,
            created_at=BASE + timedelta(minutes=minute),
        )
        db_session.add(ev)
        await db_session.flush()  # assign autoincrement id in insertion order
        events.append(ev)
    await db_session.commit()

    ids = [e.id for e in events]
    return {
        "db": db_session,
        "workspace_id": ws.id,
        "other_workspace_id": other_ws.id,
        "resource_pk": resource.id,
        "ids": ids,  # [e1, e2, e3, e4, e5]
        "created_at": [e.created_at for e in events],
    }


@pytest.mark.asyncio
async def test_orders_newest_first(seeded):
    events, next_cursor = await list_resource_events(
        seeded["db"], seeded["workspace_id"], SLUG, limit=10
    )
    got = [e.id for e in events]
    assert got == list(reversed(seeded["ids"]))  # e5..e1
    assert next_cursor is None


@pytest.mark.asyncio
async def test_cursor_pagination(seeded):
    e1, e2, e3, e4, e5 = seeded["ids"]
    db, ws = seeded["db"], seeded["workspace_id"]

    page1, c1 = await list_resource_events(db, ws, SLUG, limit=2)
    assert [e.id for e in page1] == [e5, e4]
    assert c1 == str(e4)

    page2, c2 = await list_resource_events(db, ws, SLUG, limit=2, cursor_id=int(c1))
    assert [e.id for e in page2] == [e3, e2]
    assert c2 == str(e2)

    page3, c3 = await list_resource_events(db, ws, SLUG, limit=2, cursor_id=int(c2))
    assert [e.id for e in page3] == [e1]
    assert c3 is None


@pytest.mark.asyncio
async def test_filter_op_delete(seeded):
    e4 = seeded["ids"][3]
    events, _ = await list_resource_events(seeded["db"], seeded["workspace_id"], SLUG, op="delete")
    assert [e.id for e in events] == [e4]
    assert events[0].payload is None


@pytest.mark.asyncio
async def test_filter_doc_id(seeded):
    e1, _e2, e3, _e4, _e5 = seeded["ids"]
    events, _ = await list_resource_events(
        seeded["db"], seeded["workspace_id"], SLUG, doc_id="sku-1"
    )
    assert [e.id for e in events] == [e3, e1]


@pytest.mark.asyncio
async def test_filter_version(seeded):
    e1, e2, _e3, _e4, e5 = seeded["ids"]
    events, _ = await list_resource_events(seeded["db"], seeded["workspace_id"], SLUG, version=1)
    # v1 upserts only: e5, e2, e1 (e3 is v2, e4 is delete/None)
    assert [e.id for e in events] == [e5, e2, e1]


@pytest.mark.asyncio
async def test_filter_version_zero(seeded):
    """version=0 is a real boundary, not 'no filter' — the guard must use
    'is not None', not a falsy check. Seeds a v0 upsert and asserts it is the
    sole match (a falsy `if version:` would return all events instead)."""
    db, ws = seeded["db"], seeded["workspace_id"]
    v0 = ResourceEvent(
        resource_pk=seeded["resource_pk"],
        resource_id=SLUG,
        op="upsert",
        doc_id="sku-zero",
        version=0,
        payload={"n": 0},
        created_at=BASE + timedelta(minutes=10),
    )
    db.add(v0)
    await db.commit()

    events, _ = await list_resource_events(db, ws, SLUG, version=0)
    assert [e.doc_id for e in events] == ["sku-zero"]


@pytest.mark.asyncio
async def test_filter_since(seeded):
    e3, e4, e5 = seeded["ids"][2], seeded["ids"][3], seeded["ids"][4]
    since = seeded["created_at"][2]  # e3's created_at
    events, _ = await list_resource_events(seeded["db"], seeded["workspace_id"], SLUG, since=since)
    assert [e.id for e in events] == [e5, e4, e3]


@pytest.mark.asyncio
async def test_limit_clamp_caps_page(seeded):
    # Asking for 10_000 must not exceed the seeded set (5); clamp doesn't error.
    events, _ = await list_resource_events(seeded["db"], seeded["workspace_id"], SLUG, limit=10_000)
    assert len(events) == 5


@pytest.mark.asyncio
async def test_cross_workspace_returns_empty(seeded):
    """A slug that does not resolve to a Resource in the caller's workspace
    yields an empty page — never another workspace's events (CWE-639)."""
    events, cursor = await list_resource_events(seeded["db"], seeded["other_workspace_id"], SLUG)
    assert events == []
    assert cursor is None


@pytest.mark.asyncio
async def test_unknown_slug_returns_empty(seeded):
    events, cursor = await list_resource_events(
        seeded["db"], seeded["workspace_id"], "does-not-exist"
    )
    assert events == []
    assert cursor is None
