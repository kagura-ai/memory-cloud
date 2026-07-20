"""Repository contract tests for edge.origin plumbing (Issue #722).

Verifies:
- create_or_update_edge writes the supplied origin value
- create_or_update_edge defaults to EDGE_ORIGIN_HEBBIAN
- create_edge_if_absent writes the supplied origin value
- bulk_decay_weights(only_origin=...) skips non-matching edges
- prune_weak_edges(only_origin=...) skips non-matching edges
- get_outgoing_edges(metadata_source=...) pushes the metadata filter into
  SQL (Issue #741 follow-up: tag-cooccurrence idempotency guard perf).
"""

import pytest

from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
)
from repositories.neural_edge import NeuralEdgeRepository


@pytest.mark.asyncio
async def test_create_or_update_edge_writes_origin(db_session, sample_memory_pair):
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.8,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )
    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC


@pytest.mark.asyncio
async def test_create_or_update_edge_default_origin_is_hebbian(db_session, sample_memory_pair):
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="neural_association",
        weight=0.1,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
    )
    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_HEBBIAN


@pytest.mark.asyncio
async def test_create_edge_if_absent_writes_origin(db_session, sample_memory_pair):
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    edge = await repo.create_edge_if_absent(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.7,
        confidence=0.9,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC


@pytest.mark.asyncio
async def test_bulk_decay_only_origin_hebbian_skips_semantic(
    db_session, two_edges_one_hebbian_one_semantic
):
    hebbian, semantic = two_edges_one_hebbian_one_semantic
    repo = NeuralEdgeRepository(db_session)
    user_id = hebbian.user_id

    initial_h_weight = hebbian.weight
    initial_s_weight = semantic.weight

    n = await repo.bulk_decay_weights(user_id, decay_factor=0.5, only_origin=EDGE_ORIGIN_HEBBIAN)
    # flush so ORM-layer weight updates are persisted before refresh re-reads from DB
    await db_session.flush()
    await db_session.refresh(hebbian)
    await db_session.refresh(semantic)

    assert n == 1
    assert hebbian.weight == pytest.approx(initial_h_weight * 0.5)
    assert semantic.weight == initial_s_weight  # untouched


@pytest.mark.asyncio
async def test_prune_only_origin_hebbian_skips_semantic(
    db_session, two_edges_one_hebbian_one_semantic
):
    hebbian, semantic = two_edges_one_hebbian_one_semantic
    user_id = hebbian.user_id
    # Force both below the threshold the test will use
    hebbian.weight = 0.001
    semantic.weight = 0.001
    await db_session.flush()

    repo = NeuralEdgeRepository(db_session)
    deleted = await repo.prune_weak_edges(
        user_id, weight_threshold=0.01, only_origin=EDGE_ORIGIN_HEBBIAN
    )
    assert deleted == 1

    surviving = await repo.get_edge(user_id, semantic.src_id, semantic.dst_id)
    assert surviving is not None
    assert surviving.origin == EDGE_ORIGIN_SEMANTIC


@pytest.mark.asyncio
async def test_upsert_does_not_demote_semantic_to_hebbian(db_session, sample_memory_pair):
    """Hebbian co-recall must NOT downgrade an existing semantic edge."""
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    # First write: semantic edge
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.8,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    # Second write: Hebbian co-recall upsert with default origin
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="neural_association",
        weight=0.5,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        # origin omitted — defaults to EDGE_ORIGIN_HEBBIAN
    )

    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC  # preserved, not demoted


@pytest.mark.asyncio
async def test_upsert_promotes_hebbian_to_semantic(db_session, sample_memory_pair):
    """Sleep edge_discovery upsert MUST be able to promote a hebbian edge to semantic."""
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    # First write: Hebbian edge (default)
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="neural_association",
        weight=0.05,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
    )

    # Second write: sleep edge_discovery upsert with semantic origin
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.85,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC  # promoted


