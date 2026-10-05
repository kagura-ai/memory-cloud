"""``remember``'s ``tags_normalize`` / ``dedupe`` and ``remember_many`` on real Postgres (#1853)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from models.auth import Context, IdentityLink, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RememberRequest
from services.memory_service import (
    BatchCommittedError,
    BatchItemError,
    DedupeUnavailableError,
    DuplicateCandidateError,
    MemoryService,
)
from services.quota_service import DailyReservation, QuotaService
from services.supersede_dismissal import is_dismissed
from services.tag_resolution import clear_vocabulary_cache
from utils.datetime import utcnow
from utils.exceptions import QuotaExceededError


async def _scope(db, owner: str) -> tuple[Workspace, Context]:
    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"rw-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"rw-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return ws, ctx


def _req(summary: str, **over) -> RememberRequest:
    fields = {"summary": summary, "content": "content", "type": "note", "importance": 0.5}
    fields.update(over)
    return RememberRequest(**fields)


_RESERVED = DailyReservation(key="quota:test:memories:day", count=3)


@pytest.fixture
def quiet_write():
    """No embedding task, no Redis quota: the parts of remember() that are not under test."""
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(return_value=_RESERVED),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        yield quota


async def _count(db, ctx_id) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(Memory).where(Memory.context_id == ctx_id)
        )
    ).scalar_one()


# --------------------------------------------------------------- remember_many


@pytest.mark.asyncio
async def test_remember_many_writes_every_item_in_one_transaction(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    results = await MemoryService(db_session).remember_many(
        [_req("first conclusion"), _req("second conclusion"), _req("third conclusion")],
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    assert len(results) == 3 and len({r.memory_id for r in results}) == 3
    assert all(r.persistence is not None for r in results)
    assert await _count(db_session, ctx.id) == 3
    # #1873: one all-or-nothing reservation for the batch, on the context's workspace.
    quiet_write.reserve_memories_per_day.assert_awaited_once_with(ws.id, 3)
    quiet_write.check_memories_per_day.assert_not_awaited()  # not per item as well
    quiet_write.release_memories_per_day.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_many_rolls_everything_back_when_one_item_fails(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ctx_id = ctx.id  # the rollback below expires the ORM instance
    bad = _req("a time memory without a trigger", type="time")  # _apply_time_trigger refuses it
    with pytest.raises(BatchItemError) as info:
        await MemoryService(db_session).remember_many(
            [_req("first conclusion"), bad, _req("third conclusion")],
            user_id=owner,
            client="pytest",
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
        )
    assert info.value.index == 1
    assert isinstance(info.value.cause, ValueError)
    assert await _count(db_session, ctx_id) == 0
    # #1873: the rolled-back batch gives its reservation back.
    quiet_write.reserve_memories_per_day.assert_awaited_once()
    quiet_write.release_memories_per_day.assert_awaited_once_with(_RESERVED)


# -------------------------------------------------------------- tags_normalize


@pytest.mark.asyncio
async def test_tags_normalize_stores_the_established_spelling_and_says_so(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    for i in range(2):
        db_session.add(
            Memory(
                id=uuid4(),
                user_id=owner,
                workspace_id=ws.id,
                context_id=ctx.id,
                summary=f"existing {i}",
                content="c",
                type="note",
                importance=0.5,
                confidence=1.0,
                client="pytest",
                source="manual",
                source_type="manual",
                tags=["Dev-Environment", "python"],
                embedding_status="success",
            )
        )
    await db_session.commit()
    clear_vocabulary_cache()
    result = await MemoryService(db_session).remember(
        _req(
            "a new environment note",
            tags=["dev_environment", "Python", "troubleshoot", "brand-new"],
        ),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        tags_normalize=True,
    )
    row = (
        await db_session.execute(select(Memory).where(Memory.id == result.memory_id))
    ).scalar_one()
    # Mechanical variants map; 'troubleshoot' is not a spelling of anything stored; new stays new.
    assert row.tags == ["Dev-Environment", "python", "troubleshoot", "brand-new"]
    mapped = {h.subject: h.replacement for h in result.lint if h.code == "tag_normalized"}
    assert mapped == {"dev_environment": "Dev-Environment", "Python": "python"}


# ------------------------------------------------------------------------ dedupe


@pytest.mark.asyncio
async def test_dedupe_check_returns_the_candidate_and_writes_nothing(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    candidate = {"memory_id": str(uuid4()), "summary": "the same fact", "similarity": 0.93}
    with (
        patch.object(
            MemoryService, "_find_duplicate_candidate", new=AsyncMock(return_value=candidate)
        ),
        pytest.raises(DuplicateCandidateError) as info,
    ):
        await MemoryService(db_session).remember(
            _req("the same fact, again"),
            user_id=owner,
            client="pytest",
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
            dedupe="check",
        )
    assert info.value.candidate == candidate
    assert await _count(db_session, ctx.id) == 0
    assert quiet_write.check_memories_per_day.await_count == 0  # asked before the quota is charged


@pytest.mark.asyncio
async def test_dedupe_off_writes_a_wildcard_tombstone(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    result = await MemoryService(db_session).remember(
        _req("deliberately separate fact"),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        dedupe="off",
    )
    row = (
        await db_session.execute(select(Memory).where(Memory.id == result.memory_id))
    ).scalar_one()
    assert row.supersede_candidate["dismissed"]["memory_id"] == "*"
    assert is_dismissed(row.supersede_candidate, target_id=str(uuid4()), similarity=0.99)


@pytest.mark.asyncio
async def test_find_duplicate_candidate_applies_the_detection_threshold(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    existing = Memory(
        id=uuid4(),
        user_id=owner,
        workspace_id=ws.id,
        context_id=ctx.id,
        summary="JWT expiry caused 401",
        content="c",
        type="note",
        importance=0.5,
        confidence=1.0,
        client="pytest",
        source="manual",
        source_type="manual",
        embedding_status="success",
    )
    db_session.add(existing)
    await db_session.commit()
    embedder = MagicMock(embed=AsyncMock(return_value=[0.1, 0.2]))
    service = MemoryService(db_session)

    async def run(score):
        with (
            patch(
                "services.context_routing.resolve_context_routing",
                new=AsyncMock(return_value=("collection", embedder)),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=[{"id": str(existing.id), "score": score}]),
            ),
        ):
            return await service._find_duplicate_candidate(
                user_id=owner,
                workspace_id_str=str(ws.id),
                context_id_str=str(ctx.id),
                summary="JWT expiry caused a 401",
            )

    assert await run(0.84) is None
    assert await run(0.91) == {
        "memory_id": str(existing.id),
        "summary": "JWT expiry caused 401",
        "similarity": 0.91,
    }
    embedder.embed = AsyncMock(side_effect=RuntimeError("no key"))
    with pytest.raises(DedupeUnavailableError):
        await run(0.91)


# ------------------------------------------------------ inner review (#1853)


def _existing(owner, ws, ctx, summary="JWT expiry caused 401") -> Memory:
    return Memory(
        id=uuid4(),
        user_id=owner,
        workspace_id=ws.id,
        context_id=ctx.id,
        summary=summary,
        content="c",
        type="note",
        importance=0.5,
        confidence=1.0,
        client="pytest",
        source="manual",
        source_type="manual",
        embedding_status="success",
    )


@pytest.mark.asyncio
async def test_dedupe_check_skips_the_memory_the_request_supersedes(db_session, quiet_write):
    """The documented way out of a candidate is supersedes=<its id>; the check must
    not refuse that very write."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    old = _existing(owner, ws, ctx)
    db_session.add(old)
    await db_session.commit()
    candidate = {"memory_id": str(old.id), "summary": old.summary, "similarity": 0.95}
    with patch.object(
        MemoryService, "_find_duplicate_candidate", new=AsyncMock(return_value=candidate)
    ):
        result = await MemoryService(db_session).remember(
            _req("JWT expiry caused 401; fixed with refresh rotation", supersedes=old.id),
            user_id=owner,
            client="pytest",
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
            dedupe="check",
        )
    assert result.memory_id != old.id
    assert await _count(db_session, ctx.id) == 2


