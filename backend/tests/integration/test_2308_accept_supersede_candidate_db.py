"""Accepting a supersede suggestion with ``update_memory`` (#2308), on Postgres.

``update_memory(memory_id, accept_supersede_candidate=true)`` does what
``create_edge(source_id=<memory>, target_id=<candidate>, edge_type="supersedes")``
does: the declared ``supersedes`` edge is written, the stored suggestion is
cleared and the older memory leaves default recall. The candidate is read
from the row under the edit lock, as the read path would show it; with none
to accept, nothing is written and the caller is told so.

Stubbed at the module boundary only: ``search_service.hybrid_search`` (the
candidate pool) and the embedding task / quota service of ``remember()``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import EDGE_ORIGIN_DECLARED, EDGE_TYPE_SUPERSEDES, Memory, NeuralMemoryEdge
from models.schemas import RecallRequest, RememberRequest, UpdateMemoryRequest
from services.memory_service import MemoryService
from services.quota_service import DailyReservation
from services.supersede_dismissal import build_tombstone
from utils.datetime import utcnow
from utils.exceptions import SupersedeEdgeConflictError, VersionConflictError


@pytest.fixture(autouse=True)
def default_floor(monkeypatch):
    monkeypatch.delenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", raising=False)


@pytest.fixture
def quiet_write():
    """No embedding task, no Redis quota: the parts of remember() not under test."""
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(
            return_value=DailyReservation(key="quota:test:memories:day", count=1)
        ),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        yield quota


async def _scope(db) -> tuple[str, Workspace, Context]:
    owner = f"acc_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"acc-ws-{uuid4().hex[:8]}",
        plan_name="pro",
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
        name=f"acc-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return owner, ws, ctx


async def _write(db, owner, ws, ctx, summary: str, *, age_days: int) -> UUID:
    result = await MemoryService(db).remember(
        RememberRequest(summary=summary, content=summary, type="fact", importance=0.5),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    await db.execute(
        update(Memory)
        .where(Memory.id == result.memory_id)
        .values(created_at=utcnow() - timedelta(days=age_days))
    )
    await db.commit()
    return result.memory_id


def _candidate(old_id: UUID) -> dict[str, Any]:
    return {
        "memory_id": str(old_id),
        "similarity": 0.93,
        "detected_at": "2026-10-01T00:00:00Z",
    }


async def _store(db, memory_id: UUID, value: Any) -> None:
    await db.execute(update(Memory).where(Memory.id == memory_id).values(supersede_candidate=value))
    await db.commit()


async def _row(db, memory_id: UUID) -> Memory:
    row = (await db.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    await db.refresh(row)
    return row


async def _edges(db, src: UUID, dst: UUID) -> list[NeuralMemoryEdge]:
    return list(
        (
            await db.execute(
                select(NeuralMemoryEdge).where(
                    NeuralMemoryEdge.src_id == src, NeuralMemoryEdge.dst_id == dst
                )
            )
        )
        .scalars()
        .all()
    )


async def _accept(db, owner, ws, ctx, memory_id: UUID, **extra: Any):
    return await MemoryService(db).update_memory(
        UpdateMemoryRequest(memory_id=memory_id, accept_supersede_candidate=True, **extra),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )


async def _recall(db, owner, ws, ctx, pool: list[UUID]) -> list[UUID]:
    svc = MemoryService(db)
    rows = [{"id": str(memory_id), "score": 0.9 - 0.1 * i} for i, memory_id in enumerate(pool)]
    svc.search_service.hybrid_search = AsyncMock(return_value=rows)
    response = await svc.recall(
        RecallRequest(query="the fact", k=5),
        user_id=owner,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return [r.memory_id for r in response.results]


@pytest.mark.asyncio
async def test_accepting_a_live_candidate_supersedes_and_clears_it(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _store(db_session, newer, _candidate(older))
    before = await _row(db_session, newer)
    updated_at, version = before.updated_at, before.version
    assert await _recall(db_session, owner, ws, ctx, [newer, older]) == [newer, older]

    response = await _accept(db_session, owner, ws, ctx, newer)

    assert response.superseded_memory_id == older
    assert response.supersede_candidate_dismissed is None
    assert response.re_embedded is False
    edges = await _edges(db_session, newer, older)
    assert [(e.edge_type, e.origin) for e in edges] == [
        (EDGE_TYPE_SUPERSEDES, EDGE_ORIGIN_DECLARED)
    ]
    after = await _row(db_session, newer)
    assert after.supersede_candidate is None
    # A graph action, not a content edit: no new version, no fresher timestamp.
    assert after.version == version == response.version
    assert after.updated_at == updated_at
    # The older memory is shadowed out of default recall.
    assert await _recall(db_session, owner, ws, ctx, [newer, older]) == [newer]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["none", "tombstone"])
async def test_no_live_candidate_is_refused_and_writes_nothing(db_session, quiet_write, stored):
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    value = None if stored == "none" else build_tombstone(_candidate(older))
    await _store(db_session, newer, value)

    with pytest.raises(ValueError, match="no supersede_candidate"):
        await _accept(db_session, owner, ws, ctx, newer)
    await db_session.rollback()

    assert await _edges(db_session, newer, older) == []
    assert (await _row(db_session, newer)).supersede_candidate == value


@pytest.mark.asyncio
async def test_a_candidate_whose_target_is_gone_is_not_accepted(db_session, quiet_write):
    """The read path drops a suggestion whose target was forgotten; so does accept."""
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _store(db_session, newer, _candidate(older))
    await db_session.execute(update(Memory).where(Memory.id == older).values(deleted_at=utcnow()))
    await db_session.commit()

    with pytest.raises(ValueError, match="no supersede_candidate"):
        await _accept(db_session, owner, ws, ctx, newer)
    await db_session.rollback()

    assert await _edges(db_session, newer, older) == []


@pytest.mark.asyncio
async def test_the_candidate_is_read_under_the_edit_lock(db_session, quiet_write):
    """A suggestion cleared between the authorizing read and the lock (another
    session accepted or re-detected it) is not accepted from the stale copy."""
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _store(db_session, newer, _candidate(older))

    authorize = MemoryService._update_load_authorized

    async def authorize_then_clear(self, memory_id, user_id, **scope):
        memory = await authorize(self, memory_id, user_id, **scope)
        assert memory.supersede_candidate is not None
        await self.db.execute(
            update(Memory).where(Memory.id == memory_id).values(supersede_candidate=None)
        )
        return memory

    with (
        patch.object(MemoryService, "_update_load_authorized", authorize_then_clear),
        pytest.raises(ValueError, match="no supersede_candidate"),
    ):
        await _accept(db_session, owner, ws, ctx, newer)
    await db_session.rollback()

    assert await _edges(db_session, newer, older) == []


@pytest.mark.asyncio
async def test_a_stale_expected_version_writes_nothing(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _store(db_session, newer, _candidate(older))
    version = (await _row(db_session, newer)).version

    with pytest.raises(VersionConflictError):
        await _accept(db_session, owner, ws, ctx, newer, expected_version=version + 1)
    await db_session.rollback()

    assert await _edges(db_session, newer, older) == []
    assert (await _row(db_session, newer)).supersede_candidate is not None


@pytest.mark.asyncio
async def test_a_different_declared_edge_on_the_pair_is_not_clobbered(db_session, quiet_write):
    """create_edge's duplicate contract: a declared link is provenance and is
    not silently retyped — the accept is refused and the suggestion stays."""
    from services.edge_service import create_declared_edge

    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await create_declared_edge(
        db_session,
        user_id=owner,
        source_id=newer,
        target_id=older,
        edge_type="related_to",
        weight=1.0,
        confidence=1.0,
        workspace_id=str(ws.id),
        context_id=str(ctx.id),
        overwrite=False,
    )
    await db_session.commit()
    await _store(db_session, newer, _candidate(older))

    with pytest.raises(SupersedeEdgeConflictError) as exc:
        await _accept(db_session, owner, ws, ctx, newer)
    await db_session.rollback()

    assert exc.value.existing["edge_type"] == "related_to"
    assert [e.edge_type for e in await _edges(db_session, newer, older)] == ["related_to"]
    assert (await _row(db_session, newer)).supersede_candidate is not None


@pytest.mark.asyncio
async def test_a_candidate_written_after_this_memory_is_not_accepted(db_session, quiet_write):
    """A neighbour whose embedding finished first can be stored as the earlier
    memory's candidate; accepting it would hide the newer fact."""
    owner, ws, ctx = await _scope(db_session)
    earlier = await _write(db_session, owner, ws, ctx, "the earlier fact", age_days=2)
    later = await _write(db_session, owner, ws, ctx, "the later fact", age_days=0)
    await _store(db_session, earlier, _candidate(later))

    with pytest.raises(ValueError, match="written after it"):
        await _accept(db_session, owner, ws, ctx, earlier)
    await db_session.rollback()

    assert await _edges(db_session, earlier, later) == []
    assert (await _row(db_session, earlier)).supersede_candidate is not None


