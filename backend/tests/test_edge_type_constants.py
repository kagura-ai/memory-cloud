"""Regression tests for #461: cross-module edge_type constant invariants.

Pins:

1. ``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` equals the union of the
   ``EDGE_TYPE_*`` constants exported from ``models/memory.py`` — i.e.
   the runtime set used by MCP edge tools is exactly the set the DB CHECK
   constraint accepts (the latter is mirrored as the constants in #460).
   Post-#741 the set was four values; #782 appended ``continues_from`` and
   ``references_file`` (producer-asserted structural relation types).

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
    _ALL_EDGE_TYPES,
    EDGE_TYPE_CONTINUES_FROM,
    EDGE_TYPE_CONTRADICTS,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_REFERENCES_FILE,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_SUPERSEDES,
)
from services.graph_service import GraphService
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
            EDGE_TYPE_CONTINUES_FROM,
            EDGE_TYPE_REFERENCES_FILE,
            EDGE_TYPE_SUPERSEDES,
            EDGE_TYPE_CONTRADICTS,
        }
    )


def test_all_edge_type_validator_sets_match_all_edge_types() -> None:
    """All three validator sets equal ``frozenset(_ALL_EDGE_TYPES)`` (#782).

    The model's ``_ALL_EDGE_TYPES`` tuple (``models/memory.py``) is the
    canonical source of truth for the relation axis (it drives the DB CHECK
    constraint f-string). The two runtime guard sets —
    ``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` and
    ``services/graph_service.py::GraphService.EDGE_TYPES`` — must equal the
    same set, otherwise the MCP boundary and the in-process validator drift
    (e.g. one accepts a new type that the other rejects).

    The sibling ``test_valid_edge_types_matches_constants`` and
    ``test_edge_types_matches_constants`` in
    ``tests/services/test_graph_service_edge_types.py`` each pin one set
    against a hand-typed frozenset of named constants — useful as a NAME
    coverage check, but they cannot catch a drift WITHIN that hand-typed
    set (e.g. a refactor that updates one site but not the other). This
    chain-equality assertion pins all three sites to the same source so
    adding ``EDGE_TYPE_NEW`` requires only appending to ``_ALL_EDGE_TYPES``
    and the two frozensets — the test set in each sibling file no longer
    needs hand-curation.
    """
    expected = frozenset(_ALL_EDGE_TYPES)
    assert VALID_EDGE_TYPES == expected
    assert GraphService.EDGE_TYPES == expected


def test_llm_emittable_edge_types_subset_of_valid_edge_types() -> None:
    """``LLM_EMITTABLE_EDGE_TYPES`` ⊆ ``VALID_EDGE_TYPES``.

    Per #374 the LLM judge emits a deliberate subset of DB-accepted types.
    An LLM-only type would be rejected by the DB CHECK constraint; this
    test pins the cross-module subset invariant.
    """
    assert LLM_EMITTABLE_EDGE_TYPES.issubset(VALID_EDGE_TYPES)


def test_valid_edge_type_check_constraint_matches_migration_literal() -> None:
    """``valid_edge_type`` CHECK constraint text is byte-identical to b05_223.

    Issue #509 (Phase B of #461): the CHECK constraint is now derived from
    ``_ALL_EDGE_TYPES`` via f-string instead of being a hardcoded literal.
    This test pins the *exact* output string against the literal that
    ``b05_223_tag_cooccurrence.py`` installed on production via its
    ``_NEW_EDGE_TYPES_SQL`` constant. Two reasons:

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
        "'learned_from', 'continues_from', 'references_file', "
        "'supersedes', 'contradicts')"
    )

    valid_edge_type_check = next(
        c for c in NeuralMemoryEdge.__table_args__ if getattr(c, "name", None) == "valid_edge_type"
    )
    # Use ``.text`` (raw TextClause attribute) instead of ``str()`` which
    # would route through SQLAlchemy compilation and could shift across
    # SQLAlchemy/dialect versions. ``.text`` is the original CheckConstraint
    # string, so byte-identical comparison stays stable across upgrades.
    assert valid_edge_type_check.sqltext.text == expected
