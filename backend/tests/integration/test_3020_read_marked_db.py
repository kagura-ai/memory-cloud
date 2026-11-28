"""#3020 read side, ``marked``: a link Sleep applied keeps the older memory, marked.

One context holds three linked pairs: one applied by the real supersede-apply
phase, an accepted link (``create_declared_edge``) and a dedup shadow edge
(origin ``semantic``, NULL metadata), plus an unrelated memory. The applied
link does not hide: recall and the user facts lane return its older memory as
a row of its own with ``superseded_by`` (with or without the newer one in the
pool, and nothing folds for the pair), and the ``load_guardrails`` index and
the related index keep it. The two controls hide everywhere, and so does an
applied link a person re-declared and a semantic link another writer marked
with another source.

Recall commits its session and the lane reads through a session of its own,
so the rows are committed and deleted after each test (``committed_cleanup``).
``SearchService.hybrid_search`` is stubbed with each query's pool. Not
executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import null, update

from models.memory import Memory, NeuralMemoryEdge
from models.schemas import RecallRequest
from models.sleep import SleepRelatedIndex
from repositories.config_repository import ContextSearchConfigRepository
from repositories.memory import MemoryRepository
from services.edge_service import create_declared_edge
from services.memory_service import MemoryService
from services.search_service import SearchService
from services.sleep.related_index import candidate_links, evidence
from services.user_facts_lane import USER_FACTS_QUERY
from tests.integration._supersede_apply_support import (
    Scope,
    add_edge,
    committed_cleanup,
    edge_row,
    memory,
    new_report,
    run_phase,
    scope,
    verified_pair,
)
from utils.datetime import utcnow


@pytest_asyncio.fixture(loop_scope="session")
async def committed(db_session):
    async with committed_cleanup(db_session) as ids:
        yield ids


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.delenv("SLEEP_ARCHIVE_EXEMPT_TAGS", raising=False)
    monkeypatch.delenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", raising=False)


@dataclass
class World:
    s: Scope
    applied: tuple[UUID, UUID]  # (newer, older)
    accepted: tuple[UUID, UUID]
    shadow: tuple[UUID, UUID]
    other: UUID
    related: dict[UUID, UUID]  # older -> the memory an indexed related_to edge links to it

    @property
    def pairs(self) -> list[tuple[UUID, UUID]]:
        return [self.applied, self.accepted, self.shadow]

    @property
    def olders(self) -> list[UUID]:
        return [older for _, older in self.pairs]


async def _index_related(db, s: Scope, older: Memory) -> UUID:
    """A memory linked to ``older`` by a discovered, indexed ``related_to`` edge."""
    neighbour = memory(s, summary=f"neighbour of {older.summary}", type="note")
    db.add(neighbour)
    await db.flush()
    edge = NeuralMemoryEdge(
        user_id=s.owner,
        src_id=neighbour.id,
        dst_id=older.id,
        edge_type="related_to",
        origin="semantic",
        weight=0.8,
        confidence=1.0,
        workspace_id=s.ws.id,
        context_id=s.ctx.id,
    )
    db.add(edge)
    await db.flush()
    db.add(
        SleepRelatedIndex(
            edge_id=edge.id,
            source_id=neighbour.id,
            target_id=older.id,
            context_id=s.ctx.id,
            workspace_id=s.ws.id,
            evidence={"source": evidence(neighbour), "target": evidence(older)},
        )
    )
    await db.flush()
    return neighbour.id


async def build_world(db, ids) -> World:
    """The three linked pairs, committed, with the user facts lane on."""
    s = await scope(db, ids=ids)
    config = await ContextSearchConfigRepository(db).create_or_get(s.ctx.id)
    config.user_facts_lane = True
    base = utcnow() - timedelta(days=10)
    applied = await verified_pair(db, s, created_at=base)
    accepted = await verified_pair(db, s, created_at=base + timedelta(hours=1))
    shadow = await verified_pair(db, s, created_at=base + timedelta(hours=2))
    # The dedup control: no suggestion, a shadow edge with NULL metadata.
    await db.execute(
        update(Memory).where(Memory.id == shadow[0].id).values(supersede_candidate=null())
    )
    await add_edge(db, s, shadow[0], shadow[1], "supersedes", origin="semantic")
    # The accept control: the declared link the accept path writes.
    await create_declared_edge(
        db,
        user_id=s.owner,
        source_id=accepted[0].id,
        target_id=accepted[1].id,
        edge_type="supersedes",
        weight=1.0,
        confidence=1.0,
        workspace_id=s.ws_id,
        context_id=s.ctx_id,
        overwrite=False,
    )
    other = memory(s, summary="the team meets on Tuesdays", type="note")
    db.add(other)
    await db.flush()
    related = {
        older.id: await _index_related(db, s, older) for _, older in (applied, accepted, shadow)
    }
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    _, result = await run_phase(db, s, report)
    assert result.details["applied"] == 1
    link = await edge_row(db, s.owner, applied[0].id, applied[1].id)
    assert link.edge_metadata == {"source": "sleep_supersede_apply", "report_id": str(report.id)}
    await db.commit()
    return World(
        s=s,
        applied=(applied[0].id, applied[1].id),
        accepted=(accepted[0].id, accepted[1].id),
        shadow=(shadow[0].id, shadow[1].id),
        other=other.id,
        related=related,
    )


class _Search:
    """``SearchService.hybrid_search``: the main pool, and the lane's by its query."""

    def __init__(self, main: list[UUID], lane: list[UUID] | None = None) -> None:
        self.main, self.lane = main, lane or []

    async def __call__(self, _svc, *, query: str, **kwargs: Any) -> list[dict]:
        pool = self.lane if query == USER_FACTS_QUERY else self.main
        return [{"id": str(m), "score": 0.9 - 0.02 * i} for i, m in enumerate(pool)]


