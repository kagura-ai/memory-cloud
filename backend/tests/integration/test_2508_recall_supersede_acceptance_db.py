"""``supersede_candidate.acceptance`` on recall rows, on Postgres (#2508).

Every recall row with a suggestion carries the caller's ``acceptance``, from
the rule ``reference()`` reports (#2427) and the accept path enforces,
evaluated for the whole page by ``supersede_acceptance.evaluate_many``: write
access (and, for guardrail carriers, the #2614 author gate) once per context,
ownership once per context, the declared-edge lookup once for the page. A row without a suggestion has no ``acceptance`` key, and a
failed evaluation leaves the page's suggestions without it (fail-open).

The measurement test records the cost on a k=100 page: the statements the
acceptance step adds (counted with a ``before_cursor_execute`` listener on the
test engine) and the recall's wall time with and without it.

Stubbed at the module boundary only: ``search_service.hybrid_search`` (the
candidate pool).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import event, text, update

import services.supersede_acceptance as supersede_acceptance
from api.routes.share_keys import share_recall
from models.auth import Context, ContextRole, User, Workspace
from models.memory import Memory
from models.schemas import RecallRequest
from services.memory_service import MemoryService
from tests.integration.test_2308_accept_supersede_candidate_db import _candidate, _pair
from tests.integration.test_2427_supersede_acceptance_db import _declare
from tests.integration.test_identity_link_writes_db import _seed
from tests.integration.test_identity_links_db import _memory
from utils.datetime import utcnow


async def _recall(
    db,
    caller: User,
    ws: Workspace,
    ctx: Context,
    ids: list[UUID],
    *,
    k: int = 10,
    context_ids: list[UUID] | None = None,
):
    service = MemoryService(db)
    service.search_service.hybrid_search = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": str(mid), "score": 0.9 - n * 0.001} for n, mid in enumerate(ids)]
    )
    return await service.recall(
        RecallRequest(query="a summary", k=k),
        user_id=caller.user_id,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        context_ids=context_ids,
    )


def _acceptances(result) -> dict[UUID, Any]:
    """Each row's wire ``acceptance``: absent key → ``None``; no suggestion → ``"-"``."""
    out: dict[UUID, Any] = {}
    for row in result.results:
        dumped = row.model_dump(mode="json", exclude_none=True)
        candidate = dumped.get("supersede_candidate")
        out[row.memory_id] = "-" if candidate is None else candidate.get("acceptance")
    return out


@pytest.mark.asyncio
async def test_rows_of_one_page_differ_and_a_row_without_a_suggestion_has_no_key(db_session):
    a, b, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    allowed, _ = await _pair(db_session, ctx, a, a)
    foreign, _ = await _pair(db_session, ctx, a, b)
    conflicted, conflicted_target = await _pair(db_session, ctx, a, a)
    await _declare(
        db_session,
        a,
        ws,
        ctx,
        conflicted,
        conflicted_target,
        edge_type="related_to",
        weight=1.0,
        confidence=1.0,
    )
    newer, newer_target = await _pair(db_session, ctx, a, a)
    await db_session.execute(
        update(Memory)
        .where(Memory.id == newer_target)
        .values(created_at=utcnow() + timedelta(days=1))
    )
    bare = (await _memory(db_session, ctx, a)).id
    await db_session.commit()

    result = await _recall(db_session, a, ws, ctx, [allowed, foreign, conflicted, newer, bare])

    assert _acceptances(result) == {
        allowed: {"allowed": True},
        foreign: {"allowed": False, "reason": "not_owner"},
        conflicted: {"allowed": False, "reason": "edge_exists"},
        newer: {"allowed": False, "reason": "newer_candidate"},
        bare: "-",
    }