@pytest.mark.asyncio
async def test_a_failed_lookup_is_a_server_error_not_no_candidate(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _store(db_session, newer, _candidate(older))

    with (
        patch(
            "services.agent_binding_service.filter_memory_rows_by_binding",
            new=AsyncMock(side_effect=RuntimeError("lookup down")),
        ),
        pytest.raises(RuntimeError, match="lookup down"),
    ):
        await _accept(db_session, owner, ws, ctx, newer)
    await db_session.rollback()

    assert await _edges(db_session, newer, older) == []


@pytest.mark.asyncio
async def test_an_edge_kept_as_another_type_is_not_reported_accepted(db_session, quiet_write):
    """The upsert's declared-link race guard can keep a concurrent declared
    edge of another type; the accept must not then report success."""
    from services.edge_service import EdgeWriteResult

    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _store(db_session, newer, _candidate(older))
    kept = EdgeWriteResult(operation="created", edge={"edge_type": "related_to"})

    with (
        patch("services.edge_service.create_declared_edge", new=AsyncMock(return_value=kept)),
        pytest.raises(SupersedeEdgeConflictError),
    ):
        await _accept(db_session, owner, ws, ctx, newer)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_a_suggestion_left_uncleared_rolls_the_accept_back(db_session, quiet_write):
    """The clear inside create_declared_edge is best-effort; if it did not run,
    nothing commits rather than a success with the suggestion still surfacing."""
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _store(db_session, newer, _candidate(older))

    with (
        patch("services.edge_service.accept_supersede_candidate_if_matching", new=AsyncMock()),
        pytest.raises(RuntimeError, match="not cleared"),
    ):
        await _accept(db_session, owner, ws, ctx, newer)
    await db_session.rollback()

    assert await _edges(db_session, newer, older) == []
    assert (await _row(db_session, newer)).supersede_candidate is not None


# ---------------------------------------------------------------- #1803 owner rule


async def _pair(db, context, carrier_author, target_author):
    """``target`` (older) by one account, ``carrier`` (newer, suggesting it) by another."""
    from tests.integration.test_identity_links_db import _memory

    target = await _memory(db, context, target_author)
    target.created_at = utcnow() - timedelta(days=1)
    carrier = await _memory(db, context, carrier_author)
    carrier.supersede_candidate = _candidate(target.id)
    await db.commit()
    return carrier.id, target.id


async def _accept_as(db, caller, context, memory_id):
    return await MemoryService(db).update_memory(
        UpdateMemoryRequest(memory_id=memory_id, accept_supersede_candidate=True),
        user_id=caller.user_id,
        client="pytest",
        current_context_id=context.id,
        current_workspace_id=context.workspace_id,
    )


@pytest.mark.asyncio
async def test_a_member_cannot_accept_a_candidate_that_is_another_members_memory(
    db_session, quiet_write
):
    """Shared context: B's memory suggests A's. Shadowing it would hide A's
    memory for the whole team; remember(supersedes=) refuses the same target."""
    from tests.integration.test_identity_link_writes_db import _seed

    a, b, _, context, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, context, b, a)

    with pytest.raises(ValueError, match="another member's memory"):
        await _accept_as(db_session, b, context, carrier)
    await db_session.rollback()

    assert await _edges(db_session, carrier, target) == []
    assert (await _row(db_session, carrier)).supersede_candidate is not None


