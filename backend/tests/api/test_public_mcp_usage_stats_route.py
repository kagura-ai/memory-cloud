"""Owner statistics route of a public MCP endpoint (#3035), on real rows.

``get_db`` is the test database and the caller is injected through
``get_user_from_api_key_or_session``; the real ``require_workspace_owner``,
the endpoint lookup and the context read chokepoint run. The rate counter is
an in-memory fake, so the file needs no Redis.

Fixture: workspace A (owner, member, admin) with a published resource ``cat``
(live endpoint L, an earlier revoked endpoint R), a second published resource
``two`` (endpoint T), an unpublished resource ``unpub`` and a resource ``priv``
whose context is private to the admin (a live endpoint row); workspace B
(another owner, endpoint X); workspace C, also owned by A's owner.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as day_time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.agent import AGENT_ENFORCEMENT_ENFORCE, AGENT_ENFORCEMENT_SHADOW
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import PMU_OUTCOMES, PublicMcpDocKey, PublicMcpUsageEvent
from models.resource import Resource
from services import public_mcp_usage_stats as stats
from services.public_mcp_usage_events import UsageEventInput, build_usage_event_row, doc_key
from services.public_mcp_usage_stats import LiveEndpoint, read_usage_stats
from utils.datetime import utcnow
from utils.exceptions import CapacityLockedError, PublicMcpUsageStatsUnavailableError

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOT_FOUND = {"error": "RES-001", "message": "Public MCP endpoint not found", "details": {}}


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    tag: str
    owner: str
    member: str
    admin: str
    stranger: str
    ws: UUID
    other_ws: UUID
    third_ws: UUID
    slug: dict[str, str] = field(default_factory=dict)
    ctx: dict[str, UUID] = field(default_factory=dict)
    resource_pk: dict[str, UUID] = field(default_factory=dict)
    endpoint: dict[str, UUID] = field(default_factory=dict)
    public_id: dict[str, str] = field(default_factory=dict)
    doc: dict[str, str] = field(default_factory=dict)
    memory_ids: list[UUID] = field(default_factory=list)
    first_day: Any = None

    def key(self, name: str) -> str:
        return doc_key(self.resource_pk["cat"], self.doc[name], get_settings().audit_hmac_key)


def _event(
    w: World,
    resource: str,
    endpoint: UUID,
    tool: str,
    outcome: str,
    at: datetime,
    docs: tuple[str, ...] = (),
    *,
    workspace_id: UUID | None = None,
    documents: Any = "built",
) -> dict[str, Any]:
    row = build_usage_event_row(
        UsageEventInput(
            endpoint_id=endpoint,
            workspace_id=workspace_id or (w.other_ws if resource == "x" else w.ws),
            context_id=w.ctx[resource],
            resource_pk=w.resource_pk[resource],
            resource_id=w.slug[resource],
            tool=tool,
            outcome=outcome,
            era="stateless",
            started_at=at,
            duration_ms=3,
            doc_ids=tuple(w.doc[d] for d in docs),
        ),
        key=get_settings().audit_hmac_key,
        session_details_enabled=False,
    ).row
    row["occurred_at"] = at
    if documents != "built":
        row["documents"] = documents
    return row


def _memory(w: World, name: str, summary: str, content: str, **kw: Any) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=w.ws,
        context_id=w.ctx["cat"],
        user_id=w.owner,
        summary=summary,
        content=content,
        type="resource_data",
        scope="persistent",
        client="resource-indexer",
        importance=0.5,
        embedding_status="success",
        tags=[],
        details={"resource_id": w.slug["cat"], "doc_id": w.doc[name]},
        **kw,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, admin, stranger = (f"us-{r}-{tag}" for r in ("owner", "member", "admin", "x"))
    async with factory() as s:
        for uid in (owner, member, admin, stranger):
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        ws = Workspace(id=uuid4(), name=f"us-a-{tag}", plan_name="pro", owner_user_id=owner)
        other = Workspace(id=uuid4(), name=f"us-b-{tag}", plan_name="pro", owner_user_id=stranger)
        third = Workspace(id=uuid4(), name=f"us-c-{tag}", plan_name="pro", owner_user_id=owner)
        s.add_all([ws, other, third])
        await s.flush()
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=ws.id, user_id=member, role=WorkspaceRole.MEMBER),
                WorkspaceMember(workspace_id=ws.id, user_id=admin, role=WorkspaceRole.ADMIN),
                WorkspaceMember(workspace_id=other.id, user_id=stranger, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=third.id, user_id=owner, role=WorkspaceRole.OWNER),
            ]
        )
        w = World(
            factory=factory,
            tag=tag,
            owner=owner,
            member=member,
            admin=admin,
            stranger=stranger,
            ws=ws.id,
            other_ws=other.id,
            third_ws=third.id,
        )
        for name, wsid, creator, private in (
            ("cat", ws.id, owner, False),
            ("two", ws.id, owner, False),
            ("unpub", ws.id, owner, False),
            ("priv", ws.id, admin, True),
            ("x", other.id, stranger, False),
        ):
            slug = f"{name}-{tag}"
            resource = Resource(id=uuid4(), workspace_id=wsid, resource_id=slug, created_by=creator)
            s.add(resource)
            await s.flush()
            context = Context(
                id=uuid4(),
                workspace_id=wsid,
                name=slug,
                created_by=creator,
                is_private=private,
                is_public=not private,
                resource_id=slug,
            )
            s.add(context)
            w.slug[name], w.ctx[name], w.resource_pk[name] = slug, context.id, resource.id
        await s.flush()
        for name, resource, revoked in (
            ("R", "cat", True),
            ("L", "cat", False),
            ("T", "two", False),
            ("P", "priv", False),
            ("X", "x", False),
        ):
            row = PublicMcpEndpoint(
                workspace_id=w.other_ws if resource == "x" else w.ws,
                context_id=w.ctx[resource],
                resource_pk=w.resource_pk[resource],
                resource_id=w.slug[resource],
                public_name="Catalog",
                created_by=w.stranger
                if resource == "x"
                else (admin if resource == "priv" else owner),
                revoked_at=utcnow() - timedelta(days=3) if revoked else None,
            )
            s.add(row)
            await s.flush()
            w.endpoint[name], w.public_id[name] = row.id, row.public_id

        # Documents of ``cat``: a feed product, a plain row, a deleted row, twelve more.
        names = ["d1", "d2", "d3", "unknown", *(f"d{i}" for i in range(5, 17))]
        w.doc = {n: f"zz-doc-{tag}-{n}" for n in names}
        feed = json.dumps(
            {
                "record_type": "product",
                "external_id": "sku-1",
                "title": "Trail shoe",
                "url": "https://example.com/p/1",
            }
        )
        rows = [
            _memory(w, "d1", "[cat] sync:1 v1", feed, source="resource_ingest", version=1),
            _memory(w, "d2", "Plain document two", "two"),
            _memory(w, "d3", "Deleted document", "three", deleted_at=utcnow()),
            *(_memory(w, f"d{i}", f"Document {i}", "x") for i in range(5, 17)),
        ]
        s.add_all(rows)
        w.memory_ids = [m.id for m in rows]
        for n in names:
            if n == "unknown":
                continue  # a key the mapping does not know
            s.add(
                PublicMcpDocKey(
                    resource_pk=w.resource_pk["cat"],
                    doc_key=w.key(n),
                    resource_doc_id=w.doc[n],
                )
            )

        today = utcnow().date()
        first_day = today - timedelta(days=6)
        w.first_day = first_day
        start = datetime.combine(first_day, day_time.min)
        now = utcnow().replace(microsecond=0)
        L = w.endpoint["L"]

        def at(seconds_ago: int) -> datetime:
            return max(now - timedelta(seconds=seconds_ago), datetime.combine(today, day_time.min))

        events = [
            # Counted: the first instant of the window.
            _event(w, "cat", L, "recall", "success", start, ("d1",)),
            # Not counted: one second earlier.
            _event(w, "cat", L, "recall", "success", start - timedelta(seconds=1), ("d1",)),
            _event(w, "cat", L, "recall", "success", at(60), ("d1", "d2")),
            _event(w, "cat", L, "recall", "degraded", at(50), ("d2", "d1")),
            _event(w, "cat", L, "recall", "success", at(40), ("unknown",)),
            _event(w, "cat", L, "recall", "success", at(30), ("d3",)),
            _event(w, "cat", L, "recall", "success", at(20), tuple(f"d{i}" for i in range(5, 17))),
            # Calls, never documents.
            _event(w, "cat", L, "reference", "success", at(10), ("d16",)),
            _event(w, "cat", L, "explore", "success", at(9), ("d16",)),
            _event(w, "cat", L, "recall", "rate_limited", at(8), ("d16",)),
            _event(w, "cat", L, "list_tags", "error", at(7)),
            # Never counted for L: another endpoint, the revoked one, another
            # workspace, and the live id under another workspace id.
            _event(w, "cat", w.endpoint["R"], "recall", "success", at(6), ("d16",)),
            _event(w, "two", w.endpoint["T"], "reference", "success", at(5), ("d16",)),
            _event(w, "x", w.endpoint["X"], "recall", "success", at(4), ("d16",)),
            _event(w, "cat", L, "recall", "success", at(3), ("d16",), workspace_id=w.other_ws),
            # ``two``: searches that returned nothing.
            _event(w, "two", w.endpoint["T"], "recall", "success", at(2), documents=[]),
            _event(w, "two", w.endpoint["T"], "recall", "success", at(1), documents=[]),
        ]
        await s.execute(insert(PublicMcpUsageEvent).values(events))
        await s.commit()
    try:
        yield w
    finally:
        async with factory() as s:
            await s.execute(
                delete(PublicMcpUsageEvent).where(
                    PublicMcpUsageEvent.endpoint_id.in_(list(w.endpoint.values()))
                )
            )
            await s.execute(
                delete(PublicMcpDocKey).where(
                    PublicMcpDocKey.resource_pk.in_(list(w.resource_pk.values()))
                )
            )
            await s.execute(
                delete(Workspace).where(Workspace.id.in_([w.ws, w.other_ws, w.third_ws]))
            )
            await s.execute(delete(User).where(User.user_id.in_([owner, member, admin, stranger])))
            await s.commit()


class _Counter:
    def __init__(self) -> None:
        self.calls = 0
        self.value = 0

    async def __call__(self, key: str, ttl: int | None = None) -> int:
        self.calls += 1
        self.value += 1
        return self.value


@pytest.fixture(autouse=True)
def counter(monkeypatch) -> _Counter:
    fake = _Counter()
    monkeypatch.setattr(stats, "increment_counter", fake)
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    return fake


@contextlib.asynccontextmanager
async def _client(
    w: World,
    user_id: str | None,
    *,
    workspace_id: UUID | None = None,
    scope: AgentScope | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def _user() -> dict[str, Any]:
        set_agent_scope(scope)
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.test",
            "role": "user",
            "current_workspace_id": workspace_id or w.ws,
        }

    app.dependency_overrides[get_db] = _db
    if user_id is not None:
        app.dependency_overrides[get_user_from_api_key_or_session] = _user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        set_agent_scope(None)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)


def _path(slug: str, days: Any = None) -> str:
    query = "" if days is None else f"?days={days}"
    return f"/api/v1/resources/{slug}/public-mcp/usage-stats{query}"


async def _get(w: World, slug: str, user: str | None = None, **kw: Any) -> httpx.Response:
    days = kw.pop("days", None)
    async with _client(w, user if user is not None else w.owner, **kw) as c:
        return await c.get(_path(slug, days))


def _live(w: World, name: str = "L", resource: str = "cat") -> LiveEndpoint:
    return LiveEndpoint(
        id=w.endpoint[name],
        public_id=w.public_id[name],
        resource_pk=w.resource_pk[resource],
        context_id=w.ctx[resource],
    )


async def _read(db: AsyncSession, w: World, days: int = 7):
    return await read_usage_stats(
        db,
        endpoint=_live(w),
        workspace_id=w.ws,
        days=days,
        retention_days=90,
        recording_enabled=True,
    )


# ----------------------------------------------------------------------------
# Counts
# ----------------------------------------------------------------------------


async def test_owner_gets_counts_per_utc_day(world: World) -> None:
    w = world
    r = await _get(w, w.slug["cat"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resource_id"] == w.slug["cat"]
    assert body["endpoint_id"] == str(w.endpoint["L"])
    assert (body["window_days"], body["retention_days"]) == (7, 90)
    assert body["first_day"] == w.first_day.isoformat()
    assert body["last_day"] == utcnow().date().isoformat()
    assert body["recording_enabled"] is True
    assert body["generated_at"].endswith("Z")
    days = body["days"]
    assert [d["date"] for d in days] == [
        (w.first_day + timedelta(days=i)).isoformat() for i in range(7)
    ]
    # The first instant of the window counts; one second earlier does not.
    assert (days[0]["calls"], days[0]["searches"]) == (1, 1)
    today = days[-1]
    assert today["calls"] == 9
    assert today["searches"] == 6
    assert today["search_outcomes"] == {
        **dict.fromkeys(PMU_OUTCOMES, 0),
        "success": 4,
        "degraded": 1,
        "rate_limited": 1,
    }
    totals = body["totals"]
    assert (totals["calls"], totals["searches"]) == (10, 7)
    assert totals["calls"] == sum(d["calls"] for d in days)
    assert set(totals["search_outcomes"]) == set(PMU_OUTCOMES)
    assert totals["search_outcomes"]["success"] == 5


async def test_documents_order_limit_rank_and_titles(world: World) -> None:
    w = world
    r = await _get(w, w.slug["cat"])
    docs = r.json()["documents"]
    assert docs["status"] == "ok"
    assert docs["publication_serving"] is True
    assert docs["searches_scanned"] == 6
    assert docs["scan_limit"] == 20_000
    items = docs["items"]
    assert len(items) == 10
    assert items[0] == {
        "doc_key": w.key("d1"),
        "searches": 3,
        "best_rank": 1,
        "title": "Trail shoe",
        "source_url": "https://example.com/p/1",
    }
    assert (items[1]["doc_key"], items[1]["searches"], items[1]["best_rank"]) == (w.key("d2"), 2, 1)
    assert items[1]["title"] == "Plain document two"
    rank_one = {w.key("d3"), w.key("unknown"), w.key("d5")}
    assert [i["doc_key"] for i in items[2:5]] == sorted(rank_one)
    assert [i["doc_key"] for i in items[5:]] == [w.key(f"d{n}") for n in range(6, 11)]
    assert [i["best_rank"] for i in items[5:]] == [2, 3, 4, 5, 6]
    by_key = {i["doc_key"]: i for i in items}
    assert by_key[w.key("d3")]["title"] is None  # deleted
    assert by_key[w.key("unknown")]["title"] is None  # not in the mapping
    assert by_key[w.key("d5")]["title"] == "Document 5"
    # Only recall success / degraded count: d16 was returned by other tools.
    assert w.key("d16") not in by_key
    for doc in w.doc.values():
        assert doc not in r.text
    for memory_id in w.memory_ids:
        assert str(memory_id) not in r.text


async def test_searches_that_returned_nothing_are_scanned(world: World) -> None:
    r = await _get(world, world.slug["two"])
    docs = r.json()["documents"]
    assert docs["searches_scanned"] == 2
    assert docs["items"] == []
    assert r.json()["totals"]["searches"] == 2
    assert r.json()["totals"]["calls"] == 3


async def test_scan_limit(world: World, monkeypatch) -> None:
    monkeypatch.setattr(stats, "USAGE_STATS_DOCUMENT_SCAN_LIMIT", 3)
    r = await _get(world, world.slug["cat"])
    docs = r.json()["documents"]
    assert (docs["searches_scanned"], docs["scan_limit"]) == (3, 3)
    keys = {i["doc_key"] for i in docs["items"]}
    assert world.key("d1") not in keys and world.key("d2") not in keys
    assert {world.key("d3"), world.key("unknown"), world.key("d5")} <= keys


@pytest.mark.parametrize("change", ["switch_off", "not_public", "capacity_locked"])
async def test_not_serving_hides_every_title(world: World, monkeypatch, change) -> None:
    w = world
    if change == "switch_off":
        monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", False)
    elif change == "not_public":
        async with w.factory() as s:
            await s.execute(
                update(Context).where(Context.id == w.ctx["cat"]).values(is_public=False)
            )
            await s.commit()
    else:

        async def locked(*args: Any, **kwargs: Any) -> None:
            raise CapacityLockedError.for_outsider()

        monkeypatch.setattr("mcp_server.public.publication.ensure_not_capacity_locked", locked)
    try:
        r = await _get(w, w.slug["cat"])
    finally:
        if change == "not_public":
            async with w.factory() as s:
                await s.execute(
                    update(Context).where(Context.id == w.ctx["cat"]).values(is_public=True)
                )
                await s.commit()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["calls"] == 10
    docs = body["documents"]
    assert docs["status"] == "ok"
    assert docs["publication_serving"] is False
    assert len(docs["items"]) == 10
    assert all(i["title"] is None and i["source_url"] is None for i in docs["items"])


# ----------------------------------------------------------------------------
# Real database errors, the read-only sections, the timeout, nothing written
# ----------------------------------------------------------------------------


async def test_document_failure_keeps_counts_and_the_session(world: World, monkeypatch) -> None:
    async def failing(db, endpoint, keys):
        await db.execute(text("SELECT 1/0"))

    monkeypatch.setattr(stats, "resolve_public_documents", failing)
    async with world.factory() as db:
        result = await _read(db, world)
        assert result.totals.calls == 10
        assert result.documents.status == "unavailable"
        assert result.documents.items == []
        assert (await db.execute(text("SELECT 1"))).scalar() == 1
        await db.rollback()
    r = await _get(world, world.slug["cat"])
    assert r.status_code == 200
    assert r.json()["documents"]["status"] == "unavailable"
    assert r.json()["totals"]["calls"] == 10


async def test_day_count_failure_is_503_and_keeps_the_session(world: World, monkeypatch) -> None:
    monkeypatch.setattr(stats, "DAY_COUNTS", text("SELECT 1/0 AS day"))
    async with world.factory() as db:
        with pytest.raises(PublicMcpUsageStatsUnavailableError):
            await _read(db, world)
        assert (await db.execute(text("SELECT 1"))).scalar() == 1
        await db.rollback()
    r = await _get(world, world.slug["cat"])
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert r.json()["error"] == "usage_stats_unavailable"


async def test_document_section_cannot_write(world: World, monkeypatch) -> None:
    w = world
    probe, after = uuid4().hex, uuid4().hex

    async def writing(db, endpoint, keys):
        await db.execute(
            insert(PublicMcpDocKey).values(
                resource_pk=w.resource_pk["cat"], doc_key=probe, resource_doc_id="probe"
            )
        )

    monkeypatch.setattr(stats, "resolve_public_documents", writing)
    async with w.factory() as db:
        result = await _read(db, w)
        assert result.documents.status == "unavailable"
        # The read-only mode ended with the savepoint: the session can write.
        await db.execute(
            insert(PublicMcpDocKey).values(
                resource_pk=w.resource_pk["cat"], doc_key=after, resource_doc_id="p"
            )
        )
        await db.rollback()
    async with w.factory() as db:
        written = await db.scalar(
            select(func.count())
            .select_from(PublicMcpDocKey)
            .where(PublicMcpDocKey.doc_key.in_([probe, after]))
        )
    assert written == 0


async def test_statement_timeout_is_set_in_the_reading_transaction(world: World) -> None:
    async with world.factory() as db:
        before = (await db.execute(text("SHOW statement_timeout"))).scalar()
        await _read(db, world)
        during = (await db.execute(text("SHOW statement_timeout"))).scalar()
        await db.rollback()
        after = (await db.execute(text("SHOW statement_timeout"))).scalar()
    assert during != before
    assert during.endswith("s")
    assert after == before


async def test_nothing_is_written(world: World) -> None:
    async def counts() -> tuple[int, int]:
        async with world.factory() as db:
            events = await db.scalar(select(func.count()).select_from(PublicMcpUsageEvent))
            keys = await db.scalar(select(func.count()).select_from(PublicMcpDocKey))
        return int(events or 0), int(keys or 0)

    before = await counts()
    assert (await _get(world, world.slug["cat"])).status_code == 200
    assert await counts() == before


# ----------------------------------------------------------------------------
# Rate bucket and refusals
# ----------------------------------------------------------------------------


async def test_rate_limit_is_429_after_the_lookup(world: World, counter: _Counter) -> None:
    w = world
    assert (await _get(w, w.slug["cat"], user=w.member)).status_code == 403
    assert (await _get(w, f"nope-{w.tag}")).status_code == 404
    assert counter.calls == 0
    counter.value = stats.USAGE_STATS_RATE_LIMIT_PER_MINUTE
    r = await _get(w, w.slug["cat"])
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"
    assert r.json()["error"] == "RATE-001"
    assert counter.calls == 1


async def test_refusals_before_the_lookup(world: World) -> None:
    w = world
    async with _client(w, None) as c:
        assert (await c.get(_path(w.slug["cat"]))).status_code == 401
    assert (await _get(w, w.slug["cat"], user=w.member)).status_code == 403
    assert (await _get(w, w.slug["cat"], user=w.admin)).status_code == 403


async def test_every_lookup_refusal_is_one_404(world: World, counter: _Counter) -> None:
    w = world
    enforce = AgentScope(
        agent_id=uuid4(), enforcement_mode=AGENT_ENFORCEMENT_ENFORCE, workspace_id=w.ws
    )
    answers = [
        await _get(w, f"nope-{w.tag}"),
        await _get(w, w.slug["x"]),
        await _get(w, w.slug["unpub"]),
        await _get(w, w.slug["priv"]),
        await _get(w, w.slug["cat"], workspace_id=w.third_ws),
        await _get(w, w.slug["cat"], scope=enforce),
    ]
    for r in answers:
        assert r.status_code == 404, r.text
        assert r.json() == NOT_FOUND
    assert counter.calls == 0


async def test_shadow_agent_scope_is_served(world: World) -> None:
    w = world
    shadow = AgentScope(
        agent_id=uuid4(), enforcement_mode=AGENT_ENFORCEMENT_SHADOW, workspace_id=w.ws
    )
    assert (await _get(w, w.slug["cat"], scope=shadow)).status_code == 200


@pytest.mark.parametrize("days", [0, 91, "x", -1])
async def test_days_out_of_range(world: World, days: Any) -> None:
    assert (await _get(world, world.slug["cat"], days=days)).status_code == 422


async def test_days_default_and_retention_cap(world: World, monkeypatch) -> None:
    assert (await _get(world, world.slug["cat"])).json()["window_days"] == 7
    assert (await _get(world, world.slug["cat"], days=30)).json()["window_days"] == 30
    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_retention_days", 20)
    body = (await _get(world, world.slug["cat"], days=60)).json()
    assert (body["window_days"], body["retention_days"]) == (20, 20)
    assert len(body["days"]) == 20