@pytest.mark.asyncio
async def test_a_viewer_reads_not_writable_on_every_row(db_session):
    from tests.integration.test_2427_supersede_acceptance_db import _viewer

    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    viewer = await _viewer(db_session, ws)
    carriers = [(await _pair(db_session, ctx, a, a))[0] for _ in range(3)]

    result = await _recall(db_session, viewer, ws, ctx, carriers)

    assert set(_acceptances(result)) == set(carriers)
    assert all(
        v == {"allowed": False, "reason": "not_writable"} for v in _acceptances(result).values()
    )


@pytest.mark.asyncio
async def test_a_cross_context_page_evaluates_each_context(db_session):
    """Two contexts on one page: each row is answered by its own context's
    ownership (one query per context), the edge lookup is one for both."""
    a, b, ws, first, _ = await _seed(db_session, private=False, link=False)
    second = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"second_{uuid4().hex[:6]}",
        created_by=a.user_id,
        is_private=False,
    )
    db_session.add(second)
    await db_session.commit()
    own_first, _ = await _pair(db_session, first, a, a)
    foreign_second, _ = await _pair(db_session, second, a, b)
    own_second, _ = await _pair(db_session, second, a, a)

    with _statements(db_session) as executed:
        result = await _recall(
            db_session,
            a,
            ws,
            first,
            [own_first, foreign_second, own_second],
            context_ids=[first.id, second.id],
        )

    assert _acceptances(result) == {
        own_first: {"allowed": True},
        foreign_second: {"allowed": False, "reason": "not_owner"},
        own_second: {"allowed": True},
    }
    assert len(_ownership_reads(executed)) == 2
    assert len(_edge_reads(executed)) == 1


@pytest.mark.asyncio
async def test_a_private_and_a_shared_context_on_one_page_keep_their_own_ownership(
    db_session,
):
    """#2313 per context: a linked account's pair is the caller's own in the
    caller's private context and not in a shared one, on the same page."""
    from tests.integration.test_identity_link_writes_db import _shared_context

    admin, oauth, ws, private, _ = await _seed(db_session, private=True, link=True)
    shared = await _shared_context(db_session, ws, admin)
    await db_session.commit()
    linked_private, _ = await _pair(db_session, private, oauth, oauth)
    linked_shared, _ = await _pair(db_session, shared, oauth, oauth)
    own_shared, _ = await _pair(db_session, shared, admin, admin)

    with _statements(db_session) as executed:
        result = await _recall(
            db_session,
            admin,
            ws,
            private,
            [linked_private, linked_shared, own_shared],
            context_ids=[private.id, shared.id],
        )

    assert _acceptances(result) == {
        linked_private: {"allowed": True},
        linked_shared: {"allowed": False, "reason": "not_owner"},
        own_shared: {"allowed": True},
    }
    assert len(_ownership_reads(executed)) == 2