@pytest.mark.asyncio
async def test_the_owner_of_both_memories_can_accept(db_session, quiet_write):
    from tests.integration.test_identity_link_writes_db import _seed

    a, _, _, context, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, context, a, a)

    response = await _accept_as(db_session, a, context, carrier)

    assert response.superseded_memory_id == target
    assert [e.edge_type for e in await _edges(db_session, carrier, target)] == [
        EDGE_TYPE_SUPERSEDES
    ]


@pytest.mark.asyncio
async def test_a_linked_accounts_memory_in_a_private_context_can_be_accepted(
    db_session, quiet_write
):
    """In a private context the owner is the link set (#1784): the other
    account's memory counts as the caller's own, as for remember(supersedes=)."""
    from tests.integration.test_identity_link_writes_db import _seed

    admin, oauth, _, context, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, context, oauth, admin)

    response = await _accept_as(db_session, oauth, context, carrier)

    assert response.superseded_memory_id == target
    assert [e.edge_type for e in await _edges(db_session, carrier, target)] == [
        EDGE_TYPE_SUPERSEDES
    ]


@pytest.mark.asyncio
async def test_a_linked_account_does_not_count_in_a_shared_context(db_session, quiet_write):
    """Outside a private context the link set does not widen the rule."""
    from tests.integration.test_identity_link_writes_db import _seed

    admin, oauth, _, context, _ = await _seed(db_session, private=False, link=True)
    carrier, target = await _pair(db_session, context, oauth, admin)

    with pytest.raises(ValueError, match="another member's memory"):
        await _accept_as(db_session, oauth, context, carrier)
    await db_session.rollback()

    assert await _edges(db_session, carrier, target) == []


