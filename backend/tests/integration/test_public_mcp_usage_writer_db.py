"""The usage event writer against Postgres: the INSERT shapes it sends (#2678)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.public_mcp_usage import PublicMcpDocKey, PublicMcpUsageEvent
from services.public_mcp_usage_events import UsageEventInput, doc_key
from services.public_mcp_usage_writer import PublicMcpUsageWriter

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = "k" * 40


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(delete(PublicMcpUsageEvent))
        await s.execute(delete(PublicMcpDocKey))
        await s.commit()
    yield factory
    async with factory() as s:
        await s.execute(delete(PublicMcpUsageEvent))
        await s.execute(delete(PublicMcpDocKey))
        await s.commit()


async def _resource(maker) -> tuple:
    workspace_id, resource_pk = uuid4(), uuid4()
    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO workspaces (id, name, owner_user_id, created_at) "
                "VALUES (:id, 'ws', 'owner-1', now())"
            ),
            {"id": workspace_id},
        )
        await s.execute(
            text(
                "INSERT INTO resources (id, workspace_id, resource_id, created_at) "
                "VALUES (:id, :ws, :slug, now())"
            ),
            {"id": resource_pk, "ws": workspace_id, "slug": f"catalog-{resource_pk.hex[:8]}"},
        )
        await s.commit()
    return workspace_id, resource_pk


def _event(workspace_id, resource_pk, **overrides) -> UsageEventInput:
    values = {
        "endpoint_id": uuid4(),
        "workspace_id": workspace_id,
        "context_id": uuid4(),
        "resource_pk": resource_pk,
        "resource_id": "catalog",
        "tool": "recall",
        "outcome": "success",
        "era": "session",
        "started_at": datetime.now(UTC),
        "duration_ms": 7,
        "result_count": 3,
        "doc_ids": ("doc-a", "doc-a", "doc-b"),
    }
    values.update(overrides)
    return UsageEventInput(**values)


async def test_writer_inserts_events_gaps_and_doc_keys(maker):
    workspace_id, resource_pk = await _resource(maker)
    writer = PublicMcpUsageWriter(
        session_factory=maker,
        queue_size=3,
        flush_interval=0.05,
        write_timeout=5.0,
        key=KEY,
        session_details_enabled=True,
    )
    for _ in range(2):
        writer.offer(_event(workspace_id, resource_pk, session_id="s-1", client_name="Ex/1"))
    # A resource deleted since the call: its keys are skipped, its event kept.
    writer.offer(_event(workspace_id, uuid4()))
    writer.offer(_event(workspace_id, resource_pk))  # dropped: queue_full
    writer.start()
    await writer.stop(timeout=5)

    async with maker() as s:
        rows = (
            (await s.execute(select(PublicMcpUsageEvent).order_by(PublicMcpUsageEvent.id)))
            .scalars()
            .all()
        )
        keys = (await s.execute(select(PublicMcpDocKey))).scalars().all()
    assert [r.kind for r in rows] == ["gap", "tool_call", "tool_call", "tool_call"]
    gap = rows[0]
    assert gap.gap_reason == "queue_full"
    assert gap.dropped_count == 1
    assert gap.workspace_id is None
    event = rows[1]
    assert event.txid == gap.txid  # one transaction
    assert event.documents == [
        {"doc_key": doc_key(resource_pk, "doc-a", KEY), "chunks": 2, "rank": 1},
        {"doc_key": doc_key(resource_pk, "doc-b", KEY), "chunks": 1, "rank": 3},
    ]
    assert len(event.session_key) == 32
    assert event.client_family == "ex1"
    assert event.event_id is not None
    assert {(k.resource_pk, k.resource_doc_id) for k in keys} == {
        (resource_pk, "doc-a"),
        (resource_pk, "doc-b"),
    }
    assert writer.health().last_flush_ok is True


class _CommitThenTimeOut:
    """Session factory whose first session commits and then reports a timeout."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.calls = 0

    def __call__(self) -> AsyncSession:
        self.calls += 1
        session = self._factory()
        if self.calls == 1:
            real_commit = session.commit

            async def commit() -> None:
                await real_commit()
                raise TimeoutError

            session.commit = commit  # type: ignore[method-assign]
        return session