@pytest.mark.asyncio
async def test_each_pair_on_a_page_meets_only_its_own_authors_edge(db_session):
    """The one edge lookup holds every pair to its own carrier's author and
    its own (carrier, target): an edge another account keeps on the pair, or
    the author keeps from the carrier to another row's target, refuses
    nothing. Each row matches the single read (``reference``)."""
    admin, oauth, ws, ctx, _ = await _seed(db_session, private=True, link=True)
    other = {"edge_type": "related_to", "weight": 1.0, "confidence": 1.0}
    by_admin, by_admin_target = await _pair(db_session, ctx, admin, admin)
    by_oauth, by_oauth_target = await _pair(db_session, ctx, oauth, oauth)
    by_oauth_clean, by_oauth_clean_target = await _pair(db_session, ctx, oauth, oauth)
    await _declare(db_session, oauth, ws, ctx, by_admin, by_admin_target, **other)
    await _declare(db_session, admin, ws, ctx, by_admin, by_oauth_target, **other)
    await _declare(db_session, oauth, ws, ctx, by_oauth, by_oauth_target, **other)
    await _declare(db_session, admin, ws, ctx, by_oauth_clean, by_oauth_clean_target, **other)

    result = await _recall(db_session, admin, ws, ctx, [by_admin, by_oauth, by_oauth_clean])

    expected = {
        by_admin: {"allowed": True},
        by_oauth: {"allowed": False, "reason": "edge_exists"},
        by_oauth_clean: {"allowed": True},
    }
    assert _acceptances(result) == expected
    service = MemoryService(db_session)
    for carrier, acceptance in expected.items():
        referenced = (await service.reference(carrier, admin.user_id)).supersede_candidate
        assert referenced is not None and referenced.acceptance is not None
        assert referenced.acceptance.model_dump(exclude_none=True) == acceptance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "guarded"),
    [(None, {"allowed": False, "reason": "not_writable"}), (ContextRole.EDITOR, {"allowed": True})],
    ids=["workspace_member", "context_editor"],
)
async def test_guardrail_rows_are_held_to_the_author_gate_once_per_page(db_session, role, guarded):
    """#2614 on a page: a carrier of ``details.tool_trigger`` needs context
    EDITOR or above, probed once for the context however many such rows it
    holds; a plain carrier beside them is not held to it."""
    from tests.integration.test_2427_supersede_acceptance_db import (
        _mark_guardrail,
        _workspace_member,
    )

    _, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    member = await _workspace_member(db_session, ws, ctx, role=role)
    guardrails = []
    for _ in range(3):
        carrier, _ = await _pair(db_session, ctx, member, member)
        await _mark_guardrail(db_session, carrier)
        guardrails.append(carrier)
    plain, _ = await _pair(db_session, ctx, member, member)

    probe = AsyncMock(wraps=supersede_acceptance.may_author_guardrail)
    with patch("services.supersede_acceptance.may_author_guardrail", probe):
        result = await _recall(db_session, member, ws, ctx, [*guardrails, plain])

    assert _acceptances(result) == {
        **dict.fromkeys(guardrails, guarded),
        plain: {"allowed": True},
    }
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_a_failing_evaluation_keeps_the_page_and_its_suggestions(db_session):
    """Fail-open: the edge lookup raises, the rows keep their suggestions
    without ``acceptance``, one WARNING names them, the recall returns and the
    session stays usable."""
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, a, a)
    other, _ = await _pair(db_session, ctx, a, a)

    async def failing_lookup(self, keys):
        await self.db.execute(text("SELECT 1 / 0"))

    with (
        patch("repositories.neural_edge.NeuralEdgeRepository.list_pairs_edges", failing_lookup),
        structlog.testing.capture_logs() as logs,
    ):
        result = await _recall(db_session, a, ws, ctx, [carrier, other])

    failed = [e for e in logs if e["event"] == "supersede_acceptance_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "warning"
    assert set(failed[0]["memory_ids"]) == {str(carrier), str(other)}
    rows = {r.memory_id: r for r in result.results}
    assert rows[carrier].supersede_candidate is not None
    assert rows[carrier].supersede_candidate.memory_id == target
    assert _acceptances(result) == {carrier: None, other: None}
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
    await db_session.commit()


@pytest.mark.asyncio
async def test_a_share_key_read_carries_no_acceptance(db_session):
    """The share-key principal is the key's creator and the key never writes:
    the reader gets the suggestion without the creator's ``acceptance``."""
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, _ = await _pair(db_session, ctx, a, a)
    service = MemoryService(db_session)
    service.search_service.hybrid_search = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": str(carrier), "score": 0.9}]
    )

    result = await share_recall(
        RecallRequest(query="a summary", k=5),
        {
            "user_id": a.user_id,
            "share_key_context_id": ctx.id,
            "current_workspace_id": ws.id,
            "share_key_id": "sk_test",
        },
        service,
    )

    assert _acceptances(result) == {carrier: None}


# --------------------------------------------------------------------------
# Measurement (#2508): the cost of the acceptance step on a k=100 page.
# --------------------------------------------------------------------------


