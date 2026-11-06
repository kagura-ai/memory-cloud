"""The commit-safe export cursor of the public MCP usage events, on Postgres (#2678).

The cursor publishes rows in ``(txid, id)`` order and only those whose
inserting transaction is below the snapshot's ``xmin`` horizon. These tests
hold one transaction open while another commits, the case a bigserial-only
cursor gets wrong.

The horizon is server-wide: a write transaction open anywhere on the server
(another test process on a shared development database) holds it back. Rows
are then late, never lost, so the tests wait for their own rows to be sealed
before they read (``_wait_sealed``) and assert ``horizon <= txid`` rather
than equality.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.public_mcp_usage import PublicMcpUsageEvent, PublicMcpUsageRetention
from services.public_mcp_usage_events import (
    PublicMcpUsageEventV1,
    PublicMcpUsageGapV1,
    UsageCursor,
    UsageEventInput,
    build_gap_row,
    build_usage_event_row,
    decode_cursor,
    read_usage_events_page,
)
from utils.exceptions import CursorExpiredError

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = "k" * 40
WORKSPACE = UUID("7a000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE = UUID("7a000000-0000-4000-8000-000000000002")
RESOURCE = UUID("7a000000-0000-4000-8000-0000000000aa")


def _row(workspace_id: UUID = WORKSPACE, resource_pk: UUID = RESOURCE, **overrides) -> dict:
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
        "duration_ms": 5,
    }
    values.update(overrides)
    return build_usage_event_row(
        UsageEventInput(**values), key=KEY, session_details_enabled=False
    ).row


@pytest_asyncio.fixture(loop_scope="session")
async def sessions(async_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        await s.execute(delete(PublicMcpUsageEvent))
        await s.execute(delete(PublicMcpUsageRetention))
        await s.execute(insert(PublicMcpUsageRetention).values(id=1))
        await s.commit()
    yield maker
    async with maker() as s:
        await s.execute(delete(PublicMcpUsageEvent))
        await s.execute(delete(PublicMcpUsageRetention))
        await s.commit()


async def _horizon(maker) -> int:
    async with maker() as s:
        return int(
            (
                await s.execute(
                    text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint")
                )
            ).scalar_one()
        )


async def _wait_sealed(maker, txid: int, timeout: float = 60.0) -> None:
    """Wait until every transaction up to ``txid`` has finished server-wide."""
    deadline = asyncio.get_running_loop().time() + timeout
    while await _horizon(maker) <= txid:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"a transaction on the server held the horizon below {txid}")
        await asyncio.sleep(0.05)


async def _insert(maker, *rows: dict, wait: bool = True) -> list[tuple[int, int]]:
    async with maker() as s:
        result = await s.execute(
            insert(PublicMcpUsageEvent)
            .values(list(rows))
            .returning(PublicMcpUsageEvent.txid, PublicMcpUsageEvent.id)
        )
        keys = [(r.txid, r.id) for r in result.all()]
        await s.commit()
    if wait:
        await _wait_sealed(maker, max(t for t, _ in keys))
    return keys


async def _page(maker, cursor: str | None = None, limit: int = 100, **kw):
    async with maker() as s:
        return await read_usage_events_page(
            s, workspace_id=kw.pop("workspace_id", WORKSPACE), cursor=cursor, limit=limit, **kw
        )


async def test_a_late_commit_is_never_skipped(sessions):
    """(a) + (b) + (f): A takes the smaller id and commits after B."""
    a_row, b_row = _row(), _row()
    async with sessions() as session_a:
        a_insert = await session_a.execute(
            insert(PublicMcpUsageEvent)
            .values(a_row)
            .returning(PublicMcpUsageEvent.txid, PublicMcpUsageEvent.id)
        )
        a_txid, a_id = a_insert.one()
        # B commits while A is still open; B's id is larger.
        [(b_txid, b_id)] = await _insert(sessions, b_row, wait=False)
        assert b_id > a_id
        assert a_txid < b_txid

        # (a) nothing is sealed while A runs: the horizon is at most A's
        # transaction (lower if something else on the server is running).
        page = await _page(sessions)
        assert page.events == []
        assert page.horizon <= a_txid
        # (f) B is committed but not sealed yet (A is visible to no one).
        assert page.unsealed_events == 1
        # The empty page moves the cursor up to the sealed horizon, still
        # below A: A has not finished, so it cannot be below the horizon.
        early = page.next_cursor
        assert decode_cursor(early).txid < a_txid

        # (b) the bigserial-only reader: it sees B, the highest visible id,
        # and would continue with "id > b_id".
        async with sessions() as reader:
            seen_ids = (
                (await reader.execute(text("SELECT id FROM public_mcp_usage_events ORDER BY id")))
                .scalars()
                .all()
            )
        assert seen_ids == [b_id]

        await session_a.commit()
    await _wait_sealed(sessions, b_txid)

    # The bigserial reader's next read misses A for good.
    async with sessions() as reader:
        after_b = (
            (
                await reader.execute(
                    text("SELECT id FROM public_mcp_usage_events WHERE id > :i"), {"i": b_id}
                )
            )
            .scalars()
            .all()
        )
    assert after_b == []
    # The horizon cursor publishes both, A first (its transaction is older).
    page = await _page(sessions)
    assert [e.event_id for e in page.events] == [a_row["event_id"], b_row["event_id"]]
    assert page.unsealed_events == 0
    assert page.has_more is False
    assert decode_cursor(page.next_cursor) >= UsageCursor(b_txid, b_id)
    # The cursor the empty page handed out while A was open misses neither.
    from_early = await _page(sessions, cursor=early)
    assert [e.event_id for e in from_early.events] == [a_row["event_id"], b_row["event_id"]]


async def test_c_replay_is_idempotent_and_d_chaining_equals_one_page(sessions):
    rows = [_row() for _ in range(7)]
    await _insert(sessions, *rows[:3])
    await _insert(sessions, *rows[3:5])
    await _insert(sessions, *rows[5:])

    whole = await _page(sessions)
    assert [e.event_id for e in whole.events] == [r["event_id"] for r in rows]
    assert whole.has_more is False

    seen = []
    cursor = None
    pages = 0
    while True:
        page = await _page(sessions, cursor=cursor, limit=3)
        replay = await _page(sessions, cursor=cursor, limit=3)
        assert [e.event_id for e in replay.events] == [e.event_id for e in page.events]
        if page.has_more:
            # A last page's cursor follows the server-wide horizon, which
            # other transactions on the server move between the two reads.
            assert replay.next_cursor == page.next_cursor
        seen.extend(e.event_id for e in page.events)
        cursor = page.next_cursor
        pages += 1
        if not page.has_more:
            break
    assert pages == 3
    assert seen == [r["event_id"] for r in rows]
    # An exhausted cursor keeps its position.
    tail = await _page(sessions, cursor=cursor)
    assert tail.events == []
    assert decode_cursor(tail.next_cursor) >= decode_cursor(cursor)


async def test_scope_workspace_resource_and_gap_rows(sessions):
    other_resource = uuid4()
    mine = _row()
    mine_other_resource = _row(resource_pk=other_resource)
    foreign = _row(workspace_id=OTHER_WORKSPACE)
    start = datetime(2026, 11, 6, 9, 0)
    gap = build_gap_row(
        dropped_count=4, started_at=start, ended_at=start + timedelta(seconds=5), reason="shutdown"
    )
    await _insert(sessions, gap, mine, mine_other_resource, foreign)

    workspace_page = await _page(sessions)
    assert [e.event_id for e in workspace_page.events] == [
        gap["event_id"],
        mine["event_id"],
        mine_other_resource["event_id"],
    ]
    assert isinstance(workspace_page.events[0], PublicMcpUsageGapV1)
    assert isinstance(workspace_page.events[1], PublicMcpUsageEventV1)

    resource_page = await _page(sessions, resource_pk=RESOURCE)
    assert [e.event_id for e in resource_page.events] == [gap["event_id"], mine["event_id"]]

    foreign_page = await _page(sessions, workspace_id=OTHER_WORKSPACE)
    assert [e.event_id for e in foreign_page.events] == [gap["event_id"], foreign["event_id"]]

    # The wire form: UTC with Z, never the storage id or txid.
    wire = workspace_page.events[1].model_dump(mode="json")
    assert wire["occurred_at"].endswith("Z")
    assert "id" not in wire
    assert "txid" not in wire


async def test_e_cursor_below_the_purge_mark_is_expired(sessions):
    rows = [_row() for _ in range(4)]
    keys = await _insert(sessions, *rows)
    first_two = await _page(sessions, limit=2)
    cursor_after_two = first_two.next_cursor
    first_one = await _page(sessions, limit=1)

    # A purge of the first two rows advances the mark to the second row.
    async with sessions() as s:
        await s.execute(
            delete(PublicMcpUsageEvent).where(PublicMcpUsageEvent.id.in_([keys[0][1], keys[1][1]]))
        )
        await s.execute(
            text(
                "UPDATE public_mcp_usage_retention SET purged_through_txid = :t, "
                "purged_through_id = :i WHERE id = 1"
            ),
            {"t": keys[1][0], "i": keys[1][1]},
        )
        await s.commit()

    # Below the mark: 410 with where to restart.
    with pytest.raises(CursorExpiredError) as exc:
        await _page(sessions, cursor=first_one.next_cursor)
    assert exc.value.status_code == 410
    assert exc.value.error_code == "cursor_expired"
    oldest = exc.value.details["oldest_cursor"]
    assert decode_cursor(oldest) == UsageCursor(*keys[1])

    # At the mark: fine, and it continues with the third row.
    at_mark = await _page(sessions, cursor=cursor_after_two)
    assert [e.event_id for e in at_mark.events] == [rows[2]["event_id"], rows[3]["event_id"]]
    restart = await _page(sessions, cursor=oldest)
    assert [e.event_id for e in restart.events] == [rows[2]["event_id"], rows[3]["event_id"]]
    # No cursor is never expired.
    fresh = await _page(sessions)
    assert [e.event_id for e in fresh.events] == [rows[2]["event_id"], rows[3]["event_id"]]
    assert fresh.oldest_retained_at is not None


async def _purge_mark_to(maker, txid: int, row_id: int) -> None:
    async with maker() as s:
        await s.execute(
            text(
                "UPDATE public_mcp_usage_retention SET purged_through_txid = :t, "
                "purged_through_id = :i WHERE id = 1"
            ),
            {"t": txid, "i": row_id},
        )
        await s.commit()


async def test_a_caught_up_reader_is_not_expired_by_a_purge_elsewhere(sessions):
    """An empty page advances the cursor, so a purge of rows it passed is no hole."""
    first = await _page(sessions)  # a new reader of a scope with no events
    assert first.events == []
    foreign = await _insert(sessions, _row(workspace_id=OTHER_WORKSPACE))
    polled = await _page(sessions, cursor=first.next_cursor)
    assert polled.events == []

    # Retention purges the other workspace's row: the mark passes it.
    async with sessions() as s:
        await s.execute(delete(PublicMcpUsageEvent))
        await s.commit()
    await _purge_mark_to(sessions, *foreign[0])

    after = await _page(sessions, cursor=polled.next_cursor)  # no cursor_expired
    assert after.events == []
    mine = _row()
    await _insert(sessions, mine)
    page = await _page(sessions, cursor=after.next_cursor)
    assert [e.event_id for e in page.events] == [mine["event_id"]]


async def test_page_query_uses_the_txid_index(sessions):
    await _insert(sessions, *[_row() for _ in range(3)])
    async with sessions() as s:
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in (
                await s.execute(
                    text(
                        "EXPLAIN SELECT id FROM public_mcp_usage_events "
                        "WHERE txid < pg_snapshot_xmin(pg_current_snapshot())::text::bigint "
                        "AND (txid, id) > (0, 0) ORDER BY txid, id LIMIT 101"
                    )
                )
            ).all()
        )
    assert "ix_pmue_txid_id" in plan