async def test_retry_after_a_commit_that_landed_is_not_a_loss(maker):
    """A write that committed but timed out is retried without a loss or a gap."""
    workspace_id, resource_pk = await _resource(maker)
    factory = _CommitThenTimeOut(maker)
    writer = PublicMcpUsageWriter(
        session_factory=factory,
        queue_size=10,
        flush_interval=0.05,
        write_timeout=5.0,
        key=KEY,
        session_details_enabled=False,
        retry_backoff=0.0,
    )
    writer.offer(_event(workspace_id, resource_pk))
    writer.offer(_event(workspace_id, resource_pk, doc_ids=()))
    writer.start()
    await writer.stop(timeout=5)

    async with maker() as s:
        sql_nulls = (
            await s.execute(
                text("SELECT count(*) FROM public_mcp_usage_events WHERE documents IS NULL")
            )
        ).scalar_one()
        kinds = (
            (await s.execute(select(PublicMcpUsageEvent.kind).order_by(PublicMcpUsageEvent.id)))
            .scalars()
            .all()
        )
    assert factory.calls >= 2  # the batch was retried
    assert kinds == ["tool_call", "tool_call"]  # stored once, no gap row
    assert sql_nulls == 1  # no documents is SQL NULL, not JSON null
    health = writer.health()
    assert health.dropped_since_start == 0
    assert health.last_flush_ok is True


async def test_route_caller_and_visitors_reach_postgres_apart(maker):
    """#3035: route and caller stored (gap rows NULL); visitor rows in a
    transaction of their own, sorted, and a second write of a key is a no-op."""
    from models.public_mcp_usage import PublicMcpDailyVisitor
    from services.public_mcp_usage_events import visitor_key

    workspace_id, resource_pk = await _resource(maker)
    endpoint = uuid4()
    day = datetime.now(UTC)
    values = [f"v-{i}" for i in range(3)]

    def bound(value: str) -> UsageEventInput:
        return _event(
            workspace_id,
            resource_pk,
            endpoint_id=endpoint,
            era="stateless",
            route="rest",
            caller="bound_key",
            started_at=day,
            visitor_key=visitor_key(endpoint, value, day.date(), KEY),
        )

    def make_writer(queue_size: int) -> PublicMcpUsageWriter:
        return PublicMcpUsageWriter(
            session_factory=maker,
            queue_size=queue_size,
            flush_interval=0.05,
            write_timeout=5.0,
            key=KEY,
            session_details_enabled=False,
            visitor_flush_min_keys=3,
        )

    try:
        writer = make_writer(queue_size=4)
        for value in values:
            writer.offer(bound(value))
        writer.offer(_event(workspace_id, resource_pk, endpoint_id=endpoint))  # a public MCP call
        writer.offer(_event(workspace_id, resource_pk, endpoint_id=endpoint))  # dropped: a gap
        writer.start()
        await writer.stop(timeout=5)

        # The same values again, from a second writer: no new row, no error.
        again = make_writer(queue_size=10)
        for value in values:
            again.offer(bound(value))
        again.start()
        await again.stop(timeout=5)

        async with maker() as s:
            rows = (
                (await s.execute(select(PublicMcpUsageEvent).order_by(PublicMcpUsageEvent.id)))
                .scalars()
                .all()
            )
            visitors = (
                await s.execute(
                    text(
                        "SELECT visitor_key, xmin::text::bigint FROM public_mcp_daily_visitors "
                        "WHERE endpoint_id = :e ORDER BY ctid"
                    ),
                    {"e": endpoint},
                )
            ).all()
        gap = next(r for r in rows if r.kind == "gap")
        assert (gap.route, gap.caller) == (None, None)
        calls = [r for r in rows if r.kind == "tool_call"]
        assert sorted((r.route, r.caller) for r in calls) == sorted(
            [("rest", "bound_key")] * 6 + [("mcp", "anonymous")]
        )
        assert len(visitors) == 3
        keys = [v[0] for v in visitors]
        assert keys == sorted(keys), "inserted in key order"
        assert len({v[1] for v in visitors}) == 1, "one transaction"
        assert all(r.txid % 2**32 != visitors[0][1] for r in rows), "never with events"
    finally:
        async with maker() as s:
            await s.execute(
                delete(PublicMcpDailyVisitor).where(PublicMcpDailyVisitor.endpoint_id == endpoint)
            )
            # The workspace and its resource (and with it the doc keys).
            await s.execute(text("DELETE FROM resources WHERE id = :id"), {"id": resource_pk})
            await s.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id})
            await s.commit()
