"""REST public searches made with a bound key, recorded on real rows (#3035).

The REST public search through the ASGI app against the test database, with a
usage writer started by the fixture (``ASGITransport`` runs no lifespan, so
none would run otherwise) and the search stubbed to return seeded rows:

* a bound search with recording on leaves one ``route='rest'``,
  ``caller='bound_key'`` event of the serving endpoint, whose document keys
  are the ones a public MCP ``recall`` of the same rows stores; a member's
  note keeps the later ranks true; a tombstoned row, a row of another context
  and a repeat are not answered;
* visitors are written in crowds, in a transaction of their own;
* a context whose slug moved records nothing; a failing lookup records
  nothing and does not abort the request;
* neither the key's text nor the visitor value is stored anywhere;
* anonymous searches, and keys refused at attribution or by the context
  check, leave nothing (their rows unchanged);
* the bound key's row lock is released before the search runs;
* a REST row is not in the export stream.

Every row a test commits is deleted afterwards.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.public_mcp_usage_writer as writer_module
from api.main import app
from auth.api_keys import APIKeyManager
from config.settings import get_settings
from models.agent import Agent
from models.auth import APIKey, Context, UsageStats, WorkspaceMember
from models.memory import Memory
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import (
    PublicMcpDailyVisitor,
    PublicMcpDocKey,
    PublicMcpUsageDayCount,
    PublicMcpUsageEvent,
)
from models.resource import Resource
from services.public_mcp_usage_events import doc_key, read_usage_events_page
from services.public_mcp_usage_writer import PublicMcpUsageWriter

pytestmark = pytest.mark.asyncio

TOKEN_MARKER = "TOKENMARKER3035"
VISITOR_MARKER = "vis-MARKER-3035"


def _memory(pub, *, context_id: UUID | None = None, doc: str | None = None, **kw: Any) -> Memory:
    details = {"resource_id": pub.slug, "doc_id": doc} if doc is not None else {}
    return Memory(
        id=uuid4(),
        workspace_id=pub.ws.id,
        context_id=context_id or pub.ctx,
        user_id=pub.owner,
        summary=f"row {doc or 'note'}",
        content="x",
        type="resource_data" if doc else "note",
        scope="persistent",
        client="resource-indexer" if doc else "test",
        importance=0.5,
        embedding_status="success",
        tags=[],
        details=details,
        **kw,
    )


async def _public_context(w, ws, owner: str, slug: str | None) -> UUID:
    context = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_public=True,
        is_private=False,
        resource_id=slug,
    )
    w.db.add(context)
    await w.db.flush()
    return context.id


async def _bound_key(w, user_id: str, context_id: UUID) -> tuple[str, int]:
    plaintext, row = await APIKeyManager(w.db).create_key(
        name=f"bound-{uuid4().hex[:6]}", user_id=user_id, bound_context_id=context_id
    )
    await w.db.flush()
    return plaintext, row.id


@pytest_asyncio.fixture
async def pub(connector_world, fernet_api_key_secret) -> AsyncIterator[SimpleNamespace]:
    """A workspace with a published resource context, its rows and two bound keys."""
    w = connector_world
    owner = await w.user("ps-owner")
    ws = await w.workspace(owner)
    ws_id = ws.id
    slug = f"cat-{uuid4().hex[:8]}"
    resource = Resource(id=uuid4(), workspace_id=ws.id, resource_id=slug, created_by=owner)
    w.db.add(resource)
    await w.db.flush()
    ctx = await _public_context(w, ws, owner, slug)
    other_ctx = await _public_context(w, ws, owner, None)
    endpoint = PublicMcpEndpoint(
        workspace_id=ws.id,
        context_id=ctx,
        resource_pk=resource.id,
        resource_id=slug,
        public_name="Catalog",
        created_by=owner,
    )
    w.db.add(endpoint)
    await w.db.flush()
    p = SimpleNamespace(
        w=w,
        db=w.db,
        owner=owner,
        ws=ws,
        ws_id=ws_id,
        slug=slug,
        resource_pk=resource.id,
        ctx=ctx,
        other_ctx=other_ctx,
        endpoint=endpoint.id,
        endpoints=[endpoint.id],
        contexts=[ctx, other_ctx],
    )
    p.d1 = _memory(p, doc="doc-1")
    p.d2 = _memory(p, doc="doc-2")
    p.note = _memory(p)  # a member's note in the context: no resource document
    p.gone = _memory(p, doc="doc-gone", deleted_at=func.now())
    p.foreign = _memory(p, context_id=other_ctx, doc="doc-foreign")
    w.db.add_all([p.d1, p.d2, p.note, p.gone, p.foreign])
    p.key1, p.key1_id = await _bound_key(w, owner, ctx)
    p.key2, p.key2_id = await _bound_key(w, owner, ctx)
    await w.db.commit()
    try:
        yield p
    finally:
        db = w.db
        await db.rollback()
        for model in (PublicMcpUsageEvent, PublicMcpDailyVisitor, PublicMcpUsageDayCount):
            await db.execute(delete(model).where(model.endpoint_id.in_(p.endpoints)))
        await db.execute(
            delete(PublicMcpDocKey).where(PublicMcpDocKey.resource_pk == p.resource_pk)
        )
        await db.execute(delete(APIKey).where(APIKey.user_id.in_(w.targets)))
        await db.execute(delete(UsageStats).where(UsageStats.context_id.in_(p.contexts)))
        await db.execute(
            text("DELETE FROM public_usage_budgets WHERE workspace_id = :ws"), {"ws": ws_id}
        )
        await db.commit()


@pytest_asyncio.fixture
async def writer(pub, async_engine, monkeypatch) -> AsyncIterator[PublicMcpUsageWriter]:
    """A usage writer over the test database; both switches on.

    ``visitor_flush_min_keys=2``: two distinct values reach the crowd path;
    ``stop()`` writes the rest. Stopped before ``pub`` deletes the rows.
    """
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    w = PublicMcpUsageWriter(
        session_factory=factory,
        queue_size=100,
        flush_interval=0.1,
        write_timeout=5.0,
        key=get_settings().audit_hmac_key,
        session_details_enabled=False,
        visitor_flush_min_keys=2,
    )
    monkeypatch.setattr(writer_module, "_writer", w)
    monkeypatch.setattr(get_settings(), "public_search_telemetry_enabled", True)
    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_enabled", True)
    w.start()
    try:
        yield w
    finally:
        await w.stop(timeout=5)


@pytest.fixture
def hits(monkeypatch) -> SimpleNamespace:
    """The search returns ``hits.ids`` (as row hits) in order; calls are recorded."""
    state = SimpleNamespace(ids=[], calls=[], gate=None, entered=None)

    async def _stub(self, **kwargs):  # noqa: ARG001 - mirrors the caller
        state.calls.append(kwargs)
        if state.gate is not None:
            state.entered.set()
            await state.gate.wait()
        return [{"id": str(i), "score": 0.9, "payload": {"summary": "s"}} for i in state.ids]

    monkeypatch.setattr("services.search_service.SearchService.hybrid_search", _stub)
    return state


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _search(
    client: AsyncClient,
    context_id: UUID,
    key: str | None,
    *,
    visitor: str | list[str] | None = None,
    limit: int = 5,
):
    headers: list[tuple[str, str]] = []
    if key is not None:
        headers.append(("Authorization", f"Bearer {key}"))
    for value in [visitor] if isinstance(visitor, str) else visitor or []:
        headers.append(("Kagura-Visitor", value))
    return await client.post(
        f"/api/v1/public/{context_id}/search",
        headers=headers,
        json={"query": "q", "limit": limit},
    )


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


async def _events(pub) -> list[PublicMcpUsageEvent]:
    async with AsyncSession(pub.db.bind) as s:
        return list(
            (
                await s.execute(
                    select(PublicMcpUsageEvent)
                    .where(PublicMcpUsageEvent.endpoint_id.in_(pub.endpoints))
                    .order_by(PublicMcpUsageEvent.id)
                )
            ).scalars()
        )


async def _visitors(pub) -> list[tuple[str, int]]:
    async with AsyncSession(pub.db.bind) as s:
        rows = await s.execute(
            text(
                "SELECT visitor_key, xmin::text::bigint FROM public_mcp_daily_visitors "
                "WHERE endpoint_id = ANY(:e) ORDER BY visitor_key"
            ),
            {"e": pub.endpoints},
        )
        return [(r[0], int(r[1])) for r in rows.all()]


async def _wait_events(pub, count: int) -> list[PublicMcpUsageEvent]:
    async def ready() -> bool:
        return len(await _events(pub)) >= count

    await _until(ready)
    return await _events(pub)


# ---------------------------------------------------------------------------
# One event under the serving endpoint, with the MCP document keys
# ---------------------------------------------------------------------------


async def test_a_bound_search_is_one_rest_event_of_the_serving_endpoint(pub, writer, hits, client):
    hits.ids = [pub.gone.id, pub.note.id, pub.d1.id, pub.foreign.id, pub.d1.id, pub.d2.id]
    response = await _search(client, pub.ctx, pub.key1)
    assert response.status_code == 200, response.text
    body = response.json()
    # The tombstoned row, the other context's row and the repeat are not answered.
    assert [r["memory_id"] for r in body["results"]] == [
        str(pub.note.id),
        str(pub.d1.id),
        str(pub.d2.id),
    ]
    assert body["count"] == 3
    assert hits.calls[-1]["use_rerank"] is False
    assert hits.calls[-1]["k"] == 10

    (event,) = await _wait_events(pub, 1)
    assert (event.route, event.caller, event.kind) == ("rest", "bound_key", "tool_call")
    assert (event.tool, event.era, event.outcome) == ("recall", "stateless", "success")
    assert event.endpoint_id == pub.endpoint
    assert (event.resource_pk, event.resource_id) == (pub.resource_pk, pub.slug)
    assert (event.workspace_id, event.context_id) == (pub.ws.id, pub.ctx)
    assert event.result_count == 3
    assert event.session_key is None and event.client_family is None
    assert event.protocol_version is None
    key = get_settings().audit_hmac_key
    # The member's note is rank 1 with no key; the documents keep ranks 2 and 3.
    assert event.documents == [
        {"doc_key": doc_key(pub.resource_pk, "doc-1", key), "chunks": 1, "rank": 2},
        {"doc_key": doc_key(pub.resource_pk, "doc-2", key), "chunks": 1, "rank": 3},
    ]

    async def keys_written() -> bool:
        async with AsyncSession(pub.db.bind) as s:
            n = await s.scalar(
                select(func.count())
                .select_from(PublicMcpDocKey)
                .where(PublicMcpDocKey.resource_pk == pub.resource_pk)
            )
            return n == 2

    await _until(keys_written)


async def test_visitors_are_written_in_a_crowd_of_their_own(pub, writer, hits, client):
    hits.ids = [pub.d1.id]
    assert (await _search(client, pub.ctx, pub.key1, visitor="v-one")).status_code == 200
    await _wait_events(pub, 1)
    await asyncio.sleep(0.6)  # several drainer passes
    assert await _visitors(pub) == []

    assert (await _search(client, pub.ctx, pub.key1, visitor="v-two")).status_code == 200

    async def two() -> bool:
        return len(await _visitors(pub)) == 2

    await _until(two)
    rows = await _visitors(pub)
    assert rows[0][1] == rows[1][1], "one transaction"
    events = await _events(pub)
    assert all(e.txid % 2**32 != rows[0][1] for e in events)


async def test_two_keys_sending_one_value_count_once(pub, writer, hits, client):
    hits.ids = [pub.d1.id]
    for key in (pub.key1, pub.key2):
        assert (await _search(client, pub.ctx, key, visitor="same-value")).status_code == 200
    await _wait_events(pub, 2)
    await writer.stop(timeout=5)
    assert len(await _visitors(pub)) == 1


async def test_an_anonymous_search_with_a_visitor_leaves_nothing(pub, writer, hits, client):
    hits.ids = [pub.d1.id]
    response = await _search(client, pub.ctx, None, visitor=VISITOR_MARKER)
    assert response.status_code == 200, response.text
    await asyncio.sleep(0.4)
    await writer.stop(timeout=5)
    assert await _events(pub) == []
    assert await _visitors(pub) == []


# ---------------------------------------------------------------------------
# Under the card the endpoint serves, or nowhere
# ---------------------------------------------------------------------------


async def test_a_context_whose_slug_moved_records_nothing(pub, writer, hits, client):
    claimant = await _public_context(pub.w, pub.ws, pub.owner, None)
    pub.contexts.append(claimant)
    await pub.db.execute(update(Context).where(Context.id == pub.ctx).values(resource_id=None))
    await pub.db.execute(update(Context).where(Context.id == claimant).values(resource_id=pub.slug))
    await pub.db.commit()
    hits.ids = [pub.d1.id]
    response = await _search(client, pub.ctx, pub.key1, visitor="v-1")
    assert response.status_code == 200, response.text
    await asyncio.sleep(0.4)
    await writer.stop(timeout=5)
    assert await _events(pub) == []
    async with AsyncSession(pub.db.bind) as s:
        keys = await s.scalar(
            select(func.count())
            .select_from(PublicMcpDocKey)
            .where(PublicMcpDocKey.resource_pk == pub.resource_pk)
        )
    assert keys == 0
    # The endpoint row itself is still live and unrevoked.
    async with AsyncSession(pub.db.bind) as s:
        assert (await s.get(PublicMcpEndpoint, pub.endpoint)).revoked_at is None


async def test_a_failing_lookup_does_not_abort_the_request(pub, writer, hits, client, monkeypatch):
    from services import public_search_usage

    async def failing(db, *, context_id, workspace_id):
        await db.execute(text("SELECT 1/0"))

    monkeypatch.setattr(public_search_usage, "load_context_publication", failing)
    hits.ids = [pub.d1.id, pub.gone.id]
    response = await _search(client, pub.ctx, pub.key1)
    # Served: the filter read after the rolled-back savepoint still ran.
    assert response.status_code == 200, response.text
    assert [r["memory_id"] for r in response.json()["results"]] == [str(pub.d1.id)]
    await asyncio.sleep(0.4)
    await writer.stop(timeout=5)
    assert await _events(pub) == []


# ---------------------------------------------------------------------------
# Nothing of the key or the visitor value is stored
# ---------------------------------------------------------------------------


async def test_neither_marker_is_stored(pub, writer, hits, client, monkeypatch):
    import secrets

    token = f"kagura_{secrets.token_urlsafe(12)[:16]}{TOKEN_MARKER}{secrets.token_urlsafe(8)}"
    monkeypatch.setattr(APIKeyManager, "_generate_key", staticmethod(lambda: token))
    marked, marked_id = await _bound_key(pub.w, pub.owner, pub.ctx)
    await pub.db.commit()
    assert marked == token
    hits.ids = [pub.d1.id, pub.d2.id]
    for value in (VISITOR_MARKER, "other-visitor"):
        response = await _search(client, pub.ctx, marked, visitor=value)
        assert response.status_code == 200, response.text
    await _wait_events(pub, 2)
    await writer.stop(timeout=5)
    assert len(await _visitors(pub)) == 2
    async with AsyncSession(pub.db.bind) as s:
        for table in (
            "SELECT count(*) FROM public_mcp_usage_events AS t WHERE t::text LIKE :m",
            "SELECT count(*) FROM public_mcp_daily_visitors AS t WHERE t::text LIKE :m",
            "SELECT count(*) FROM public_mcp_doc_keys AS t WHERE t::text LIKE :m",
            "SELECT count(*) FROM api_keys AS t WHERE t::text LIKE :m",
            "SELECT count(*) FROM usage_stats AS t WHERE t::text LIKE :m",
        ):
            for marker in (TOKEN_MARKER, VISITOR_MARKER):
                found = await s.scalar(text(table), {"m": f"%{marker}%"})
                assert found == 0, (table, marker)
        # The key served: its use is recorded under its id, not its text.
        assert await s.scalar(
            select(func.count()).select_from(UsageStats).where(UsageStats.api_key_id == marked_id)
        )


# ---------------------------------------------------------------------------
# Keys refused at attribution or by the context check leave nothing
# ---------------------------------------------------------------------------


async def _key_state(db: AsyncSession, key_ids: list[int]) -> dict[int, tuple]:
    rows = await db.execute(
        select(APIKey.id, APIKey.last_used_at, APIKey.revoked_at, APIKey.expires_at).where(
            APIKey.id.in_(key_ids)
        )
    )
    return {r[0]: tuple(r[1:]) for r in rows.all()}


async def test_refused_keys_keep_their_rows(pub, writer, hits, client):
    w, db = pub.w, pub.db
    owner_key, owner_row = await APIKeyManager(db).create_key(name="owner", user_id=pub.owner)
    agent = await w.agent(pub.ws, by=pub.owner)
    agent_id = agent.id
    agent_key, agent_key_id = await w.agent_key(pub.owner, pub.ws, agent)
    private_ctx = await _public_context(w, pub.ws, pub.owner, None)
    pub.contexts.append(private_ctx)
    private_key, private_key_id = await _bound_key(w, pub.owner, private_ctx)
    member = await w.user("ps-member")
    db.add(WorkspaceMember(workspace_id=pub.ws.id, user_id=member, role="member"))
    await db.flush()
    member_key, member_key_id = await _bound_key(w, member, pub.ctx)
    await db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == pub.ws.id, WorkspaceMember.user_id == member
        )
    )
    await db.execute(update(Context).where(Context.id == private_ctx).values(is_public=False))
    await db.commit()
    ids = [owner_row.id, agent_key_id, private_key_id, member_key_id]
    before = await _key_state(db, ids)
    seen_before = await db.scalar(select(Agent.last_seen_at).where(Agent.id == agent_id))
    hits.ids = [pub.d1.id]

    assert (await _search(client, pub.ctx, owner_key, visitor="v-1")).status_code == 403
    assert (await _search(client, pub.ctx, agent_key, visitor="v-1")).status_code == 403
    assert (await _search(client, private_ctx, private_key, visitor="v-1")).status_code == 403
    assert (await _search(client, pub.ctx, member_key, visitor="v-1")).status_code == 401

    await asyncio.sleep(0.3)
    await writer.stop(timeout=5)
    db.expire_all()
    assert await _key_state(db, ids) == before
    assert all(state[0] is None for state in before.values())
    assert await db.scalar(select(Agent.last_seen_at).where(Agent.id == agent_id)) == seen_before
    assert await _events(pub) == []
    assert await _visitors(pub) == []
    assert hits.calls == []


async def test_a_bound_search_does_not_touch_the_public_usage_budget(
    pub, writer, hits, client, monkeypatch
):
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", True)
    hits.ids = [pub.d1.id]
    assert (await _search(client, pub.ctx, pub.key1)).status_code == 200
    async with AsyncSession(pub.db.bind) as s:
        budget = await s.scalar(
            text("SELECT count(*) FROM public_usage_budgets WHERE workspace_id = :ws"),
            {"ws": pub.ws.id},
        )
    assert budget == 0


# ---------------------------------------------------------------------------
# The key's row lock is released before the search runs
# ---------------------------------------------------------------------------


async def test_the_key_row_is_not_locked_while_the_search_runs(pub, writer, hits, client):
    db = pub.db
    assert (await db.get(APIKey, pub.key1_id)).last_used_at is None
    hits.ids = [pub.d1.id]
    hits.gate, hits.entered = asyncio.Event(), asyncio.Event()
    request = asyncio.create_task(_search(client, pub.ctx, pub.key1))
    try:
        await asyncio.wait_for(hits.entered.wait(), timeout=5)
        async with AsyncSession(db.bind) as s, s.begin():
            row = await s.execute(
                text("SELECT id, last_used_at FROM api_keys WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": pub.key1_id},
            )
            locked_id, last_used = row.one()
        assert locked_id == pub.key1_id
        assert last_used is not None, "the key's use was committed before the search"
    finally:
        hits.gate.set()
        response = await request
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The export stream (schema v1) carries public MCP rows only
# ---------------------------------------------------------------------------


async def test_a_rest_row_is_not_exported(pub, writer, hits, client):
    hits.ids = [pub.d1.id]
    assert (await _search(client, pub.ctx, pub.key1)).status_code == 200
    (rest,) = await _wait_events(pub, 1)
    await writer.stop(timeout=5)
    async with AsyncSession(pub.db.bind) as s:
        mcp_event_id = uuid4()
        mcp = PublicMcpUsageEvent(
            event_id=mcp_event_id,
            occurred_at=rest.occurred_at,
            kind="tool_call",
            endpoint_id=pub.endpoint,
            workspace_id=pub.ws.id,
            context_id=pub.ctx,
            resource_pk=pub.resource_pk,
            resource_id=pub.slug,
            tool="recall",
            outcome="success",
            era="session",
            duration_ms=2,
            route="mcp",
            caller="anonymous",
        )
        s.add(mcp)
        await s.commit()
    async with AsyncSession(pub.db.bind) as s:
        page = await read_usage_events_page(s, workspace_id=pub.ws.id, limit=1000)
    exported = {e.event_id for e in page.events}
    assert mcp_event_id in exported
    assert rest.event_id not in exported


# ---------------------------------------------------------------------------
# Through the owner statistics (#3035)
# ---------------------------------------------------------------------------


async def _owner_stats(pub) -> dict:
    from auth.dependencies import get_user_from_api_key_or_session

    async def _user() -> dict:
        return {
            "user_id": pub.owner,
            "email": f"{pub.owner}@example.test",
            "role": "user",
            "current_workspace_id": pub.ws_id,
        }

    app.dependency_overrides[get_user_from_api_key_or_session] = _user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get(f"/api/v1/resources/{pub.slug}/public-mcp/usage-stats")
    finally:
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_owner_statistics_split_routes_callers_and_count_visitors(
    pub, writer, hits, client, monkeypatch
):
    from datetime import timedelta

    from api.routes import public_search as route
    from services.public_mcp_usage_events import (
        UsageEventInput,
        build_usage_event_row,
        visitor_key,
    )
    from utils.datetime import utcnow
    from utils.exceptions import RateLimitError

    hits.ids = [pub.d1.id]
    for _ in range(2):
        response = await _search(client, pub.ctx, pub.key1, visitor="same-value")
        assert response.status_code == 200, response.text
    real_bucket = route.check_bound_key_rate_limit

    async def refused(key_id: int, limit: int) -> None:
        raise RateLimitError("over", retry_after=60, reason="key_limit")

    monkeypatch.setattr(route, "check_bound_key_rate_limit", refused)
    assert (await _search(client, pub.ctx, pub.key1, visitor="refused-value")).status_code == 429
    monkeypatch.setattr(route, "check_bound_key_rate_limit", real_bucket)
    await _wait_events(pub, 3)
    await writer.stop(timeout=5)

    key = get_settings().audit_hmac_key
    now = utcnow()
    yesterday = now.date() - timedelta(days=1)
    other_endpoint = uuid4()
    pub.endpoints.append(other_endpoint)
    mcp_row = build_usage_event_row(
        UsageEventInput(
            endpoint_id=pub.endpoint,
            workspace_id=pub.ws_id,
            context_id=pub.ctx,
            resource_pk=pub.resource_pk,
            resource_id=pub.slug,
            tool="recall",
            outcome="success",
            era="session",
            started_at=now,
            duration_ms=3,
        ),
        key=key,
        session_details_enabled=False,
    ).row
    async with AsyncSession(pub.db.bind) as s:
        await s.execute(PublicMcpUsageEvent.__table__.insert().values(mcp_row))
        await s.execute(
            PublicMcpDailyVisitor.__table__.insert().values(
                [
                    {
                        "endpoint_id": pub.endpoint,
                        "day": yesterday,
                        "visitor_key": visitor_key(pub.endpoint, "same-value", yesterday, key),
                        "workspace_id": pub.ws_id,
                    },
                    {
                        "endpoint_id": other_endpoint,
                        "day": now.date(),
                        "visitor_key": visitor_key(other_endpoint, "same-value", now.date(), key),
                        "workspace_id": pub.ws_id,
                    },
                ]
            )
        )
        await s.commit()

    body = await _owner_stats(pub)
    totals = body["totals"]
    assert totals["calls_by_route"] == {"mcp": 1, "rest": 3}
    assert totals["calls_by_caller"] == {"anonymous": 1, "bound_key": 3}
    assert totals["searches_by_route"] == {"mcp": 1, "rest": 3}
    # The refused search is a search, not an answered one.
    assert totals["searches_answered_by_route"] == {"mcp": 1, "rest": 2}
    assert totals["search_outcomes"]["rate_limited"] == 1
    days = body["days"]
    assert (days[-2]["visitors"], days[-1]["visitors"]) == (1, 1)
    assert body["rest_recording_enabled"] is True
    assert body["visitors"] == {"status": "ok", "counting_enabled": True}
