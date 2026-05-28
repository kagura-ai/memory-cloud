"""Tests for GraphService.EDGE_TYPES drift fix (#506, updated for #741 / #782).

PR #460 introduced ``EDGE_TYPE_*`` constants in ``models/memory.py`` to
mirror the DB CHECK constraint values. PR #507 (#461 Phase A) wired
``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` and the LLM-emittable
subset in ``services/sleep/edge_discovery.py`` to those constants.

#506 closed the last defining-site drift by adding ``tag_cooccurrence`` to
``GraphService.EDGE_TYPES``. #741 then collapsed the edge_type axis to four
values (``neural_association`` / ``related_to`` / ``depends_on`` /
``learned_from``) by moving provenance to the ``origin`` axis and tag
seeding to ``edge_metadata['source']``. #782 widened the set back to six by
appending two producer-asserted structural relation types
(``continues_from`` / ``references_file``) emitted by the
kagura-memory-ai-worker pipeline. The drift tests in this file pin the
current shape.

Tests pin three invariants:

1. ``GraphService.EDGE_TYPES`` equals the union of the surviving
   ``EDGE_TYPE_*`` constants (matching the DB CHECK constraint), so future
   drift between the constants block and this validator set fails CI before
   the inconsistency can ship.

2. ``add_edge(rel_type="neural_association")`` succeeds — post-#741 every
   non-relational write (formerly ``semantic_similarity`` / ``declared_link``
   / ``tag_cooccurrence``) goes through ``neural_association`` and discriminates
   via ``origin`` + ``edge_metadata``.

3. ``add_edge(rel_type=<producer-asserted>)`` succeeds and, when called with
   ``origin=EDGE_ORIGIN_DECLARED``, forwards both ``edge_type`` and
   ``origin`` to the repository — the #782 producer-asserted path.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_TYPE_CONTINUES_FROM,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_REFERENCES_FILE,
    EDGE_TYPE_RELATED_TO,
)
from services.graph_service import GraphService


def test_edge_types_matches_constants() -> None:
    """``GraphService.EDGE_TYPES`` is the union of all surviving ``EDGE_TYPE_*``.

    Mirrors ``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` and the DB CHECK
    constraint in ``models/memory.py::valid_edge_type``. Post-#741 the set
    is the four-element union: ``neural_association`` / ``related_to`` /
    ``depends_on`` / ``learned_from``. Adding a new edge_type must update
    both the constants block and this set in lock-step; this test enforces
    the lock.
    """
    assert GraphService.EDGE_TYPES == frozenset(
        {
            EDGE_TYPE_NEURAL_ASSOCIATION,
            EDGE_TYPE_RELATED_TO,
            EDGE_TYPE_DEPENDS_ON,
            EDGE_TYPE_LEARNED_FROM,
            EDGE_TYPE_CONTINUES_FROM,
            EDGE_TYPE_REFERENCES_FILE,
        }
    )


@pytest.mark.asyncio
async def test_add_edge_accepts_neural_association() -> None:
    """``add_edge(rel_type="neural_association")`` succeeds.

    Post-#741 ``neural_association`` is the generic catch-all for any
    provenance not expressible as a relation (Hebbian co-activation +
    cold-start seeds + tag_cooccurrence + declared link). The validator
    must accept it and forward to ``NeuralEdgeRepository``.
    """
    src_id = uuid4()
    dst_id = uuid4()

    mock_db = MagicMock()
    mock_edge_repo = MagicMock()
    mock_edge_repo.create_or_update_edge = AsyncMock()

    graph = GraphService.__new__(GraphService)
    graph.user_id = "test_user_506"
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = mock_db
    graph.edge_repo = mock_edge_repo

    await graph.add_edge(
        src_id=src_id,
        dst_id=dst_id,
        rel_type=EDGE_TYPE_NEURAL_ASSOCIATION,
        weight=0.3,
    )

    mock_edge_repo.create_or_update_edge.assert_awaited_once()
    kwargs = mock_edge_repo.create_or_update_edge.await_args.kwargs
    assert kwargs["edge_type"] == EDGE_TYPE_NEURAL_ASSOCIATION


@pytest.mark.parametrize(
    "rel_type",
    [EDGE_TYPE_CONTINUES_FROM, EDGE_TYPE_REFERENCES_FILE],
)
@pytest.mark.asyncio
async def test_add_edge_accepts_producer_asserted_types(rel_type: str) -> None:
    """#782: ``add_edge`` accepts the two producer-asserted structural types.

    ``continues_from`` / ``references_file`` are emitted by the
    kagura-memory-ai-worker ingest pipeline (not the LLM judge). The validator
    must accept them and forward ``edge_type`` unchanged to
    ``NeuralEdgeRepository``. Origin pinning is the caller's responsibility on
    this path (see the sibling ``test_add_edge_propagates_explicit_origin``
    for the producer-asserted contract): when ``origin`` is omitted the
    repository column default ``hebbian`` applies — which is wrong for the
    producer-asserted relation types, hence #782's explicit ``origin=`` knob.
    """
    src_id = uuid4()
    dst_id = uuid4()

    mock_edge_repo = MagicMock()
    mock_edge_repo.create_or_update_edge = AsyncMock()

    graph = GraphService.__new__(GraphService)
    graph.user_id = "test_user_782"
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = MagicMock()
    graph.edge_repo = mock_edge_repo

    await graph.add_edge(
        src_id=src_id,
        dst_id=dst_id,
        rel_type=rel_type,
        weight=0.8,
    )

    mock_edge_repo.create_or_update_edge.assert_awaited_once()
    kwargs = mock_edge_repo.create_or_update_edge.await_args.kwargs
    assert kwargs["edge_type"] == rel_type
    # When `origin` is omitted, `add_edge` does NOT forward it — the
    # repository column default (`hebbian`) takes over. Pinning this here so
    # a future "set origin to hebbian by default" refactor (which would mask
    # the producer-asserted contract documented above) fails fast.
    assert "origin" not in kwargs


@pytest.mark.parametrize(
    "rel_type",
    [EDGE_TYPE_CONTINUES_FROM, EDGE_TYPE_REFERENCES_FILE],
)
@pytest.mark.asyncio
async def test_add_edge_propagates_explicit_origin(rel_type: str) -> None:
    """#782: when a non-MCP producer-asserted caller passes
    ``origin=EDGE_ORIGIN_DECLARED``, ``add_edge`` forwards it to the
    repository so the row is exempt from ``DecayManager``.

    Mirrors the contract pinned by ``mcp_server/tools/edge.py:handle_create_edge``
    (which sets ``origin=EDGE_ORIGIN_DECLARED`` for MCP create_edge callers)
    but exercises the in-process ``GraphService`` path used by any non-MCP
    producer (sleep service, future consolidation workers, integration tests).
    Without explicit propagation the producer-asserted rows would land with
    ``origin='hebbian'`` and silently decay over time.
    """
    src_id = uuid4()
    dst_id = uuid4()

    mock_edge_repo = MagicMock()
    mock_edge_repo.create_or_update_edge = AsyncMock()

    graph = GraphService.__new__(GraphService)
    graph.user_id = "test_user_782_origin"
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = MagicMock()
    graph.edge_repo = mock_edge_repo

    await graph.add_edge(
        src_id=src_id,
        dst_id=dst_id,
        rel_type=rel_type,
        weight=0.8,
        origin=EDGE_ORIGIN_DECLARED,
    )

    mock_edge_repo.create_or_update_edge.assert_awaited_once()
    kwargs = mock_edge_repo.create_or_update_edge.await_args.kwargs
    assert kwargs["edge_type"] == rel_type
    assert kwargs["origin"] == EDGE_ORIGIN_DECLARED


@pytest.mark.asyncio
async def test_add_edge_rejects_invalid_origin() -> None:
    """#782 Copilot review: invalid ``origin`` fails fast at the service boundary.

    Without this check, an unrecognized origin string would silently flow
    through ``GraphService.add_edge`` and reach the DB CHECK constraint
    ``valid_edge_origin``, surfacing as a deep IntegrityError. The fail-fast
    ValueError at the service boundary is more actionable for non-MCP callers
    (the MCP path pins origin to a constant and never hits this).
    """
    graph = GraphService.__new__(GraphService)
    graph.user_id = "test_user_782_origin_invalid"
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = MagicMock()
    graph.edge_repo = MagicMock()
    graph.edge_repo.create_or_update_edge = AsyncMock()

    with pytest.raises(ValueError, match="Invalid origin"):
        await graph.add_edge(
            src_id=uuid4(),
            dst_id=uuid4(),
            rel_type=EDGE_TYPE_CONTINUES_FROM,
            weight=0.8,
            origin="not_a_real_origin",
        )

    # Validator must reject BEFORE reaching the repo — proves "fail-fast"
    # is at the right layer (not merely earlier-than-DB).
    graph.edge_repo.create_or_update_edge.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_edge_rejects_unknown_rel_type() -> None:
    """Validator still rejects values outside ``EDGE_TYPES``.

    Negative-side complement to ``test_add_edge_accepts_neural_association``:
    confirms the validator (post-#741 = 4 values, post-#782 = 6 values)
    did not become a no-op. The deprecated ``semantic_similarity`` /
    ``declared_link`` / ``tag_cooccurrence`` strings are also still rejected,
    but this test uses a clearly invalid string so the assertion does not
    need to enumerate the historical drift surface.
    """
    src_id = uuid4()
    dst_id = uuid4()

    graph = GraphService.__new__(GraphService)
    graph.user_id = "test_user_506_neg"
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = MagicMock()
    graph.edge_repo = MagicMock()

    with pytest.raises(ValueError, match="Invalid rel_type"):
        await graph.add_edge(
            src_id=src_id,
            dst_id=dst_id,
            rel_type="not_a_real_edge_type",
            weight=1.0,
        )
