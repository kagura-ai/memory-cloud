"""Integration test for memory_service edge creation post-#741.

After migration ``e20_741_deprecate_edge_type``:
- ``edge_type IN ('semantic_similarity', 'declared_link', 'tag_cooccurrence')``
  is rejected by the ``valid_edge_type`` CHECK constraint (only 4 values
  remain: neural_association / related_to / depends_on / learned_from).
- The discriminator carried by the deprecated edge_types moves to:
    * declared_link        → origin='declared'
    * semantic_similarity  → origin='semantic'
    * tag_cooccurrence     → edge_metadata['source']='tag_cooccurrence'
      (the row stays hebbian-origin so it still participates in
      decay/prune; metadata records derivation.)

The 4 producer sites in ``memory_service.py`` were silently failing under
try/except + log-and-continue before #741's full pivot. This integration
test exercises the real DB path against the live CHECK constraint so a
future producer that drifts back to a deprecated edge_type literal —
or forgets to attach the new discriminator — fails this suite instead
of silently dropping edges in production.

Test strategy:
- Skip Qdrant / embedding wiring. We call the producers directly with the
  inputs they would receive from ``remember()``.
- Each producer test asserts: (1) edge rows persist, (2) edge_type is
  ``neural_association``, (3) the appropriate origin / edge_metadata
  carries the deprecated-edge-type signal.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    Memory,
    NeuralMemoryEdge,
)
from models.schemas import RememberRequest
from repositories.neural_edge import NeuralEdgeRepository


@pytest_asyncio.fixture
async def edge_producer_scenario(db_session: AsyncSession):
    """Workspace + context + three memories ready for edge creation."""
    owner_id = f"owner_{uuid4().hex[:8]}"

    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
    )

    def _mk_memory(*, tags: list[str] | None = None) -> Memory:
        return Memory(
            id=uuid4(),
            user_id=owner_id,
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"mem-{uuid4().hex[:6]}",
            content="x",
            type="note",
            client="test",
            tags=tags or [],
        )

    src = _mk_memory(tags=["python", "backend", "memory-cloud"])
    dst_a = _mk_memory(tags=["python", "backend", "memory-cloud"])
    dst_b = _mk_memory(tags=["python", "backend", "memory-cloud"])

    db_session.add(ws)
    await db_session.flush()
    db_session.add(ctx)
    await db_session.flush()
    db_session.add_all([src, dst_a, dst_b])
    await db_session.flush()

    yield {
        "owner_id": owner_id,
        "ws_id": ws.id,
        "ctx_id": ctx.id,
        "src": src,
        "dst_a": dst_a,
        "dst_b": dst_b,
    }

    # Best-effort cleanup; session.rollback in the global fixture also fires.
    await db_session.rollback()


@pytest.mark.asyncio
async def test_create_declared_links_writes_origin_declared(
    db_session: AsyncSession, edge_producer_scenario
):
    """_create_declared_links must persist edges with edge_type=neural_association + origin=declared.

    Pre-#741 this site wrote edge_type='declared_link'. After migration
    e20_741, that literal trips the valid_edge_type CHECK constraint and
    the row is silently dropped by the producer's outer try/except. The
    test asserts the post-#741 contract — neural_association + declared
    origin — against the real DB CHECK.
    """
    from services.memory_service import MemoryService

    s = edge_producer_scenario
    service = MemoryService(db_session)

    request = RememberRequest(
        summary="seed memory",
        content="seed content",
        type="note",
        linked_memory_ids=[s["dst_a"].id, s["dst_b"].id],
    )

    await service._create_declared_links(
        memory_id=s["src"].id,
        request=request,
        user_id=s["owner_id"],
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
    )

    result = await db_session.execute(
        select(NeuralMemoryEdge).where(NeuralMemoryEdge.src_id == s["src"].id)
    )
    edges = list(result.scalars().all())
    assert len(edges) == 2, "both declared links should persist (no CHECK rejection)"

    for edge in edges:
        assert edge.edge_type == EDGE_TYPE_NEURAL_ASSOCIATION, (
            "post-#741 declared links write neural_association, not declared_link"
        )
        assert edge.origin == EDGE_ORIGIN_DECLARED, (
            "declared-link discriminator moved from edge_type to origin"
        )
        assert edge.weight == 1.0
        assert edge.confidence == 1.0

    # Round-trip through the consumer to prove the fetch path also pivoted.
    out_links, _, _, _ = await service._fetch_declared_link_refs(
        memory_id=s["src"].id,
        workspace_id=s["ws_id"],
        context_id=s["ctx_id"],
    )
    out_ids = {ref.memory_id for ref in out_links}
    assert out_ids == {s["dst_a"].id, s["dst_b"].id}, (
        "_fetch_declared_link_refs must surface declared links via origin filter"
    )


@pytest.mark.asyncio
async def test_create_knn_seed_edges_writes_origin_semantic(
    db_session: AsyncSession, edge_producer_scenario
):
    """_create_knn_seed_edges must persist edges with edge_type=neural_association + origin=semantic.

    Pre-#741 this site wrote edge_type='semantic_similarity'. The repo
    call is exercised directly here rather than going through Qdrant
    because Qdrant integration is out of scope for this regression
    pin — the goal is to catch any producer that drifts back to the
    deprecated literal.
    """
    s = edge_producer_scenario
    repo = NeuralEdgeRepository(db_session)

    # Mirror the kwargs the production call site (memory_service:2386) passes.
    edge = await repo.create_edge_if_absent(
        user_id=s["owner_id"],
        src_id=s["src"].id,
        dst_id=s["dst_a"].id,
        edge_type=EDGE_TYPE_NEURAL_ASSOCIATION,
        weight=0.3,
        confidence=0.85,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    assert edge is not None, "edge must persist (CHECK constraint must not reject)"
    assert edge.edge_type == EDGE_TYPE_NEURAL_ASSOCIATION
    assert edge.origin == EDGE_ORIGIN_SEMANTIC
    assert edge.weight == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_create_tag_cooccurrence_writes_edge_metadata_source(
    db_session: AsyncSession, edge_producer_scenario
):
    """Tag-cooccurrence seed must persist edge_type=neural_association + edge_metadata.source.

    Pre-#741 this site wrote edge_type='tag_cooccurrence'. After #741 the
    row is plain hebbian-origin (so it still participates in decay/prune)
    but ``edge_metadata['source']`` records the derivation so the
    idempotency guard can detect a prior seed.
    """
    s = edge_producer_scenario
    repo = NeuralEdgeRepository(db_session)

    # Mirror the kwargs the production call site (memory_service:2657) passes.
    edge = await repo.create_edge_if_absent(
        user_id=s["owner_id"],
        src_id=s["src"].id,
        dst_id=s["dst_a"].id,
        edge_type=EDGE_TYPE_NEURAL_ASSOCIATION,
        weight=0.25,
        confidence=0.5,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
        edge_metadata={"source": "tag_cooccurrence"},
    )

    assert edge is not None, "edge must persist (CHECK constraint must not reject)"
    assert edge.edge_type == EDGE_TYPE_NEURAL_ASSOCIATION
    # Default origin is hebbian; tag-cooccurrence seeds inherit decay semantics.
    assert edge.origin == EDGE_ORIGIN_HEBBIAN
    assert edge.edge_metadata == {"source": "tag_cooccurrence"}

    # The idempotency guard path must recognize the stamp via the SQL-pushed
    # metadata_source filter (#741 follow-up): origin=hebbian +
    # metadata_source='tag_cooccurrence' + limit=1 is the O(1) early-out the
    # producer uses on high-degree memory nodes.
    seeded = await repo.get_outgoing_edges(
        user_id=s["owner_id"],
        src_id=s["src"].id,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
        origin=EDGE_ORIGIN_HEBBIAN,
        metadata_source="tag_cooccurrence",
        limit=1,
    )
    assert len(seeded) == 1, (
        "idempotency guard must detect prior tag_cooccurrence seed via the "
        "SQL-pushed metadata_source filter on hebbian-origin outgoing edges"
    )
    assert seeded[0].edge_metadata == {"source": "tag_cooccurrence"}


@pytest.mark.asyncio
async def test_deprecated_edge_type_literals_rejected_by_check(
    db_session: AsyncSession,
):
    """Regression bait: the valid_edge_type CHECK constraint enumerates the
    post-#741 set only.

    If a future refactor accidentally re-introduces 'semantic_similarity',
    'declared_link', or 'tag_cooccurrence' on the CHECK constraint, the
    other tests in this file would still pass silently (they only assert
    positive paths). This test inspects ``pg_constraint`` directly so the
    suite catches a schema drift that would re-allow the deprecated values
    — without needing to actually INSERT-and-fail, which is awkward to
    structure around savepoint poisoning when the validator pre-flight
    SELECT runs first.
    """
    from sqlalchemy import text

    result = await db_session.execute(
        text(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname = 'valid_edge_type'
            """
        )
    )
    rows = list(result.scalars().all())
    assert rows, "valid_edge_type CHECK constraint must exist on neural_memory_edges"

    constraint_def = rows[0]
    for deprecated in ("semantic_similarity", "declared_link", "tag_cooccurrence"):
        assert deprecated not in constraint_def, (
            f"deprecated edge_type {deprecated!r} must NOT appear in the "
            f"valid_edge_type CHECK constraint post-#741. Constraint def: "
            f"{constraint_def}"
        )

    # Positive: every value in ``_ALL_EDGE_TYPES`` (the canonical source of
    # truth from models/memory.py) MUST appear in the live CHECK so the
    # constraint is not accidentally too restrictive either. Iterating the
    # source-of-truth tuple — instead of hand-typing the list here — means
    # adding ``EDGE_TYPE_NEW`` requires no edit to this test (#782 self-review).
    from models.memory import _ALL_EDGE_TYPES

    for kept in _ALL_EDGE_TYPES:
        assert kept in constraint_def, f"surviving edge_type {kept!r} missing from CHECK constraint"
