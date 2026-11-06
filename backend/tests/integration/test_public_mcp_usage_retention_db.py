"""Retention of public MCP usage events and the purge mark, on Postgres (#2678)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import (
    PublicMcpDocKey,
    PublicMcpUsageEvent,
    PublicMcpUsageRetention,
)
from services.public_mcp_usage_events import UsageEventInput, build_usage_event_row
from tasks.public_mcp_usage_tasks import purge_public_mcp_usage_events
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = "k" * 40
RETENTION_DAYS = 90


@pytest.fixture(autouse=True)
def _retention(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_retention_days", RETENTION_DAYS)


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def clean() -> None:
        async with factory() as s:
            await s.execute(delete(PublicMcpUsageEvent))
            await s.execute(delete(PublicMcpUsageRetention))
            await s.execute(delete(PublicMcpDocKey))
            await s.execute(delete(PublicMcpEndpoint))
            await s.commit()

    await clean()
    async with factory() as s:
        await s.execute(insert(PublicMcpUsageRetention).values(id=1))
        await s.commit()
    yield factory
    await clean()


def _row(age_days: float) -> dict:
    started = utcnow() - timedelta(days=age_days)
    return build_usage_event_row(
        UsageEventInput(
            endpoint_id=uuid4(),
            workspace_id=uuid4(),
            context_id=uuid4(),
            resource_pk=uuid4(),
            resource_id="catalog",
            tool="recall",
            outcome="success",
            era="session",
            started_at=started,
            duration_ms=1,
        ),
        key=KEY,
        session_details_enabled=False,
    ).row


async def _insert(maker, rows: list[dict]) -> list[tuple[int, int]]:
    keys = []
    for row in rows:  # one transaction each: distinct txids
        async with maker() as s:
            result = await s.execute(
                insert(PublicMcpUsageEvent)
                .values(row)
                .returning(PublicMcpUsageEvent.txid, PublicMcpUsageEvent.id)
            )
            keys.append(tuple(result.one()))
            await s.commit()
    return keys


async def _mark(maker) -> tuple[int, int, int]:
    async with maker() as s:
        r = (await s.execute(select(PublicMcpUsageRetention))).scalar_one()
        return r.purged_through_txid, r.purged_through_id, r.purged_total


async def test_purge_in_batches_advances_the_mark_to_the_max_deleted(maker):
    old_keys = await _insert(maker, [_row(100 + i) for i in range(7)])
    await _insert(maker, [_row(1), _row(89)])

    async with maker() as s:
        deleted = await purge_public_mcp_usage_events(s, batch_size=3)
    assert deleted == 7

    async with maker() as s:
        left = (await s.execute(select(func.count()).select_from(PublicMcpUsageEvent))).scalar()
    assert left == 2
    t, i, total = await _mark(maker)
    assert (t, i) == max(old_keys)
    assert total == 7

    # Rerun: nothing more to delete, the mark stays.
    async with maker() as s:
        assert await purge_public_mcp_usage_events(s, batch_size=3) == 0
    assert await _mark(maker) == (t, i, 7)


async def test_mark_never_moves_back(maker):
    await _insert(maker, [_row(200)])
    async with maker() as s:
        await s.execute(
            text(
                "UPDATE public_mcp_usage_retention SET purged_through_txid = 9223372036854775000, "
                "purged_through_id = 5 WHERE id = 1"
            )
        )
        await s.commit()
    async with maker() as s:
        assert await purge_public_mcp_usage_events(s) == 1
    t, i, total = await _mark(maker)
    assert (t, i) == (9223372036854775000, 5)
    assert total == 1


async def test_missing_mark_row_is_created(maker):
    async with maker() as s:
        await s.execute(delete(PublicMcpUsageRetention))
        await s.commit()
    keys = await _insert(maker, [_row(120)])
    async with maker() as s:
        assert await purge_public_mcp_usage_events(s) == 1
    assert await _mark(maker) == (*keys[0], 1)


async def _published(maker, *, revoked_days_ago: float | None, live: bool) -> object:
    """A workspace / context / resource with endpoint rows as described."""
    workspace_id, context_id, resource_pk = uuid4(), uuid4(), uuid4()
    slug = f"cat-{resource_pk.hex[:8]}"
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
                "INSERT INTO contexts (id, workspace_id, name, created_at, is_private, is_public, "
                "resource_id) VALUES (:id, :ws, 'c', now(), false, true, :slug)"
            ),
            {"id": context_id, "ws": workspace_id, "slug": slug},
        )
        await s.execute(
            text(
                "INSERT INTO resources (id, workspace_id, resource_id, created_at) "
                "VALUES (:id, :ws, :slug, now())"
            ),
            {"id": resource_pk, "ws": workspace_id, "slug": slug},
        )
        common = {
            "workspace_id": workspace_id,
            "context_id": context_id,
            "resource_pk": resource_pk,
            "resource_id": slug,
            "public_name": "P",
            "created_by": "owner-1",
        }
        if revoked_days_ago is not None:
            await s.execute(
                insert(PublicMcpEndpoint).values(
                    **common, revoked_at=utcnow() - timedelta(days=revoked_days_ago)
                )
            )
        if live:
            await s.execute(insert(PublicMcpEndpoint).values(**common))
        await s.execute(
            insert(PublicMcpDocKey).values(
                resource_pk=resource_pk,
                doc_key="a" * 32,
                resource_doc_id="doc-a",
                first_seen_at=utcnow() - timedelta(days=400),
            )
        )
        await s.commit()
    return resource_pk


async def test_doc_keys_are_dropped_only_for_long_unpublished_resources(maker):
    live = await _published(maker, revoked_days_ago=300, live=True)
    long_gone = await _published(maker, revoked_days_ago=120, live=False)
    recently_gone = await _published(maker, revoked_days_ago=10, live=False)
    never_published = await _published(maker, revoked_days_ago=None, live=False)

    async with maker() as s:
        await purge_public_mcp_usage_events(s)
    async with maker() as s:
        kept = set((await s.execute(select(PublicMcpDocKey.resource_pk))).scalars().all())
    assert live in kept
    assert recently_gone in kept
    assert long_gone not in kept
    assert never_published not in kept