async def recall(db, w: World, main: list[UUID], *, lane=None, **request_kw):
    search = _Search(main, lane)

    async def fake(svc, **kwargs):
        return await search(svc, **kwargs)

    with patch.object(SearchService, "hybrid_search", new=fake):
        return await MemoryService(db).recall(
            RecallRequest(query="where is the office", k=10, **request_kw),
            user_id=w.s.owner,
            current_context_id=w.s.ctx.id,
            current_workspace_id=w.s.ws.id,
            user_facts_lane=lane is not None,
        )


def everything(w: World) -> list[UUID]:
    return [m for pair in w.pairs for m in pair] + [w.other]


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_both_in_the_pool_both_are_rows_and_the_older_is_marked(committed):
    w = await build_world(committed.db, committed)
    newer, older = w.applied

    response = await recall(committed.db, w, everything(w))

    by_id = {r.memory_id: r for r in response.results}
    assert set(by_id) == {newer, older, w.accepted[0], w.shadow[0], w.other}
    assert by_id[older].superseded_by == newer
    assert by_id[newer].superseded_by is None
    assert by_id[newer].folded is None
    assert by_id[newer].supersede_candidate is None


@pytest.mark.asyncio
async def test_the_older_memory_alone_is_returned_marked(committed):
    w = await build_world(committed.db, committed)
    newer, older = w.applied

    response = await recall(committed.db, w, [older, w.other])

    by_id = {r.memory_id: r for r in response.results}
    assert set(by_id) == {older, w.other}
    assert by_id[older].superseded_by == newer


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kw", [{"include_superseded": True}, {"order": "date"}])
async def test_the_full_history_returns_every_older_memory_marked(committed, request_kw):
    w = await build_world(committed.db, committed)

    response = await recall(committed.db, w, everything(w), **request_kw)

    by_id = {r.memory_id: r for r in response.results}
    assert set(by_id) == set(everything(w))
    for newer, older in w.pairs:
        assert by_id[older].superseded_by == newer


@pytest.mark.asyncio
async def test_the_user_facts_lane_returns_the_applied_older_memory_marked(committed):
    w = await build_world(committed.db, committed)
    newer, older = w.applied

    response = await recall(committed.db, w, [w.other], lane=w.olders)

    assert [r.memory_id for r in response.user_facts] == [older]
    assert response.user_facts[0].superseded_by == newer


@pytest.mark.asyncio
async def test_the_session_index_keeps_only_the_applied_older_memory(committed):
    w = await build_world(committed.db, committed)

    rows, _ = await MemoryRepository(committed.db).list_session_index(w.s.ws.id, w.s.ctx.id, 60)

    listed = {row.id for row in rows}
    assert w.applied[1] in listed
    assert w.accepted[1] not in listed and w.shadow[1] not in listed


@pytest.mark.asyncio
async def test_the_related_index_keeps_links_touching_the_applied_older_memory(committed):
    w = await build_world(committed.db, committed)
    db = committed.db

    found = {}
    for older, neighbour in w.related.items():
        seed = await db.get(Memory, neighbour)
        links = await candidate_links(db, [seed], context_id=w.s.ctx.id, workspace_id=w.s.ws.id)
        found[older] = [link.target_id for link in links]
    assert found == {w.applied[1]: [w.applied[1]], w.accepted[1]: [], w.shadow[1]: []}


@pytest.mark.asyncio
async def test_an_applied_link_a_person_re_declared_hides(committed):
    w = await build_world(committed.db, committed)
    db = committed.db
    newer, older = w.applied
    await create_declared_edge(
        db,
        user_id=w.s.owner,
        source_id=newer,
        target_id=older,
        edge_type="supersedes",
        weight=1.0,
        confidence=1.0,
        workspace_id=w.s.ws_id,
        context_id=w.s.ctx_id,
        overwrite=True,
    )
    await db.commit()

    response = await recall(db, w, everything(w))

    assert older not in {r.memory_id for r in response.results}


@pytest.mark.asyncio
async def test_a_semantic_link_naming_another_source_hides(committed):
    w = await build_world(committed.db, committed)
    db = committed.db
    newer, older = await verified_pair(db, w.s, created_at=utcnow() - timedelta(days=5))
    await db.execute(update(Memory).where(Memory.id == newer.id).values(supersede_candidate=null()))
    await add_edge(
        db, w.s, newer, older, "supersedes", origin="semantic", metadata={"source": "other"}
    )
    newer_id, older_id = newer.id, older.id
    await db.commit()

    response = await recall(db, w, [newer_id, older_id, w.other])

    assert {r.memory_id for r in response.results} == {newer_id, w.other}