@pytest.mark.asyncio
async def test_a_member_cannot_accept_on_another_members_memory(db_session, quiet_write):
    """The superseding end must be the caller's own too, as remember's new
    memory always is — an edge kept under the caller would not be the one
    detection looks up for the carrier's author."""
    from tests.integration.test_identity_link_writes_db import _seed

    a, b, _, context, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, context, a, b)

    with pytest.raises(ValueError, match="on your own memory"):
        await _accept_as(db_session, b, context, carrier)
    await db_session.rollback()

    assert await _edges(db_session, carrier, target) == []


@pytest.mark.asyncio
async def test_a_linked_accounts_carrier_keeps_the_edge_under_its_author(db_session, quiet_write):
    """Private context: oauth accepts on the admin account's memory. The edge
    is the carrier author's, which is where detection looks for it, so a
    re-embed does not bring the suggestion back."""
    from repositories.neural_edge import NeuralEdgeRepository
    from tests.integration.test_identity_link_writes_db import _seed

    admin, oauth, _, context, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, context, admin, oauth)

    await _accept_as(db_session, oauth, context, carrier)

    edges = await _edges(db_session, carrier, target)
    assert [(e.edge_type, e.user_id) for e in edges] == [(EDGE_TYPE_SUPERSEDES, admin.user_id)]
    found = await NeuralEdgeRepository(db_session).get_edge(
        admin.user_id,
        carrier,
        target,
        workspace_id=str(context.workspace_id),
        context_id=str(context.id),
    )
    assert found is not None
