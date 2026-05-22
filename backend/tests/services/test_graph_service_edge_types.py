"""Tests for GraphService.EDGE_TYPES drift fix (#506, updated for #741).

PR #460 introduced ``EDGE_TYPE_*`` constants in ``models/memory.py`` to
mirror the DB CHECK constraint values. PR #507 (#461 Phase A) wired
``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` and the LLM-emittable
subset in ``services/sleep/edge_discovery.py`` to those constants.

#506 closed the last defining-site drift by adding ``tag_cooccurrence`` to
``GraphService.EDGE_TYPES``. #741 then collapsed the edge_type axis to four
values (``neural_association`` / ``related_to`` / ``depends_on`` /
``learned_from``) by moving provenance to the ``origin`` axis and tag
seeding to ``edge_metadata['source']``. The remaining set drift tests pin
the post-#741 shape.

Tests pin both invariants:

1. ``GraphService.EDGE_TYPES`` equals the union of the four surviving
   ``EDGE_TYPE_*`` constants (matching the DB CHECK constraint), so future
   drift between the constants block and this validator set fails CI before
   the inconsistency can ship.

2. ``add_edge(rel_type="neural_association")`` succeeds — post-#741 every
   non-relational write (formerly ``semantic_similarity`` / ``declared_link``
   / ``tag_cooccurrence``) goes through ``neural_association`` and discriminates
   via ``origin`` + ``edge_metadata``.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.memory import (
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_NEURAL_ASSOCIATION,
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


@pytest.mark.asyncio
async def test_add_edge_rejects_unknown_rel_type() -> None:
    """Validator still rejects values outside ``EDGE_TYPES``.

    Negative-side complement to ``test_add_edge_accepts_neural_association``:
    confirms the validator narrowed to four values without becoming a no-op.
    Post-#741 the deprecated ``semantic_similarity`` / ``declared_link`` /
    ``tag_cooccurrence`` strings are also no longer in the set, but this
    test uses a clearly invalid string so the assertion does not need to
    enumerate them.
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
