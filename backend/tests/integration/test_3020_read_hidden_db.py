"""#3020 read side, ``hidden``: a link Sleep applied hides like an accepted one.

One context holds three linked pairs: one applied by the real supersede-apply
phase, an accepted link (``create_declared_edge``) and a dedup shadow edge
(origin ``semantic``, NULL metadata), plus an unrelated memory. Every reader
treats the three alike: default recall, the user facts lane, the
``load_guardrails`` index lane and the related index leave the older memory
out; ``include_superseded=true`` and ``order='date'`` return it with
``superseded_by``. An indexed link to the unrelated memory is the related
index's control: it is returned, so an empty answer for the older memories is
the supersession rule, not an index that returns nothing.

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
    related_control: UUID  # the memory an indexed related_to edge links to ``other``

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
    related_control = await _index_related(db, s, other)
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
        related_control=related_control,
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
async def test_default_recall_leaves_every_older_memory_out(committed):
    w = await build_world(committed.db, committed)

    response = await recall(committed.db, w, everything(w))

    ids = [r.memory_id for r in response.results]
    assert set(ids) == {w.applied[0], w.accepted[0], w.shadow[0], w.other}
    newer = next(r for r in response.results if r.memory_id == w.applied[0])
    assert newer.folded is None
    assert newer.supersede_candidate is None


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kw", [{"include_superseded": True}, {"order": "date"}])
async def test_the_full_history_returns_every_older_memory_marked(committed, request_kw):
    w = await build_world(committed.db, committed)

    response = await recall(committed.db, w, everything(w), **request_kw)

    by_id = {r.memory_id: r for r in response.results}
    assert set(by_id) == set(everything(w))
    for newer, older in w.pairs:
        assert by_id[older].superseded_by == newer
        assert by_id[newer].superseded_by is None


@pytest.mark.asyncio
async def test_the_user_facts_lane_never_returns_an_older_memory(committed):
    w = await build_world(committed.db, committed)

    response = await recall(committed.db, w, [w.other], lane=[*w.olders, w.applied[0]])

    assert [r.memory_id for r in response.user_facts] == [w.applied[0]]


@pytest.mark.asyncio
async def test_the_session_index_omits_every_older_memory(committed):
    w = await build_world(committed.db, committed)

    rows, _ = await MemoryRepository(committed.db).list_session_index(w.s.ws.id, w.s.ctx.id, 60)

    listed = {row.id for row in rows}
    assert not listed & set(w.olders)
    assert {newer for newer, _ in w.pairs} <= listed


@pytest.mark.asyncio
async def test_the_related_index_excludes_links_touching_an_older_memory(committed):
    w = await build_world(committed.db, committed)
    db = committed.db

    async def targets(seed_id: UUID) -> list[UUID]:
        seed = await db.get(Memory, seed_id)
        links = await candidate_links(db, [seed], context_id=w.s.ctx.id, workspace_id=w.s.ws.id)
        return [link.target_id for link in links]

    # The control: the same kind of indexed link to an unsuperseded memory is returned.
    assert await targets(w.related_control) == [w.other]
    for older, neighbour in w.related.items():
        assert await targets(neighbour) == [], older
