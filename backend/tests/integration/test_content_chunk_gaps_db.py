"""#2107 content chunk gaps against a real Postgres.

The stamp (throttled, cursor reset, ``updated_at`` kept), the reconciler's
selection (settled stamps of live, stored-on contexts that are active now;
an inactive context keeps its stamp), its re-queue (only the rows whose first
chunk point is missing or of another text, with the D4 backfill's fences),
the keyset cursor, the fenced clear, and the flag change that clears the
stamp. The vector store is patched: the chunk point hashes come from the
test. Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from models.auth import Context, Workspace
from models.config import ContextSearchConfig
from models.memory import Memory
from models.schemas import ContextSearchConfigUpdate
from repositories.config_repository import ContextSearchConfigRepository
from services import content_chunk_gaps as gaps
from services import content_chunk_policy as policy
from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS, content_hash
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "chunk-gaps-it"
LONG = "z" * (CONTENT_CHUNK_MIN_CONTENT_CHARS + 10)
SHORT = "z" * (CONTENT_CHUNK_MIN_CONTENT_CHARS - 1)
_EDITED = datetime(2026, 2, 3, 4, 5, 6)
_CFG_UPDATED = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _qdrant(monkeypatch):
    monkeypatch.setattr(gaps, "chunk_points_supported", lambda: True)
    monkeypatch.setattr(policy, "chunk_points_supported", lambda: True)
    monkeypatch.setattr(gaps, "RECONCILE_CONTEXTS_PER_TICK", 1000)
    monkeypatch.setattr(gaps, "STAMPED_READ_LIMIT", 100_000)
    monkeypatch.setattr(gaps, "RECONCILE_GLOBAL_BACKLOG_LIMIT", 100_000)
    monkeypatch.setattr(gaps, "_rotation", gaps._Rotation())


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(
        policy, "get_settings", lambda: SimpleNamespace(content_chunks_require_plan=True)
    )


async def _workspace(db, plan: str = "free") -> UUID:
    ws = Workspace(
        id=uuid4(),
        name=f"gap-ws-{uuid4().hex[:8]}",
        plan_name=plan,
        owner_user_id=_USER,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    return ws.id


async def _context(db, ws_id: UUID, *, on: bool = True, deleted: bool = False) -> UUID:
    ctx = Context(
        id=uuid4(),
        workspace_id=ws_id,
        name=f"gap-{uuid4().hex[:8]}",
        created_by=_USER,
        deleted_at=func.now() if deleted else None,
    )
    db.add(ctx)
    await db.flush()
    db.add(
        ContextSearchConfig(
            context_id=ctx.id,
            content_chunks_enabled=on,
            content_chunks_enabled_at=func.now() if on else None,
        )
    )
    await db.flush()
    await db.execute(
        update(ContextSearchConfig)
        .where(ContextSearchConfig.context_id == ctx.id)
        .values(updated_at=_CFG_UPDATED)
    )
    return ctx.id


def _memory(ws_id, ctx_id, *, content=LONG, status="success", **extra) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ws_id,
        context_id=ctx_id,
        user_id=_USER,
        summary="s",
        content=content,
        type="note",
        scope="working",
        client="test",
        created_at=utcnow(),
        updated_at=_EDITED,
        embedding_status=status,
        **extra,
    )


async def _age_stamp(db, ctx_id: UUID, seconds: int = 3600) -> datetime:
    """Stamp the context as if the gap were ``seconds`` old (past the settle time)."""
    stamp = datetime.now(UTC) - timedelta(seconds=seconds)
    await db.execute(
        update(ContextSearchConfig)
        .where(ContextSearchConfig.context_id == ctx_id)
        .values(content_chunks_gap_at=stamp, updated_at=ContextSearchConfig.updated_at)
    )
    await db.commit()
    return stamp


async def _config(db, ctx_id: UUID):
    return (
        await db.execute(
            select(
                ContextSearchConfig.content_chunks_gap_at,
                ContextSearchConfig.content_chunks_gap_cursor,
                ContextSearchConfig.updated_at,
            )
            .where(ContextSearchConfig.context_id == ctx_id)
            .execution_options(populate_existing=True)
        )
    ).one()


async def _statuses(db, ids: dict[str, UUID]) -> dict[str, str]:
    rows = await db.execute(
        select(Memory.id, Memory.embedding_status)
        .where(Memory.id.in_(list(ids.values())))
        .execution_options(populate_existing=True)
    )
    by_id = dict(rows.all())
    return {label: by_id[memory_id] for label, memory_id in ids.items()}


def _hashes(mapping: dict[UUID, str | None]):
    async def fake(memory_ids, collection_name):
        return {str(m): mapping[m] for m in memory_ids if m in mapping}

    return fake


# --- the stamp ----------------------------------------------------------------


async def test_the_stamp_is_throttled_resets_the_cursor_and_keeps_updated_at(db_session):
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    await db_session.commit()

    assert await gaps.record_content_chunk_gap(db_session, ctx, reason="t") is True
    await db_session.commit()
    first = await _config(db_session, ctx)
    assert first.content_chunks_gap_at is not None
    assert first.updated_at == _CFG_UPDATED

    # Within the refresh: no write.
    assert await gaps.record_content_chunk_gap(db_session, ctx, reason="t") is False
    await db_session.commit()
    assert (await _config(db_session, ctx)).content_chunks_gap_at == first.content_chunks_gap_at

    # Older than the refresh: written again, and the cursor starts over.
    old = await _age_stamp(db_session, ctx, seconds=gaps.GAP_STAMP_REFRESH_SECONDS + 5)
    await db_session.execute(
        update(ContextSearchConfig)
        .where(ContextSearchConfig.context_id == ctx)
        .values(content_chunks_gap_cursor=uuid4(), updated_at=ContextSearchConfig.updated_at)
    )
    await db_session.commit()
    assert await gaps.record_content_chunk_gap(db_session, ctx, reason="t") is True
    await db_session.commit()
    after = await _config(db_session, ctx)
    assert after.content_chunks_gap_at > old
    assert after.content_chunks_gap_cursor is None
    assert after.updated_at == _CFG_UPDATED


# --- the reconciler -----------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session):
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    rows = {
        "missing": _memory(ws, ctx),
        "stale": _memory(ws, ctx),
        "current": _memory(ws, ctx),
        "no_hash": _memory(ws, ctx),
        "short": _memory(ws, ctx, content=SHORT),
        "pending": _memory(ws, ctx, status="pending"),
        "failed": _memory(ws, ctx, status="failed"),
        "deleted": _memory(ws, ctx, deleted_at=utcnow()),
        "resource": _memory(ws, ctx, details={"resource_id": "docs"}),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    ids = {label: m.id for label, m in rows.items()}
    hashes = {
        ids["stale"]: "another-text",
        ids["current"]: content_hash(LONG),
        ids["no_hash"]: None,
    }
    return ws, ctx, ids, hashes


async def test_an_active_context_requeues_only_rows_without_current_chunks(db_session, world):
    _ws, ctx, ids, hashes = world
    await _age_stamp(db_session, ctx)
    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)) as read:
        out = await gaps.reconcile_content_chunk_gaps(db_session)

    statuses = await _statuses(db_session, ids)
    assert {k for k, v in statuses.items() if v == "pending"} == {"missing", "stale", "pending"}
    assert statuses["failed"] == "failed"
    assert out.requeued == 2 and out.cleared >= 1
    # Read from the collection the context is served from; only the D4 rows.
    asked = set(read.await_args.args[0])
    assert asked == {ids["missing"], ids["stale"], ids["current"], ids["no_hash"]}
    config = await _config(db_session, ctx)
    assert (config.content_chunks_gap_at, config.content_chunks_gap_cursor) == (None, None)
    assert config.updated_at == _CFG_UPDATED
    # The edit cue of the re-queued rows is not written.
    edited = await db_session.scalar(
        select(Memory.updated_at)
        .where(Memory.id == ids["missing"])
        .execution_options(populate_existing=True)
    )
    assert edited == _EDITED

    # Idempotent: a second tick has nothing stamped for this context.
    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)) as read:
        await gaps.reconcile_content_chunk_gaps(db_session)
    read_ids = {m for call in read.await_args_list for m in call.args[0]}
    assert not read_ids & set(ids.values())


async def test_an_unsettled_stamp_waits(db_session, world):
    _ws, ctx, ids, hashes = world
    await gaps.record_content_chunk_gap(db_session, ctx, reason="t")
    await db_session.commit()
    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)):
        await gaps.reconcile_content_chunk_gaps(db_session)
    assert (await _statuses(db_session, ids))["missing"] == "success"
    assert (await _config(db_session, ctx)).content_chunks_gap_at is not None


async def test_an_inactive_context_keeps_its_stamp(db_session, world, gated):
    _ws, ctx, ids, hashes = world  # a free plan: no content_chunks feature
    stamp = await _age_stamp(db_session, ctx)
    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)) as read:
        await gaps.reconcile_content_chunk_gaps(db_session)
    read.assert_not_awaited()
    assert (await _statuses(db_session, ids))["missing"] == "success"
    assert (await _config(db_session, ctx)).content_chunks_gap_at == stamp


async def test_the_upgrade_that_makes_it_active_closes_the_gap(db_session, world, gated):
    ws, ctx, ids, hashes = world
    await _age_stamp(db_session, ctx)
    await db_session.execute(update(Workspace).where(Workspace.id == ws).values(plan_name="basic"))
    await db_session.commit()
    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)):
        await gaps.reconcile_content_chunk_gaps(db_session)
    assert (await _statuses(db_session, ids))["missing"] == "pending"
    assert (await _config(db_session, ctx)).content_chunks_gap_at is None


async def test_stored_off_and_deleted_contexts_are_not_read(db_session):
    ws = await _workspace(db_session)
    off = await _context(db_session, ws, on=False)
    gone = await _context(db_session, ws, deleted=True)
    db_session.add_all([_memory(ws, off), _memory(ws, gone)])
    await db_session.commit()
    for ctx in (off, gone):
        await _age_stamp(db_session, ctx)
    picked = {row[0] for row in await gaps._stamped_contexts(db_session, None)}
    await db_session.commit()
    assert off not in picked and gone not in picked


async def test_the_cursor_pages_through_and_the_clear_is_fenced(db_session, world, monkeypatch):
    _ws, ctx, ids, hashes = world
    monkeypatch.setattr(gaps, "RECONCILE_SCAN_BATCH", 2)
    stamp = await _age_stamp(db_session, ctx)
    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)):
        await gaps.reconcile_content_chunk_gaps(db_session)
    first = await _config(db_session, ctx)
    assert first.content_chunks_gap_at == stamp
    assert first.content_chunks_gap_cursor is not None

    # A new gap (the context went inactive again) moves the stamp: the next
    # cursor write and the clear are fenced on the stamp they read, and miss.
    moved = await gaps._move_stamp(
        db_session, ctx, stamp - timedelta(seconds=1), cursor=None, clear=True
    )
    assert moved is False
    assert (await _config(db_session, ctx)).content_chunks_gap_at == stamp

    for _ in range(3):
        with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)):
            await gaps.reconcile_content_chunk_gaps(db_session)
    statuses = await _statuses(db_session, ids)
    assert statuses["missing"] == "pending" and statuses["stale"] == "pending"
    assert statuses["current"] == "success" and statuses["no_hash"] == "success"
    assert (await _config(db_session, ctx)).content_chunks_gap_at is None


async def test_a_backlog_defers_the_context(db_session, world, monkeypatch):
    _ws, ctx, ids, hashes = world
    monkeypatch.setattr(gaps, "RECONCILE_BACKLOG_LIMIT", 1)  # "pending" is one
    stamp = await _age_stamp(db_session, ctx)
    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes(hashes)) as read:
        out = await gaps.reconcile_content_chunk_gaps(db_session)
    read.assert_not_awaited()
    assert out.deferred >= 1
    assert (await _statuses(db_session, ids))["missing"] == "success"
    assert (await _config(db_session, ctx)).content_chunks_gap_at == stamp


async def test_turning_the_flag_off_or_on_clears_the_stamp(db_session, world):
    _ws, ctx, _ids, _hashes = world
    repo = ContextSearchConfigRepository(db_session)
    base = ContextSearchConfigUpdate(
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
    )
    with patch.object(repo, "_purge_chunk_points", AsyncMock()):
        for on in (False, True):
            await _age_stamp(db_session, ctx)
            await repo.update(ctx, base.model_copy(update={"content_chunks_enabled": on}))
            assert (await _config(db_session, ctx)).content_chunks_gap_at is None
        # An edit that does not change the flag keeps it.
        stamp = await _age_stamp(db_session, ctx)
        await repo.update(ctx, base.model_copy(update={"fetch_factor": 4}))
        assert (await _config(db_session, ctx)).content_chunks_gap_at == stamp


async def test_the_stamp_survives_the_callers_rollback(db_session):
    """Committed on its own session: no row lock held for the caller's
    transaction, and a caller that rolls back keeps the stamp."""
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    await db_session.commit()
    assert await gaps.record_content_chunk_gap(db_session, ctx, reason="t") is True
    await db_session.rollback()
    assert (await _config(db_session, ctx)).content_chunks_gap_at is not None


async def test_inactive_stamps_do_not_starve_an_active_one(db_session, gated, monkeypatch):
    """The read goes round all stamped contexts, a few at a time."""
    monkeypatch.setattr(gaps, "STAMPED_READ_LIMIT", 2)
    free_ws = await _workspace(db_session)  # no content_chunks feature: inactive
    inactive = [await _context(db_session, free_ws) for _ in range(4)]
    paid_ws = await _workspace(db_session, plan="basic")
    active = await _context(db_session, paid_ws)
    row = _memory(paid_ws, active)
    db_session.add(row)
    await db_session.commit()
    for ctx in [*inactive, active]:
        await _age_stamp(db_session, ctx)

    with patch.object(gaps, "chunk_points_content_hashes", side_effect=_hashes({})):
        for _ in range(200):
            await gaps.reconcile_content_chunk_gaps(db_session)
            if (await _config(db_session, active)).content_chunks_gap_at is None:
                break
    assert (await _config(db_session, active)).content_chunks_gap_at is None
    assert (await _statuses(db_session, {"row": row.id}))["row"] == "pending"
    for ctx in inactive:
        assert (await _config(db_session, ctx)).content_chunks_gap_at is not None


# --- the one-time startup seed --------------------------------------------------


async def _clear_marker(db):
    from sqlalchemy import delete

    from models.config import ConfigOverride

    await db.execute(delete(ConfigOverride).where(ConfigOverride.key == gaps.GAP_SEED_MARKER_KEY))
    await db.commit()


async def _marker_exists(db) -> bool:
    from models.config import ConfigOverride

    return (
        await db.scalar(
            select(func.count())
            .select_from(ConfigOverride)
            .where(ConfigOverride.key == gaps.GAP_SEED_MARKER_KEY)
        )
    ) == 1


async def test_the_seed_without_the_gate_stamps_and_records_nothing(db_session):
    await _clear_marker(db_session)
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    await db_session.commit()
    assert await gaps.seed_content_chunk_gaps(db_session) is None
    assert (await _config(db_session, ctx)).content_chunks_gap_at is None
    assert not await _marker_exists(db_session)


async def test_the_seed_with_the_gate_stamps_once(db_session, gated):
    await _clear_marker(db_session)
    try:
        ws = await _workspace(db_session)
        on, off = await _context(db_session, ws), await _context(db_session, ws, on=False)
        stamped_before = await _context(db_session, ws)
        await db_session.commit()
        kept = await _age_stamp(db_session, stamped_before)

        assert (await gaps.seed_content_chunk_gaps(db_session) or 0) >= 1
        assert (await _config(db_session, on)).content_chunks_gap_at is not None
        assert (await _config(db_session, off)).content_chunks_gap_at is None
        # An existing stamp (and its walk) is left as it was.
        assert (await _config(db_session, stamped_before)).content_chunks_gap_at == kept
        assert (await _config(db_session, on)).updated_at == _CFG_UPDATED
        assert await _marker_exists(db_session)

        # A restart: the marker is there, nothing is stamped again.
        await _move_off(db_session, on)
        assert await gaps.seed_content_chunk_gaps(db_session) is None
        assert (await _config(db_session, on)).content_chunks_gap_at is None
    finally:
        await _clear_marker(db_session)


async def _move_off(db, ctx_id: UUID) -> None:
    """Clear a stamp, as a finished reconcile pass would."""
    await db.execute(
        update(ContextSearchConfig)
        .where(ContextSearchConfig.context_id == ctx_id)
        .values(content_chunks_gap_at=None, updated_at=ContextSearchConfig.updated_at)
    )
    await db.commit()
