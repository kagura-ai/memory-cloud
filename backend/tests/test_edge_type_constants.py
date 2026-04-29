"""Regression tests for #461: cross-module edge_type constant invariants.

Pins:

1. ``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` equals the union of the
   seven ``EDGE_TYPE_*`` constants exported from ``models/memory.py`` — i.e.
   the runtime set used by MCP edge tools is exactly the set the DB CHECK
   constraint accepts (the latter is mirrored as the constants in #460).

2. ``services/sleep/edge_discovery.py::LLM_EMITTABLE_EDGE_TYPES`` is a
   subset of ``VALID_EDGE_TYPES`` — the LLM judge can only emit a deliberate
   subset of the types the DB accepts (#374). A future PR widening the
   ``Literal`` without widening the full set (or vice versa) fails this
   test before the drift can ship.

These invariants cannot be expressed at the type-checker layer because
``Literal`` requires string literals (not module-level constants), so this
runtime test is the next safety net after pyright.
"""

from mcp_server.tools.edge import VALID_EDGE_TYPES
from models.memory import (
    EDGE_TYPE_DECLARED_LINK,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_SEMANTIC_SIMILARITY,
    EDGE_TYPE_TAG_COOCCURRENCE,
)
from services.sleep.edge_discovery import LLM_EMITTABLE_EDGE_TYPES


def test_valid_edge_types_matches_constants() -> None:
    """``tools/edge.py::VALID_EDGE_TYPES`` equals the union of all ``EDGE_TYPE_*``.

    Mirrors the DB CHECK constraint at ``models/memory.py::valid_edge_type``.
    Adding a new edge_type requires updating both the constants block and
    this set in lock-step; this test makes the lock enforceable.
    """
    assert VALID_EDGE_TYPES == frozenset(
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


def test_llm_emittable_edge_types_subset_of_valid_edge_types() -> None:
    """``LLM_EMITTABLE_EDGE_TYPES`` ⊆ ``VALID_EDGE_TYPES``.

    Per #374 the LLM judge emits a deliberate subset of DB-accepted types.
    An LLM-only type would be rejected by the DB CHECK constraint; this
    test pins the cross-module subset invariant.
    """
    assert LLM_EMITTABLE_EDGE_TYPES.issubset(VALID_EDGE_TYPES)


def test_valid_edge_type_check_constraint_matches_migration_literal() -> None:
    """``valid_edge_type`` CHECK constraint text is byte-identical to b03_396.

    Issue #509 (Phase B of #461): the CHECK constraint is now derived from
    ``_ALL_EDGE_TYPES`` via f-string instead of being a hardcoded literal.
    This test pins the *exact* output string against the literal that the
    b03_396 migration installed on production. Two reasons:

    1. ``Base.metadata.create_all()`` (used by tests + fresh dev DBs) must
       produce a CHECK constraint identical to the alembic head, so test
       fixtures and prod schema stay in sync.
    2. ``alembic revision --autogenerate`` compares the model's CheckConstraint
       string against the migration head; any textual divergence (sorted vs
       registration order, whitespace, quote style) generates a spurious
       no-op migration. This test catches such drift before it lands.

    If a future PR legitimately changes the CHECK string (adds a new
    edge_type, reorders, etc.), update this expected literal AND write the
    accompanying alembic migration in the same PR.
    """
    from models.memory import NeuralMemoryEdge

    expected = (
        "edge_type IN ('neural_association', 'related_to', 'depends_on', "
        "'learned_from', 'semantic_similarity', 'declared_link', "
        "'tag_cooccurrence')"
    )

    valid_edge_type_check = next(
        c for c in NeuralMemoryEdge.__table_args__ if getattr(c, "name", None) == "valid_edge_type"
    )
    assert str(valid_edge_type_check.sqltext) == expected
