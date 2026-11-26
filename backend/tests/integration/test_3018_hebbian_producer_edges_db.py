"""A Hebbian update leaves another producer's edge alone (#3018).

Real PostgreSQL: the rule lives in the ``ON CONFLICT`` clause of the Hebbian
upsert (``NeuralEdgeRepository.upsert_hebbian_edge``) and in the guarded
delete of the learner's prune branch (``prune_hebbian_edge``), so only a real
database exercises it.

Producer-owned rows (not a seed, not hebbian): an edge Sleep edge discovery
confirmed, a ``supersedes`` edge whatever its origin, and a declared edge.
A Hebbian update changes none of their columns, except that a declared
edge's weight keeps moving; it never removes one of them. Seeds (k-NN
nearest-neighbour seeds and tag co-occurrence seeds) and hebbian rows stay
reinforceable and keep their metadata.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import and_, select

from models.auth import Context, Workspace
from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    Memory,
    NeuralMemoryEdge,
)
from models.sleep import SleepRelatedIndex
from neural.config import NeuralMemoryConfig
from neural.hebbian import HebbianLearner
from neural.models import ActivationState, NeuralMemoryNode
from repositories.neural_edge import NeuralEdgeRepository
from services.graph_service import GraphService
from services.sleep.edge_discovery import ConfirmedEdge, EdgeDiscoveryPhase
from services.sleep.related_index import RelatedIndexPhase, candidate_links
from services.sleep.reporter import SleepBudget, SleepReporter
from utils.datetime import utcnow

EDGES = NeuralMemoryEdge.__table__

# Every column a Hebbian update must leave alone on a frozen row.
_FROZEN_COLUMNS = (
    "edge_type",
    "origin",
    "weight",
    "confidence",
    "metadata",
    "created_at",
    "last_updated",
)


async def _scenario(db, n: int = 2) -> tuple[str, Workspace, Context, list[Memory]]:
    owner = f"owner_{uuid4().hex[:8]}"
    ws = Workspace(id=uuid4(), name=f"ws-{uuid4().hex[:6]}", plan_name="pro", owner_user_id=owner)
    db.add(ws)
    await db.flush()
    ctx = Context(id=uuid4(), workspace_id=ws.id, name=f"c-{uuid4().hex[:6]}", created_by=owner)
    db.add(ctx)
    await db.flush()
    mems = [
        Memory(
            id=uuid4(),
            user_id=owner,
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"Fictional memory {i}",
            content=f"Fictional content {i}",
            type="fact",
            client="pytest",
            tags=["alpha", "beta"],
        )
        for i in range(n)
    ]
    db.add_all(mems)
    await db.flush()
    return owner, ws, ctx, mems


async def _row(db, owner: str, src: UUID, dst: UUID) -> dict[str, Any] | None:
    """Every column of one directed row, read through Core (no identity map)."""
    found = (
        (
            await db.execute(
                select(EDGES).where(
                    and_(EDGES.c.user_id == owner, EDGES.c.src_id == src, EDGES.c.dst_id == dst)
                )
            )
        )
        .mappings()
        .first()
    )
    return dict(found) if found is not None else None


async def _seed_row(db, owner, ws, ctx, src, dst, spec: dict[str, Any]) -> None:
    """Write ``spec`` as an existing row the way its producer does (plain upsert)."""
    await NeuralEdgeRepository(db).create_or_update_edge(
        user_id=owner,
        src_id=src,
        dst_id=dst,
        edge_type=spec["edge_type"],
        weight=spec["weight"],
        confidence=spec["confidence"],
        edge_metadata=spec["metadata"],
        workspace_id=str(ws.id),
        context_id=str(ctx.id),
        origin=spec["origin"],
        return_fresh_edge=False,
    )
    await db.flush()


async def _hebbian_upsert(db, owner, ws, ctx, src, dst, weight: float) -> bool:
    written = await NeuralEdgeRepository(db).upsert_hebbian_edge(
        user_id=owner,
        src_id=src,
        dst_id=dst,
        weight=weight,
        confidence=1.0,
        edge_metadata={"created_by": "hebbian_learning"},
        workspace_id=str(ws.id),
        context_id=str(ctx.id),
    )
    await db.flush()
    return written


async def _hebbian_recall_pass(db, owner, ws, ctx, mems: list[Memory]) -> int:
    """The recall learning pass as ``MemoryService._recall_learn_on_session``
    runs it (GraphService + HebbianLearner), minus the Redis tracker, the lock
    and the calibrated gate, which do not change the write."""
    config = await NeuralMemoryConfig.from_db(db)
    graph = GraphService(user_id=owner, db=db, workspace_id=str(ws.id), context_id=str(ctx.id))
    learner = HebbianLearner(graph, config, None)
    emb = [1.0, 0.0, 0.0, 0.0]
    nodes = {
        str(m.id): NeuralMemoryNode(
            id=str(m.id),
            user_id=owner,
            kind=m.type,
            text=m.summary,
            embedding=emb,
            created_at=m.created_at or utcnow(),
            importance=0.5,
            confidence=1.0,
        )
        for m in mems
    }
    await learner.queue_update(
        owner, [ActivationState(node_id=nid, activation=0.9) for nid in nodes], nodes
    )
    updated = await learner.apply_updates(owner)
    await db.flush()
    return updated


# --------------------------------------------------------------------------
# Repository level: the ON CONFLICT rule, both directions of a pair
# --------------------------------------------------------------------------

_DISCOVERY = {
    "edge_type": "related_to",
    "origin": EDGE_ORIGIN_SEMANTIC,
    "weight": 0.83,
    "confidence": 0.8,
    "metadata": {"source": "sleep_edge_discovery"},
}

FROZEN_SPECS = {
    "edge_discovery": _DISCOVERY,
    "semantic_typed_without_marker": {
        "edge_type": "depends_on",
        "origin": EDGE_ORIGIN_SEMANTIC,
        "weight": 0.71,
        "confidence": 0.9,
        "metadata": None,
    },
    "semantic_association_with_discovery_marker": {
        "edge_type": "neural_association",
        "origin": EDGE_ORIGIN_SEMANTIC,
        "weight": 0.66,
        "confidence": 0.7,
        "metadata": {"source": "sleep_edge_discovery"},
    },
    "sleep_dedup_supersedes": {
        "edge_type": "supersedes",
        "origin": EDGE_ORIGIN_SEMANTIC,
        "weight": 1.0,
        "confidence": 1.0,
        "metadata": None,
    },
    "hebbian_origin_supersedes": {
        "edge_type": "supersedes",
        "origin": EDGE_ORIGIN_HEBBIAN,
        "weight": 1.0,
        "confidence": 1.0,
        "metadata": None,
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(FROZEN_SPECS))
async def test_hebbian_upsert_leaves_a_producer_edge_untouched(db_session, case):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    spec = FROZEN_SPECS[case]
    for src, dst in ((a.id, b.id), (b.id, a.id)):
        await _seed_row(db, owner, ws, ctx, src, dst, spec)
    before = {d: await _row(db, owner, *d) for d in ((a.id, b.id), (b.id, a.id))}

    for src, dst in ((a.id, b.id), (b.id, a.id)):
        assert await _hebbian_upsert(db, owner, ws, ctx, src, dst, 1.7) is False

    for direction, row in before.items():
        after = await _row(db, owner, *direction)
        assert after is not None
        assert {c: after[c] for c in _FROZEN_COLUMNS} == {c: row[c] for c in _FROZEN_COLUMNS}


@pytest.mark.asyncio
@pytest.mark.parametrize("edge_type", ["related_to", "supersedes"])
async def test_hebbian_upsert_moves_only_a_declared_edges_weight(db_session, edge_type):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    spec = {
        "edge_type": edge_type,
        "origin": EDGE_ORIGIN_DECLARED,
        "weight": 1.0,
        "confidence": 0.7,
        "metadata": {"note": "user asserted"},
    }
    for src, dst in ((a.id, b.id), (b.id, a.id)):
        await _seed_row(db, owner, ws, ctx, src, dst, spec)
    before = {d: await _row(db, owner, *d) for d in ((a.id, b.id), (b.id, a.id))}

    for src, dst in ((a.id, b.id), (b.id, a.id)):
        assert await _hebbian_upsert(db, owner, ws, ctx, src, dst, 1.04) is True

    for direction, row in before.items():
        after = await _row(db, owner, *direction)
        assert after is not None
        assert after["weight"] == pytest.approx(1.04)
        for column in ("edge_type", "origin", "confidence", "metadata", "created_at"):
            assert after[column] == row[column], column


SEED_SPECS = {
    "knn_seed": {
        "edge_type": "neural_association",
        "origin": EDGE_ORIGIN_SEMANTIC,
        "weight": 0.3,
        "confidence": 0.82,
        "metadata": None,
    },
    "tag_cooccurrence_seed": {
        "edge_type": "neural_association",
        "origin": EDGE_ORIGIN_HEBBIAN,
        "weight": 0.25,
        "confidence": 0.5,
        "metadata": {"source": "tag_cooccurrence"},
    },
    "hebbian": {
        "edge_type": "neural_association",
        "origin": EDGE_ORIGIN_HEBBIAN,
        "weight": 0.2,
        "confidence": 1.0,
        "metadata": {"created_by": "hebbian_learning"},
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(SEED_SPECS))
async def test_hebbian_upsert_reinforces_a_seed_and_keeps_its_metadata(db_session, case):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    spec = SEED_SPECS[case]
    for src, dst in ((a.id, b.id), (b.id, a.id)):
        await _seed_row(db, owner, ws, ctx, src, dst, spec)
    before = {d: await _row(db, owner, *d) for d in ((a.id, b.id), (b.id, a.id))}

    for src, dst in ((a.id, b.id), (b.id, a.id)):
        assert await _hebbian_upsert(db, owner, ws, ctx, src, dst, 0.61) is True

    for direction, row in before.items():
        after = await _row(db, owner, *direction)
        assert after is not None
        assert after["weight"] == pytest.approx(0.61)
        for column in ("edge_type", "origin", "metadata", "created_at"):
            assert after[column] == row[column], column


@pytest.mark.asyncio
async def test_tag_seed_marker_survives_so_a_later_remember_does_not_reseed(db_session):
    """The tag co-occurrence idempotency guard (``get_outgoing_edges`` with
    ``metadata_source='tag_cooccurrence'``) still finds the seed after co-recall."""
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    await _seed_row(db, owner, ws, ctx, a.id, b.id, SEED_SPECS["tag_cooccurrence_seed"])

    await _hebbian_upsert(db, owner, ws, ctx, a.id, b.id, 0.9)

    seeded = await NeuralEdgeRepository(db).get_outgoing_edges(
        owner,
        a.id,
        workspace_id=str(ws.id),
        context_id=str(ctx.id),
        origin=EDGE_ORIGIN_HEBBIAN,
        metadata_source="tag_cooccurrence",
        limit=1,
    )
    assert len(seeded) == 1


@pytest.mark.asyncio
async def test_hebbian_upsert_creates_a_missing_edge_as_hebbian(db_session):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)

    assert await _hebbian_upsert(db, owner, ws, ctx, a.id, b.id, 0.12) is True

    row = await _row(db, owner, a.id, b.id)
    assert row is not None
    assert row["origin"] == EDGE_ORIGIN_HEBBIAN
    assert row["edge_type"] == "neural_association"
    assert row["weight"] == pytest.approx(0.12)
    assert row["metadata"] == {"created_by": "hebbian_learning"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(FROZEN_SPECS))
async def test_hebbian_prune_never_removes_a_producer_edge(db_session, case):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    spec = FROZEN_SPECS[case]
    await _seed_row(db, owner, ws, ctx, a.id, b.id, spec)
    before = await _row(db, owner, a.id, b.id)
    repo = NeuralEdgeRepository(db)

    removed = await repo.prune_hebbian_edge(
        owner, a.id, b.id, weight=0.001, workspace_id=str(ws.id), context_id=str(ctx.id)
    )
    await db.flush()

    assert removed is False
    after = await _row(db, owner, a.id, b.id)
    assert after is not None and before is not None
    assert {c: after[c] for c in _FROZEN_COLUMNS} == {c: before[c] for c in _FROZEN_COLUMNS}


@pytest.mark.asyncio
async def test_hebbian_prune_keeps_a_declared_edge_and_moves_its_weight(db_session):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    spec = {
        "edge_type": "related_to",
        "origin": EDGE_ORIGIN_DECLARED,
        "weight": 0.02,
        "confidence": 0.7,
        "metadata": None,
    }
    await _seed_row(db, owner, ws, ctx, a.id, b.id, spec)
    repo = NeuralEdgeRepository(db)

    removed = await repo.prune_hebbian_edge(
        owner, a.id, b.id, weight=0.004, workspace_id=str(ws.id), context_id=str(ctx.id)
    )
    await db.flush()

    assert removed is False
    after = await _row(db, owner, a.id, b.id)
    assert after is not None
    assert after["weight"] == pytest.approx(0.004)
    assert (after["edge_type"], after["origin"], after["confidence"]) == (
        "related_to",
        EDGE_ORIGIN_DECLARED,
        0.7,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(SEED_SPECS))
async def test_hebbian_prune_removes_a_seed_or_hebbian_edge(db_session, case):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    await _seed_row(db, owner, ws, ctx, a.id, b.id, SEED_SPECS[case])
    repo = NeuralEdgeRepository(db)

    removed = await repo.prune_hebbian_edge(
        owner, a.id, b.id, weight=0.001, workspace_id=str(ws.id), context_id=str(ctx.id)
    )
    await db.flush()

    assert removed is True
    assert await _row(db, owner, a.id, b.id) is None


# --------------------------------------------------------------------------
# End to end: the Sleep writers, then a co-recall through HebbianLearner
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["a_to_b", "b_to_a"])
async def test_discovery_edge_survives_co_recall(db_session, reverse):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    src, dst = (b, a) if reverse else (a, b)
    await EdgeDiscoveryPhase(db, MagicMock())._persist_confirmed_edges(
        [ConfirmedEdge(src_id=src.id, dst_id=dst.id, edge_type="related_to", confidence=0.8)],
        [(src.id, dst.id, 0.83)],
        owner,
        str(ws.id),
        str(ctx.id),
        None,
        None,
    )
    await db.flush()
    before = await _row(db, owner, src.id, dst.id)
    assert before is not None

    for _ in range(3):
        await _hebbian_recall_pass(db, owner, ws, ctx, [a, b])

    after = await _row(db, owner, src.id, dst.id)
    assert after is not None
    assert {c: after[c] for c in _FROZEN_COLUMNS} == {c: before[c] for c in _FROZEN_COLUMNS}
    assert after["edge_type"] == "related_to"
    assert after["weight"] == pytest.approx(0.83)
    assert after["metadata"] == {"source": "sleep_edge_discovery"}
    # The other direction had no row: co-recall creates its own hebbian edge.
    other = await _row(db, owner, dst.id, src.id)
    assert other is not None and other["origin"] == EDGE_ORIGIN_HEBBIAN


@pytest.mark.asyncio
async def test_related_index_pairs_stay_eligible_after_co_recall(db_session, monkeypatch):
    """(a, b) is not indexed yet when co-recall touches it: the build still
    picks it up. (c, d) is indexed first: the read still returns it."""
    monkeypatch.setenv("SLEEP_RELATED_INDEX_ENABLED", "true")
    db = db_session
    owner, ws, ctx, (a, b, c, d) = await _scenario(db, n=4)
    phase = EdgeDiscoveryPhase(db, MagicMock())
    await phase._persist_confirmed_edges(
        [ConfirmedEdge(src_id=c.id, dst_id=d.id, edge_type="related_to", confidence=0.8)],
        [(c.id, d.id, 0.81)],
        owner,
        str(ws.id),
        str(ctx.id),
        None,
        None,
    )
    await db.flush()
    report = await SleepReporter(db).create_report(owner, str(ws.id), str(ctx.id))
    await RelatedIndexPhase(db).execute(
        None, owner, str(ws.id), str(ctx.id), SleepBudget(max_memories=200), report_id=report.id
    )
    await db.flush()
    assert [
        (x.source_id, x.target_id)
        for x in await candidate_links(db, [c], context_id=ctx.id, workspace_id=ws.id)
    ] == [(c.id, d.id)]

    await phase._persist_confirmed_edges(
        [ConfirmedEdge(src_id=a.id, dst_id=b.id, edge_type="related_to", confidence=0.8)],
        [(a.id, b.id, 0.83)],
        owner,
        str(ws.id),
        str(ctx.id),
        None,
        None,
    )
    await db.flush()

    for _ in range(3):
        await _hebbian_recall_pass(db, owner, ws, ctx, [a, b])
        await _hebbian_recall_pass(db, owner, ws, ctx, [c, d])

    assert [
        (x.source_id, x.target_id)
        for x in await candidate_links(db, [c], context_id=ctx.id, workspace_id=ws.id)
    ] == [(c.id, d.id)]
    report = await SleepReporter(db).create_report(owner, str(ws.id), str(ctx.id))
    await RelatedIndexPhase(db).execute(
        None, owner, str(ws.id), str(ctx.id), SleepBudget(max_memories=200), report_id=report.id
    )
    await db.flush()
    indexed = (
        await db.scalars(
            select(SleepRelatedIndex.target_id).where(SleepRelatedIndex.source_id == a.id)
        )
    ).all()
    assert list(indexed) == [b.id]


@pytest.mark.asyncio
async def test_sleep_dedup_supersedes_edge_survives_co_recall(db_session):
    from services.sleep.dedup_merge import DedupMergePhase

    db = db_session
    owner, ws, ctx, (winner, loser) = await _scenario(db)
    executed, _ = await DedupMergePhase(db, MagicMock())._execute_shadow_merge(
        winner, loser, owner, str(ws.id), str(ctx.id)
    )
    await db.flush()
    assert executed is True
    before = await _row(db, owner, winner.id, loser.id)
    assert before is not None and before["edge_type"] == "supersedes"

    for _ in range(3):
        await _hebbian_recall_pass(db, owner, ws, ctx, [winner, loser])

    after = await _row(db, owner, winner.id, loser.id)
    assert after is not None
    assert {c: after[c] for c in _FROZEN_COLUMNS} == {c: before[c] for c in _FROZEN_COLUMNS}


@pytest.mark.asyncio
async def test_co_recall_still_strengthens_a_tag_seed_and_keeps_its_marker(db_session):
    db = db_session
    owner, ws, ctx, (a, b) = await _scenario(db)
    for src, dst in ((a.id, b.id), (b.id, a.id)):
        await NeuralEdgeRepository(db).create_edge_if_absent(
            user_id=owner,
            src_id=src,
            dst_id=dst,
            edge_type="neural_association",
            weight=0.25,
            confidence=0.5,
            workspace_id=str(ws.id),
            context_id=str(ctx.id),
            edge_metadata={"source": "tag_cooccurrence"},
        )
    await db.flush()

    await _hebbian_recall_pass(db, owner, ws, ctx, [a, b])

    for src, dst in ((a.id, b.id), (b.id, a.id)):
        row = await _row(db, owner, src, dst)
        assert row is not None
        assert row["weight"] > 0.25
        assert row["origin"] == EDGE_ORIGIN_HEBBIAN
        assert row["metadata"] == {"source": "tag_cooccurrence"}