@contextmanager
def _statements(db) -> Iterator[list[str]]:
    """The SQL the acceptance step executes, in order: everything
    ``_attach_supersede_acceptance`` sends, its savepoint included."""
    executed: list[str] = []
    window = {"open": False}
    original = MemoryService._attach_supersede_acceptance

    async def counted(self: MemoryService, *args: Any, **kwargs: Any) -> None:
        window["open"] = True
        try:
            await original(self, *args, **kwargs)
        finally:
            window["open"] = False

    def listener(_conn, _cursor, statement, _params, _context, _executemany):
        if window["open"]:
            executed.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", listener)
    try:
        with patch.object(MemoryService, "_attach_supersede_acceptance", counted):
            yield executed
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _ownership_reads(executed: list[str]) -> list[str]:
    return [s for s in executed if "FROM memories" in s]


def _edge_reads(executed: list[str]) -> list[str]:
    return [s for s in executed if "FROM neural_memory_edges" in s]


async def _page_of_carriers(db, ctx: Context, author: User, n: int) -> list[UUID]:
    """``n`` own carriers, each suggesting its own older target."""
    now = utcnow()
    carriers: list[Memory] = []
    for i in range(n):
        target = Memory(
            id=uuid4(),
            user_id=author.user_id,
            workspace_id=ctx.workspace_id,
            context_id=ctx.id,
            summary=f"an older summary {i}",
            content="content",
            type="note",
            client="pytest",
            embedding_status="success",
            created_at=now - timedelta(days=2, minutes=i),
        )
        carrier = Memory(
            id=uuid4(),
            user_id=author.user_id,
            workspace_id=ctx.workspace_id,
            context_id=ctx.id,
            summary=f"a newer summary {i}",
            content="content",
            type="note",
            client="pytest",
            embedding_status="success",
            created_at=now - timedelta(days=1, minutes=i),
            supersede_candidate=_candidate(target.id),
        )
        db.add_all([target, carrier])
        carriers.append(carrier)
    await db.commit()
    return [c.id for c in carriers]


@pytest.mark.asyncio
async def test_measurement_k100_statement_count_is_bounded_and_timed(db_session):
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carriers = await _page_of_carriers(db_session, ctx, a, 100)

    with _statements(db_session) as page:
        result = await _recall(db_session, a, ws, ctx, carriers, k=100)
    assert len(result.results) == 100
    assert all(v == {"allowed": True} for v in _acceptances(result).values())

    with _statements(db_session) as single:
        one = await _recall(db_session, a, ws, ctx, carriers[:1], k=1)
    assert _acceptances(one) == {carriers[0]: {"allowed": True}}

    # Independent of the row count: the same statements for 1 row and 100.
    assert len(page) == len(single), (page, single)
    # One ownership query and one edge query; the rest are the savepoint and
    # the context's access checks (write access, privacy), none of which
    # grows with the page.
    assert len(_ownership_reads(page)) == 1
    assert len(_edge_reads(page)) == 1
    savepoint = [s for s in page if "SAVEPOINT" in s]
    assert len(savepoint) == 2
    access_checks = len(page) - 2 - len(savepoint)

    async def timed(runs: int = 5) -> float:
        samples = []
        for _ in range(runs):
            start = time.perf_counter()
            await _recall(db_session, a, ws, ctx, carriers, k=100)
            samples.append((time.perf_counter() - start) * 1000)
        return statistics.median(samples)

    await timed(1)  # warm-up
    with_acceptance = await timed()
    with patch.object(MemoryService, "_attach_supersede_acceptance", AsyncMock()):
        without_acceptance = await timed()

    # The measurement the issue asks for (#2508); run with -s to read it.
    print(
        f"\n#2508 measurement k=100: acceptance statements={len(page)} "
        f"(ownership=1, edges=1, savepoint=2, access checks={access_checks}); "
        f"recall median ms with={with_acceptance:.1f} without={without_acceptance:.1f} "
        f"delta={with_acceptance - without_acceptance:.1f}\n"
        + "\n".join(" ".join(s.split())[:90] for s in page)
    )