@pytest.mark.asyncio
async def test_atomic_check_runs_before_any_row_or_quota_charge(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ctx_id = ctx.id

    async def find(self, *, summary, **_):
        return (
            {"memory_id": str(uuid4()), "summary": "dup", "similarity": 0.9}
            if "dup" in summary
            else None
        )

    with (
        patch.object(MemoryService, "_find_duplicate_candidate", new=find),
        pytest.raises(BatchItemError) as info,
    ):
        await MemoryService(db_session).remember_many(
            [_req("first conclusion"), _req("a dup of something"), _req("third conclusion")],
            user_id=owner,
            client="pytest",
            current_context_id=ctx_id,
            current_workspace_id=ws.id,
            dedupe="check",
        )
    assert info.value.index == 1 and isinstance(info.value.cause, DuplicateCandidateError)
    assert quiet_write.check_memories_per_day.await_count == 0  # no prepare ran
    assert quiet_write.reserve_memories_per_day.await_count == 0  # nor the batch reservation
    assert await _count(db_session, ctx_id) == 0


@pytest.mark.asyncio
async def test_remember_many_reports_a_row_whose_post_commit_step_failed(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    with (
        patch.object(
            MemoryService,
            "_create_declared_links",
            new=AsyncMock(side_effect=[None, RuntimeError("edge store down"), None]),
        ),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()) as embed,
    ):
        results = await MemoryService(db_session).remember_many(
            [_req("first conclusion"), _req("second conclusion"), _req("third conclusion")],
            user_id=owner,
            client="pytest",
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
        )
    assert len(results) == 3 and len({r.memory_id for r in results}) == 3
    assert results[1].persistence is not None and results[1].lint == []
    assert await _count(db_session, ctx.id) == 3
    assert embed.call_count == 3  # the embedding task is scheduled before anything can fail


@pytest.mark.asyncio
async def test_tags_fold_within_one_request_too(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    clear_vocabulary_cache()
    result = await MemoryService(db_session).remember(
        _req("spelling variants in one write", tags=["Foo-Bar", "foo_bar", "FooBar"]),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        tags_normalize=True,
    )
    row = (
        await db_session.execute(select(Memory).where(Memory.id == result.memory_id))
    ).scalar_one()
    assert row.tags == ["Foo-Bar"]  # the first spelling wins
    assert sorted(h.subject for h in result.lint if h.code == "tag_normalized") == [
        "FooBar",
        "foo_bar",
    ]


@pytest.mark.asyncio
async def test_atomic_batch_folds_tags_across_its_items(db_session, quiet_write):
    """Item 2's spelling lands on item 1's even though nothing is committed yet."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    clear_vocabulary_cache()
    results = await MemoryService(db_session).remember_many(
        [
            _req("first environment note", tags=["Dev-Env"]),
            _req("second environment note", tags=["dev_env"]),
        ],
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        tags_normalize=True,
    )
    rows = (
        (
            await db_session.execute(
                select(Memory).where(Memory.id.in_([r.memory_id for r in results]))
            )
        )
        .scalars()
        .all()
    )
    assert {tuple(r.tags) for r in rows} == {("Dev-Env",)}
    assert [h.replacement for h in results[1].lint if h.code == "tag_normalized"] == ["Dev-Env"]
    assert "earlier in this request" in results[1].lint[0].hint


@pytest.mark.asyncio
async def test_duplicate_check_skips_a_deleted_top_hit_for_the_next_live_one(
    db_session, quiet_write
):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    gone = _existing(owner, ws, ctx, summary="gone")
    gone.deleted_at = utcnow()
    live = _existing(owner, ws, ctx, summary="still here")
    db_session.add_all([gone, live])
    await db_session.commit()
    embedder = MagicMock(embed=AsyncMock(return_value=[0.1, 0.2]))
    hits = [{"id": str(gone.id), "score": 0.95}, {"id": str(live.id), "score": 0.9}]
    with (
        patch(
            "services.context_routing.resolve_context_routing",
            new=AsyncMock(return_value=("collection", embedder)),
        ),
        patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=hits)),
    ):
        candidate = await MemoryService(db_session)._find_duplicate_candidate(
            user_id=owner, workspace_id_str=str(ws.id), context_id_str=str(ctx.id), summary="x"
        )
    assert candidate == {"memory_id": str(live.id), "summary": "still here", "similarity": 0.9}


@pytest.mark.asyncio
async def test_declared_links_are_written_before_the_embedding_task_starts(db_session, quiet_write):
    """The post-embed detection looks for the supersedes edge; the edge must exist first."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    order: list[str] = []

    async def links(self, **_):
        order.append("links")

    async def embed(_memory_id):
        order.append("embed")

    with (
        patch.object(MemoryService, "_create_declared_links", new=links),
        patch("services.memory_service.process_pending_embedding", new=embed),
    ):
        await MemoryService(db_session).remember(
            _req("order matters here"),
            user_id=owner,
            client="pytest",
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
        )
        await asyncio.sleep(0)  # let the scheduled task run
    assert order == ["links", "embed"]


# ------------------------------------------- tags_normalize: numbers (#1871)


def _tagged(owner, ws, ctx, tags: list[str]) -> Memory:
    memory = _existing(owner, ws, ctx, summary=f"stored {uuid4().hex[:6]}")
    memory.tags = tags
    return memory


async def _remember_tags(db, owner, ws, ctx, tags: list[str]):
    clear_vocabulary_cache()
    result = await MemoryService(db).remember(
        _req(f"a tagged note {uuid4().hex[:6]}", tags=tags),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        tags_normalize=True,
    )
    row = (await db.execute(select(Memory).where(Memory.id == result.memory_id))).scalar_one()
    mapped = {h.subject: h.replacement for h in result.lint if h.code == "tag_normalized"}
    return row.tags, mapped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "written"),
    [
        ("v0.1.10", "v0.11.0"),
        ("pr-1-23", "pr-123"),
        ("v0.9.4", "v0.94"),
        ("2026-1-12", "2026-11-2"),
    ],
)
async def test_tags_normalize_never_maps_a_tag_onto_one_with_other_numbers(
    db_session, quiet_write, stored, written
):
    """One ``normalize_tag`` fold, two identifiers: the tag is stored as written."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    db_session.add_all([_tagged(owner, ws, ctx, [stored]) for _ in range(3)])
    await db_session.commit()
    tags, mapped = await _remember_tags(db_session, owner, ws, ctx, [written])
    assert tags == [written]
    assert mapped == {}


@pytest.mark.asyncio
async def test_tags_normalize_still_maps_mechanical_variants_that_carry_numbers(
    db_session, quiet_write
):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    db_session.add(_tagged(owner, ws, ctx, ["dev-environment", "v0.94.0", "pr-123"]))
    await db_session.commit()
    tags, mapped = await _remember_tags(
        db_session, owner, ws, ctx, ["Dev_Environment", "V0.94.0", "PR_123"]
    )
    assert tags == ["dev-environment", "v0.94.0", "pr-123"]
    assert mapped == {
        "Dev_Environment": "dev-environment",
        "V0.94.0": "v0.94.0",
        "PR_123": "pr-123",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("frequent", ["v0.1.10", "v0.11.0"])
async def test_tags_normalize_reaches_the_true_variant_behind_a_colliding_spelling(
    db_session, quiet_write, frequent
):
    """Two stored spellings share a fold; frequency must not decide which is reachable."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    for spelling in ("v0.1.10", "v0.11.0"):
        copies = 3 if spelling == frequent else 1
        db_session.add_all([_tagged(owner, ws, ctx, [spelling]) for _ in range(copies)])
    await db_session.commit()
    tags, mapped = await _remember_tags(db_session, owner, ws, ctx, ["V0.11.0", "V0.1.10"])
    assert tags == ["v0.11.0", "v0.1.10"]
    assert mapped == {"V0.11.0": "v0.11.0", "V0.1.10": "v0.1.10"}