@pytest.mark.asyncio
async def test_upsert_declared_wins_over_existing_semantic(db_session, sample_memory_pair):
    """#1406: a user-asserted ``declared`` upsert MUST overwrite an existing
    ``semantic`` seed origin.

    When ingest-time k-NN cold-start seeding has already linked a
    near-duplicate pair (``origin='semantic'``) and the user then declares a
    supersession over it (``remember(supersedes=...)`` -> the
    ``origin=EDGE_ORIGIN_DECLARED`` write path), the upsert must land the
    edge as ``origin='declared'``. The old sticky-origin rule preserved any
    existing non-hebbian origin, silently keeping ``semantic`` and leaving the
    declared supersede outside the #457/#741 ``protect_declared_link`` shield.
    """
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    # Existing seed edge: k-NN cold-start seeding wrote origin='semantic'.
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.5,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    # User declares a supersession over the same pair.
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="supersedes",
        weight=1.0,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_DECLARED,
    )

    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.edge_type == "supersedes"
    assert edge.origin == EDGE_ORIGIN_DECLARED  # user assertion wins over seed


@pytest.mark.asyncio
async def test_upsert_declared_survives_later_semantic_reseed(db_session, sample_memory_pair):
    """#1406 guard: an existing ``declared`` origin is NOT downgraded by a
    subsequent ``semantic`` upsert — the incoming-declared arm must not
    weaken the existing sticky protection for machine-origin writes.
    """
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    # Existing user-declared supersede edge.
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="supersedes",
        weight=1.0,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_DECLARED,
    )

    # A later machine reseed with semantic origin must not overwrite it.
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.6,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_DECLARED  # declared preserved, not demoted


@pytest.mark.asyncio
async def test_upsert_declared_wins_under_protect_link_flips_both_columns(
    db_session, sample_memory_pair
):
    """#1406 co-management: with ``protect_declared_link=True`` an incoming
    ``declared`` origin landing over an existing ``semantic`` seed must flip
    ``edge_type`` AND ``origin`` together.

    This is the property the issue calls out — a declared supersede must not
    land with ``origin='semantic'`` (outside the #457/#741 shield) while its
    ``edge_type`` becomes ``supersedes``. The ``edge_type_set`` arm keys on the
    EXISTING origin ('semantic', not 'declared') so it takes the incoming
    edge_type, and the ``origin_set`` arm keys on the INCOMING origin
    ('declared') so it takes 'declared' — both columns move as one.
    """
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    # Existing semantic seed edge.
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.5,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    # Incoming declared supersede via a protect_declared_link writer.
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="supersedes",
        weight=1.0,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_DECLARED,
        protect_declared_link=True,
    )

    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.edge_type == "supersedes"  # edge_type flipped to incoming
    assert edge.origin == EDGE_ORIGIN_DECLARED  # origin flipped together


