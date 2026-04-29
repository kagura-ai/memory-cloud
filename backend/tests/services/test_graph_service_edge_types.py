"""Tests for GraphService.EDGE_TYPES drift fix (#506).

PR #460 introduced ``EDGE_TYPE_*`` constants in ``models/memory.py`` to
mirror the DB CHECK constraint values. PR #507 (#461 Phase A) wired
``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` and the LLM-emittable
subset in ``services/sleep/edge_discovery.py`` to those constants.

This issue (#506) closes the last defining-site drift: ``GraphService.EDGE_TYPES``
was missing ``tag_cooccurrence`` (introduced in #223), causing
``add_edge(rel_type="tag_cooccurrence")`` to raise ``ValueError`` at the
``:189`` validator. The production tag_cooccurrence write path goes through
``NeuralEdgeRepository`` directly (memory_service), so this was a latent
bug — but any future caller using ``GraphService`` for tag_cooccurrence
edges would have hit it.

Tests pin both invariants:

1. ``GraphService.EDGE_TYPES`` equals the union of all ``EDGE_TYPE_*``
   constants (matching the DB CHECK constraint), so future drift between
   the constants block and this validator set fails CI before the
   inconsistency can ship.

2. ``add_edge(rel_type="tag_cooccurrence")`` no longer raises — the
   behavior change that motivated the atomic split from #461.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.memory import (
    EDGE_TYPE_DECLARED_LINK,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_SEMANTIC_SIMILARITY,
    EDGE_TYPE_TAG_COOCCURRENCE,
)
from services.graph_service import GraphService


def test_edge_types_matches_constants() -> None:
    """``GraphService.EDGE_TYPES`` is the union of all ``EDGE_TYPE_*``.

    Mirrors ``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` (PR #507) and
    the DB CHECK constraint in ``models/memory.py::valid_edge_type``.
    Adding a new edge_type must update both the constants block and this
    set in lock-step; this test enforces the lock.
    """
    assert GraphService.EDGE_TYPES == frozenset(
        {
            EDGE_TYPE_NEURAL_ASSOCIATION,
            EDGE_TYPE_RELATED_TO,
            EDGE_TYPE_DEPENDS_ON,
            EDGE_TYPE_LEARNED_FROM,
            EDGE_TYPE_SEMANTIC_SIMILARITY,
            EDGE_TYPE_DECLARED_LINK,
            EDGE_TYPE_TAG_COOCCURRENCE,
        }
    )


@pytest.mark.asyncio
async def test_add_edge_accepts_tag_cooccurrence() -> None:
    """``add_edge(rel_type="tag_cooccurrence")`` no longer raises ``ValueError``.

    Pre-#506 behavior: ``GraphService.EDGE_TYPES`` was missing
    ``tag_cooccurrence`` (drifted from the DB CHECK constraint), so the
    ``:189`` validator (``if rel_type not in self.EDGE_TYPES: raise``)
    rejected legitimate tag_cooccurrence writes. Post-#506: validator
    accepts and the call propagates to ``NeuralEdgeRepository``.
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

    # Pre-#506 this would raise ValueError at graph_service.py:189.
    await graph.add_edge(
        src_id=src_id,
        dst_id=dst_id,
        rel_type=EDGE_TYPE_TAG_COOCCURRENCE,
        weight=0.3,
    )

    mock_edge_repo.create_or_update_edge.assert_awaited_once()
    kwargs = mock_edge_repo.create_or_update_edge.await_args.kwargs
    assert kwargs["edge_type"] == EDGE_TYPE_TAG_COOCCURRENCE


@pytest.mark.asyncio
async def test_add_edge_rejects_unknown_rel_type() -> None:
    """Validator still rejects values outside ``EDGE_TYPES``.

    Negative-side complement to ``test_add_edge_accepts_tag_cooccurrence``:
    confirms #506 widened the accepted set without disabling the validator.
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