@pytest.mark.asyncio
async def test_tags_with_other_numbers_do_not_fold_within_one_request(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    tags, mapped = await _remember_tags(
        db_session, owner, ws, ctx, ["v0.11.0", "v0.1.10", "V0.1.10", "V0.11.0"]
    )
    assert tags == ["v0.11.0", "v0.1.10"]
    assert mapped == {"V0.1.10": "v0.1.10", "V0.11.0": "v0.11.0"}


@pytest.mark.asyncio
async def test_atomic_batch_keeps_tags_with_other_numbers_apart(db_session, quiet_write):
    """Across the items of one batch: variants still fold, colliding numbers do not."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    clear_vocabulary_cache()
    results = await MemoryService(db_session).remember_many(
        [
            _req("the first release note", tags=["v0.1.10", "Dev_Environment"]),
            _req("the second release note", tags=["v0.11.0", "dev-environment"]),
            _req("the third release note", tags=["V0.11.0"]),
        ],
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        tags_normalize=True,
    )
    stored = []
    for result in results:
        row = (
            await db_session.execute(select(Memory).where(Memory.id == result.memory_id))
        ).scalar_one()
        stored.append(row.tags)
    assert stored == [["v0.1.10", "Dev_Environment"], ["v0.11.0", "Dev_Environment"], ["v0.11.0"]]
    mapped = [
        {h.subject: h.replacement for h in r.lint if h.code == "tag_normalized"} for r in results
    ]
    assert mapped == [{}, {"dev-environment": "Dev_Environment"}, {"V0.11.0": "v0.11.0"}]


# ------------------------------------------------------- batch follow-ups (#1873)


@pytest.fixture
def daily_counter():
    """The real daily-quota gate over an in-memory counter: limit 100, 60 used."""
    counter: dict[str, int] = {}

    async def incrby(key, amount, ttl=None):
        counter[key] = counter.get(key, 0) + amount
        return counter[key]

    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.incrby_counter", new=incrby),
        patch.object(QuotaService, "check_memory_quota", new=AsyncMock(return_value=(True, None))),
        patch.object(
            Workspace, "effective_memories_per_day", new_callable=PropertyMock, return_value=100
        ),
    ):
        yield counter


def _batch(n: int) -> list[RememberRequest]:
    return [_req(f"conclusion number {i} of the batch") for i in range(n)]


@pytest.mark.asyncio
async def test_a_batch_over_the_daily_quota_charges_nothing_and_a_smaller_one_fits(
    db_session, daily_counter
):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ws_id, ctx_id = ws.id, ctx.id
    service = MemoryService(db_session)
    write = {
        "user_id": owner,
        "client": "pytest",
        "current_context_id": ctx_id,
        "current_workspace_id": ws_id,
    }
    await QuotaService(db_session).check_memories_per_day(ws_id, count=60)  # 40 left
    (key,) = daily_counter
    with pytest.raises(QuotaExceededError) as info:  # the batch itself, no item to blame
        await service.remember_many(_batch(50), **write)
    assert info.value.details["requested"] == 50 and info.value.details["used_today"] == 60
    assert daily_counter[key] == 60
    assert await _count(db_session, ctx_id) == 0
    results = await service.remember_many(_batch(40), **write)
    assert len(results) == 40
    assert daily_counter[key] == 100
    assert await _count(db_session, ctx_id) == 40


@pytest.mark.asyncio
async def test_a_rolled_back_batch_releases_its_daily_reservation(db_session, daily_counter):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ws_id, ctx_id = ws.id, ctx.id
    await QuotaService(db_session).check_memories_per_day(ws_id, count=60)
    (key,) = daily_counter
    bad = _req("a time memory without a trigger", type="time")
    with pytest.raises(BatchItemError):
        await MemoryService(db_session).remember_many(
            [*_batch(5), bad, *_batch(4)],
            user_id=owner,
            client="pytest",
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
    assert daily_counter[key] == 60  # not 65: the five prepared items cost nothing
    assert await _count(db_session, ctx_id) == 0


@pytest.mark.asyncio
async def test_a_cancellation_in_a_post_commit_step_reports_the_committed_batch(
    db_session, quiet_write
):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ctx_id = ctx.id
    with (
        patch.object(
            MemoryService,
            "_create_declared_links",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError(), None]),
        ) as links,
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()) as embed,
        pytest.raises(BatchCommittedError) as info,
    ):
        await MemoryService(db_session).remember_many(
            _batch(3),
            user_id=owner,
            client="pytest",
            current_context_id=ctx_id,
            current_workspace_id=ws.id,
        )
    responses = info.value.responses
    assert len(responses) == 3 and len({r.memory_id for r in responses}) == 3
    assert embed.call_count == 3  # every committed row becomes searchable
    assert links.await_count == 2  # nothing more is awaited once the cancellation arrived
    quiet_write.release_memories_per_day.assert_not_awaited()  # the rows exist: still charged
    await db_session.rollback()
    assert await _count(db_session, ctx_id) == 3


@pytest.mark.asyncio
async def test_a_cancellation_during_the_commit_cannot_leave_the_outcome_unknown(
    db_session, quiet_write
):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ctx_id = ctx.id
    commit = db_session.commit
    in_commit = asyncio.Event()

    async def slow_commit():
        in_commit.set()
        await asyncio.sleep(0.05)
        await commit()

    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()) as embed,
        patch.object(db_session, "commit", new=slow_commit),
    ):
        task = asyncio.ensure_future(
            MemoryService(db_session).remember_many(
                _batch(3),
                user_id=owner,
                client="pytest",
                current_context_id=ctx_id,
                current_workspace_id=ws.id,
            )
        )
        await in_commit.wait()
        task.cancel()
        with pytest.raises(BatchCommittedError) as info:
            await asyncio.wait_for(task, 5)
    assert len(info.value.responses) == 3
    assert embed.call_count == 3
    quiet_write.release_memories_per_day.assert_not_awaited()
    assert await _count(db_session, ctx_id) == 3


@pytest.mark.asyncio
async def test_a_cancellation_before_the_commit_rolls_back_and_releases(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ctx_id = ctx.id
    preparing = asyncio.Event()

    async def stall(*_a, **_k):
        if quiet_write.check_memory_quota.await_count == 2:  # item 0 is already added
            preparing.set()
            await asyncio.sleep(30)
        return True, None

    quiet_write.check_memory_quota.side_effect = stall
    task = asyncio.ensure_future(
        MemoryService(db_session).remember_many(
            _batch(3),
            user_id=owner,
            client="pytest",
            current_context_id=ctx_id,
            current_workspace_id=ws.id,
        )
    )
    await preparing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    quiet_write.release_memories_per_day.assert_awaited_once_with(_RESERVED)
    assert await _count(db_session, ctx_id) == 0


async def _duplicate_check_scope(db, *, user_id, ws, ctx) -> dict:
    """The keyword arguments the duplicate check hands the vector search."""
    embedder = MagicMock(embed=AsyncMock(return_value=[0.1, 0.2]))
    search = AsyncMock(return_value=[])
    with (
        patch(
            "services.context_routing.resolve_context_routing",
            new=AsyncMock(return_value=("collection", embedder)),
        ),
        patch("db.qdrant.search_memories_qdrant", new=search),
    ):
        await MemoryService(db)._find_duplicate_candidate(
            user_id=user_id, workspace_id_str=str(ws.id), context_id_str=str(ctx.id), summary="x"
        )
    return search.await_args.kwargs


@pytest.mark.asyncio
async def test_duplicate_check_searches_every_member_of_a_shared_context(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)  # is_private=False
    await db_session.commit()
    scope = await _duplicate_check_scope(db_session, user_id=owner, ws=ws, ctx=ctx)
    assert scope["is_shared_context"] is True  # as recall: no author filter
    assert scope["owner_ids"] is None


@pytest.mark.asyncio
async def test_duplicate_check_includes_linked_accounts_in_a_private_context(
    db_session, quiet_write
):
    owner, other = f"o-{uuid4().hex[:6]}", f"l-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    ctx.is_private = True
    await db_session.commit()
    alone = await _duplicate_check_scope(db_session, user_id=owner, ws=ws, ctx=ctx)
    assert alone["is_shared_context"] is False and alone["owner_ids"] is None

    db_session.add(User(email=f"{other}@test.example", user_id=other, role="user"))
    await db_session.flush()
    group = uuid4()
    for account in (owner, other):
        db_session.add(IdentityLink(group_id=group, user_id=account, linked_by=owner))
    await db_session.commit()
    linked = await _duplicate_check_scope(db_session, user_id=owner, ws=ws, ctx=ctx)
    assert linked["is_shared_context"] is False
    assert linked["owner_ids"] == sorted([owner, other])


# ------------------------------------------- reservation accounting (#1873 review)


@pytest.mark.asyncio
async def test_a_release_returns_exactly_the_reservation_across_midnight(db_session, daily_counter):
    from datetime import UTC, datetime

    owner = f"o-{uuid4().hex[:6]}"
    ws, _ctx = await _scope(db_session, owner)
    await db_session.commit()
    quota = QuotaService(db_session)
    with patch(
        "services.quota_service.utcnow", return_value=datetime(2026, 1, 1, 23, 59, tzinfo=UTC)
    ):
        reservation = await quota.reserve_memories_per_day(ws.id, 7)
    assert reservation is not None and reservation.count == 7
    assert reservation.key.endswith("2026-01-01") and daily_counter == {reservation.key: 7}
    with patch(
        "services.quota_service.utcnow", return_value=datetime(2026, 1, 2, 0, 1, tzinfo=UTC)
    ):
        await quota.release_memories_per_day(reservation)
    assert daily_counter == {reservation.key: 0}  # the new day's counter is never lowered


@pytest.mark.asyncio
async def test_a_batch_that_reserved_nothing_releases_nothing(db_session, daily_counter):
    """The counter is down at reservation time (fail-open) and back for the rollback."""
    from utils.exceptions import RedisError

    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    ws_id, ctx_id = ws.id, ctx.id
    calls: list[int] = []

    async def down_once(key, amount, ttl=None):
        calls.append(amount)
        if len(calls) == 1:
            raise RedisError("counter unreachable")
        daily_counter[key] = daily_counter.get(key, 0) + amount
        return daily_counter[key]

    bad = _req("a time memory without a trigger", type="time")
    with (
        patch("services.quota_service.incrby_counter", new=down_once),
        pytest.raises(BatchItemError),
    ):
        await MemoryService(db_session).remember_many(
            [*_batch(2), bad],
            user_id=owner,
            client="pytest",
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
    assert calls == [3]  # the failed reservation only: no decrement of what was never added
    assert daily_counter == {}


@pytest.mark.asyncio
async def test_a_reservation_cancelled_in_flight_is_undone(db_session, daily_counter):
    from services import quota_service

    owner = f"o-{uuid4().hex[:6]}"
    ws, _ctx = await _scope(db_session, owner)
    await db_session.commit()
    in_flight = asyncio.Event()

    async def slow(key, amount, ttl=None):
        daily_counter[key] = daily_counter.get(key, 0) + amount  # the INCRBY landed
        if amount > 0:
            in_flight.set()
            await asyncio.sleep(0.05)  # ... the TTL bookkeeping is still running
        return daily_counter[key]

    with patch("services.quota_service.incrby_counter", new=slow):
        task = asyncio.ensure_future(QuotaService(db_session).reserve_memories_per_day(ws.id, 5))
        await in_flight.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        (key,) = daily_counter
        assert daily_counter[key] == 5
        for _ in range(100):
            if daily_counter[key] == 0:
                break
            await asyncio.sleep(0.01)
    assert daily_counter[key] == 0
    assert not quota_service._PENDING_UNDO


# ------------------------------------------ cross-author candidates (#1873 review)


@pytest.mark.asyncio
async def test_another_members_candidate_is_not_resolved_by_supersedes(db_session, quiet_write):
    owner, member = f"o-{uuid4().hex[:6]}", f"m-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)  # shared
    db_session.add(User(email=f"{member}@test.example", user_id=member, role="user"))
    await db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=member, role=WorkspaceRole.MEMBER))
    theirs = _existing(owner, ws, ctx, summary="the owner wrote this")
    mine = _existing(member, ws, ctx, summary="the member wrote this")
    db_session.add_all([theirs, mine])
    await db_session.commit()
    ws_id, ctx_id, theirs_id, mine_id = ws.id, ctx.id, theirs.id, mine.id
    embedder = MagicMock(embed=AsyncMock(return_value=[0.1, 0.2]))
    service = MemoryService(db_session)

    async def check(target_id, **over):
        with (
            patch(
                "services.context_routing.resolve_context_routing",
                new=AsyncMock(return_value=("collection", embedder)),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=[{"id": str(target_id), "score": 0.93}]),
            ),
        ):
            return await service.remember(
                _req("the same fact again", **over),
                user_id=member,
                client="pytest",
                current_context_id=ctx_id,
                current_workspace_id=ws_id,
                dedupe="check",
            )

    with pytest.raises(DuplicateCandidateError) as info:
        await check(theirs_id)
    assert info.value.candidate == {
        "memory_id": str(theirs_id),
        "summary": "the owner wrote this",
        "similarity": 0.93,
        "supersedable": False,
    }
    # supersedes cannot shadow another member's memory (#1803): still a candidate,
    # not a write that reports success without the edge.
    with pytest.raises(DuplicateCandidateError) as again:
        await check(theirs_id, supersedes=theirs_id)
    assert again.value.candidate["supersedable"] is False
    assert await _count(db_session, ctx_id) == 2
    # The member's own memory keeps today's shape and the supersedes resolution.
    with pytest.raises(DuplicateCandidateError) as own:
        await check(mine_id)
    assert "supersedable" not in own.value.candidate
    result = await check(mine_id, supersedes=mine_id)
    assert result.memory_id != mine_id


@pytest.mark.asyncio
async def test_a_linked_accounts_candidate_in_a_private_context_is_supersedable(
    db_session, quiet_write
):
    owner, other = f"o-{uuid4().hex[:6]}", f"l-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    ctx.is_private = True
    db_session.add(User(email=f"{other}@test.example", user_id=other, role="user"))
    await db_session.flush()
    group = uuid4()
    for account in (owner, other):
        db_session.add(IdentityLink(group_id=group, user_id=account, linked_by=owner))
    linked_memory = _existing(other, ws, ctx, summary="written by the linked account")
    db_session.add(linked_memory)
    await db_session.commit()
    embedder = MagicMock(embed=AsyncMock(return_value=[0.1, 0.2]))
    with (
        patch(
            "services.context_routing.resolve_context_routing",
            new=AsyncMock(return_value=("collection", embedder)),
        ),
        patch(
            "db.qdrant.search_memories_qdrant",
            new=AsyncMock(return_value=[{"id": str(linked_memory.id), "score": 0.9}]),
        ),
    ):
        candidate = await MemoryService(db_session)._find_duplicate_candidate(
            user_id=owner, workspace_id_str=str(ws.id), context_id_str=str(ctx.id), summary="x"
        )
    assert candidate is not None and "supersedable" not in candidate