@pytest.mark.asyncio
async def test_get_outgoing_edges_metadata_source_filter(db_session, sample_memory_pair):
    """``metadata_source`` filter must be applied in SQL (#741 follow-up).

    Seeds two outgoing edges from the same src memory: one stamped with
    ``edge_metadata['source']='tag_cooccurrence'`` and one without metadata.
    Asserts:
      - ``metadata_source='tag_cooccurrence'`` returns only the stamped row.
      - ``metadata_source=None`` (default) returns both rows.
      - ``metadata_source='nonexistent'`` returns nothing.
      - Combined with ``origin=EDGE_ORIGIN_HEBBIAN``, the filter is
        conjunctive (only hebbian + tag_cooccurrence rows match).
    """
    from uuid import uuid4

    from models.memory import Memory

    src, _ = sample_memory_pair
    user_id = src.user_id
    workspace_id = src.workspace_id
    context_id = src.context_id

    # Need a second destination memory so we can seed 2 outgoing edges from
    # src (the ``unique_edge`` constraint covers (user, src, dst)).
    dst_b = Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        summary="dst-b for metadata filter test",
        content="dst-b content",
        type="note",
        client="test",
    )
    db_session.add(dst_b)
    await db_session.flush()

    # Also need a non-hebbian sibling for the conjunctive arm — seed a
    # semantic edge that ALSO has the tag_cooccurrence stamp; the
    # origin+metadata_source intersection should NOT include it.
    dst_c = Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        summary="dst-c (semantic origin + tag_cooccurrence stamp)",
        content="dst-c content",
        type="note",
        client="test",
    )
    db_session.add(dst_c)
    await db_session.flush()

    repo = NeuralEdgeRepository(db_session)

    # Edge 1: tag_cooccurrence-stamped hebbian
    await repo.create_edge_if_absent(
        user_id=user_id,
        src_id=src.id,
        dst_id=sample_memory_pair[1].id,
        edge_type="neural_association",
        weight=0.25,
        confidence=0.5,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
        edge_metadata={"source": "tag_cooccurrence"},
    )
    # Edge 2: plain hebbian (no metadata)
    await repo.create_edge_if_absent(
        user_id=user_id,
        src_id=src.id,
        dst_id=dst_b.id,
        edge_type="neural_association",
        weight=0.30,
        confidence=0.9,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
    )
    # Edge 3: semantic-origin tag_cooccurrence-stamped (would mismatch the
    # conjunctive filter if SQL did NOT AND origin + metadata_source).
    await repo.create_edge_if_absent(
        user_id=user_id,
        src_id=src.id,
        dst_id=dst_c.id,
        edge_type="related_to",
        weight=0.40,
        confidence=1.0,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
        edge_metadata={"source": "tag_cooccurrence"},
    )

    # 1) metadata_source=None → all 3 rows
    all_out = await repo.get_outgoing_edges(
        user_id=user_id,
        src_id=src.id,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
    )
    assert len(all_out) == 3, "default (no filter) must return every outgoing edge"

    # 2) metadata_source='tag_cooccurrence' → 2 stamped rows (hebbian + semantic)
    stamped = await repo.get_outgoing_edges(
        user_id=user_id,
        src_id=src.id,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
        metadata_source="tag_cooccurrence",
    )
    assert len(stamped) == 2, "metadata_source filter must match every stamped row"
    for e in stamped:
        assert e.edge_metadata == {"source": "tag_cooccurrence"}

    # 3) metadata_source='nonexistent' → 0 rows
    none_match = await repo.get_outgoing_edges(
        user_id=user_id,
        src_id=src.id,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
        metadata_source="nonexistent-source",
    )
    assert none_match == []

    # 4) origin=hebbian AND metadata_source='tag_cooccurrence' →
    #    exactly the stamped hebbian row (Edge 1), NOT the semantic-stamped
    #    one (Edge 3) and NOT the unstamped hebbian (Edge 2). This proves
    #    the filter is conjunctive.
    intersected = await repo.get_outgoing_edges(
        user_id=user_id,
        src_id=src.id,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
        origin=EDGE_ORIGIN_HEBBIAN,
        metadata_source="tag_cooccurrence",
    )
    assert len(intersected) == 1
    assert intersected[0].origin == EDGE_ORIGIN_HEBBIAN
    assert intersected[0].edge_metadata == {"source": "tag_cooccurrence"}

    # 5) limit=1 must return at most 1 row (idempotency-guard early-out).
    early_out = await repo.get_outgoing_edges(
        user_id=user_id,
        src_id=src.id,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
        origin=EDGE_ORIGIN_HEBBIAN,
        metadata_source="tag_cooccurrence",
        limit=1,
    )
    assert len(early_out) == 1


@pytest.mark.asyncio
async def test_get_incoming_edges_metadata_source_filter(db_session, sample_memory_pair):
    """Symmetric metadata_source filter on get_incoming_edges (#741 follow-up).

    Mirrors the outgoing test on a single incoming edge to keep the
    fixture footprint small — the SQL builder is identical to
    get_outgoing_edges, so a thinner positive/negative pair is sufficient
    to pin the kwarg is wired through.
    """
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    await repo.create_edge_if_absent(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="neural_association",
        weight=0.2,
        confidence=0.5,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        edge_metadata={"source": "tag_cooccurrence"},
    )

    # Positive: stamped incoming edge surfaces.
    match = await repo.get_incoming_edges(
        user_id=src.user_id,
        dst_id=dst.id,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        metadata_source="tag_cooccurrence",
    )
    assert len(match) == 1
    assert match[0].edge_metadata == {"source": "tag_cooccurrence"}

    # Negative: a non-matching source returns nothing even though the row
    # exists (proves the filter is actually pushed into SQL).
    no_match = await repo.get_incoming_edges(
        user_id=src.user_id,
        dst_id=dst.id,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        metadata_source="other-source",
    )
    assert no_match == []
