"""``remember``'s ``tags_normalize`` / ``dedupe`` and ``remember_many`` on real Postgres (#1853)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RememberRequest
from services.memory_service import (
    BatchItemError,
    DedupeUnavailableError,
    DuplicateCandidateError,
    MemoryService,
)
from services.supersede_dismissal import is_dismissed
from services.tag_resolution import clear_vocabulary_cache
from utils.datetime import utcnow


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


@pytest.fixture
def quiet_write():
    """No embedding task, no Redis quota: the parts of remember() that are not under test."""
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
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
    assert quiet_write.check_memories_per_day.await_count == 3  # the daily quota per item


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
