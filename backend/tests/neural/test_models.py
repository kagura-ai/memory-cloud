"""Tests for neural memory models."""

from datetime import datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from models.memory import (
    _ALL_EDGE_ORIGINS,
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    NeuralMemoryEdge,
)
from neural.models import (
    ActivationState,
    CoActivationRecord,
    HebbianUpdate,
    MemoryKind,
    NeuralMemoryNode,
    RecallResult,
    SourceKind,
)


class TestMemoryKind:
    """Test MemoryKind enum."""

    def test_all_kinds(self):
        """Test all memory kinds exist."""
        assert MemoryKind.FACT is not None
        assert MemoryKind.PREFERENCE is not None

    def test_kind_values(self):
        """Test kind enum values are strings."""
        for kind in MemoryKind:
            assert isinstance(kind.value, str)


class TestSourceKind:
    """Test SourceKind enum."""

    def test_user_source(self):
        """Test USER source kind."""
        assert SourceKind.USER is not None

    def test_all_sources(self):
        """Test all source kinds."""
        for source in SourceKind:
            assert isinstance(source.value, str)


class TestActivationState:
    """Test ActivationState dataclass."""

    def test_basic_creation(self):
        """Test basic ActivationState creation."""
        state = ActivationState(node_id="n1", activation=0.8)
        assert state.node_id == "n1"
        assert state.activation == 0.8
        assert state.hop == 0
        assert state.source_node_id is None

    def test_with_hop_and_source(self):
        """Test ActivationState with hop and source."""
        state = ActivationState(node_id="n2", activation=0.5, hop=1, source_node_id="n1")
        assert state.hop == 1
        assert state.source_node_id == "n1"


class TestNeuralMemoryNode:
    """Test NeuralMemoryNode dataclass."""

    def test_basic_creation(self):
        """Test basic node creation."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="user1",
            kind=MemoryKind.FACT,
            text="Test memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow(),
        )
        assert node.id == "node1"
        assert node.importance == 0.5  # default
        assert node.confidence == 1.0  # default
        assert node.use_count == 0
        assert node.long_term is False

    def test_all_fields(self):
        """Test node with all fields."""
        now = datetime.utcnow()
        node = NeuralMemoryNode(
            id="node1",
            user_id="user1",
            kind=MemoryKind.FACT,
            text="Test",
            embedding=[0.1] * 3,
            created_at=now,
            last_used_at=now,
            use_count=10,
            importance=0.9,
            confidence=0.8,
            source=SourceKind.USER,
            long_term=True,
        )
        assert node.use_count == 10
        assert node.long_term is True


class TestCoActivationRecord:
    """Test CoActivationRecord dataclass."""

    def test_basic_creation(self):
        """Test basic record creation."""
        record = CoActivationRecord(
            node_id_1="n1",
            node_id_2="n2",
            count=1,
            total_activation_product=0.8,
            user_id="user1",
        )
        assert record.count == 1
        assert record.user_id == "user1"

    def test_average_activation_product(self):
        """Test average calculation."""
        record = CoActivationRecord(
            node_id_1="n1",
            node_id_2="n2",
            count=4,
            total_activation_product=3.2,
            user_id="user1",
        )
        assert record.average_activation_product == pytest.approx(0.8)

    def test_update(self):
        """Test record update."""
        record = CoActivationRecord(
            node_id_1="n1",
            node_id_2="n2",
            count=1,
            total_activation_product=1.0,
            user_id="user1",
        )
        record.update(0.5, 0.6)
        assert record.count == 2
        assert record.total_activation_product == pytest.approx(1.0 + 0.5 * 0.6)


class TestHebbianUpdate:
    """Test HebbianUpdate dataclass."""

    def test_creation(self):
        """Test HebbianUpdate creation."""
        update = HebbianUpdate(
            user_id="user1",
            src_id="n1",
            dst_id="n2",
            delta_weight=0.05,
        )
        assert update.delta_weight == 0.05


class TestRecallResult:
    """Test RecallResult dataclass."""

    def test_creation(self):
        """Test RecallResult creation."""
        node = NeuralMemoryNode(
            id="n1",
            user_id="u1",
            kind=MemoryKind.FACT,
            text="Test",
            embedding=[0.1],
            created_at=datetime.utcnow(),
        )
        result = RecallResult(node=node, score=0.85)
        assert result.score == 0.85
        assert result.components == {}

    def test_with_components(self):
        """Test RecallResult with score components."""
        node = NeuralMemoryNode(
            id="n1",
            user_id="u1",
            kind=MemoryKind.FACT,
            text="Test",
            embedding=[0.1],
            created_at=datetime.utcnow(),
        )
        result = RecallResult(
            node=node,
            score=0.85,
            components={"semantic": 0.9, "recency": 0.7},
        )
        assert result.components["semantic"] == 0.9


# ---------------------------------------------------------------------------
# NeuralMemoryEdge.origin column tests (Issue #722)
# ---------------------------------------------------------------------------


def test_origin_column_present_with_default_hebbian():
    cols = {c.name: c for c in inspect(NeuralMemoryEdge).columns}
    assert "origin" in cols
    assert cols["origin"].nullable is False
    assert cols["origin"].default.arg == EDGE_ORIGIN_HEBBIAN


def test_all_edge_origins_constants():
    assert _ALL_EDGE_ORIGINS == (
        EDGE_ORIGIN_HEBBIAN,
        EDGE_ORIGIN_SEMANTIC,
        EDGE_ORIGIN_DECLARED,
    )


@pytest.mark.asyncio
async def test_invalid_origin_rejected_by_check_constraint(db_session, sample_memory_pair):
    src, dst = sample_memory_pair
    bad = NeuralMemoryEdge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        workspace_id=src.workspace_id,
        context_id=src.context_id,
        edge_type="neural_association",
        weight=0.1,
        confidence=1.0,
        origin="bogus",
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def test_valid_edge_origin_check_constraint_matches_migration_literal():
    """``valid_edge_origin`` CHECK constraint text is byte-identical to e17_722.

    Mirrors ``test_valid_edge_type_check_constraint_matches_migration_literal``
    in ``tests/test_edge_type_constants.py`` for the new origin discriminator.

    The CHECK constraint is derived from ``_ALL_EDGE_ORIGINS`` via f-string.
    This test pins the *exact* output string against the literal that
    ``e17_722_neural_edge_origin.py`` installs on production via its
    ``op.execute(...)`` block. Two reasons:

    1. ``Base.metadata.create_all()`` (used by tests + fresh dev DBs) must
       produce a CHECK constraint identical to the alembic head, so test
       fixtures and prod schema stay in sync.
    2. ``alembic revision --autogenerate`` compares the model's CheckConstraint
       string against the migration head; any textual divergence (sorted vs
       registration order, whitespace, quote style) generates a spurious
       no-op migration. This test catches such drift before it lands.

    If a future PR legitimately changes the CHECK string (adds a new origin,
    reorders, etc.), update this expected literal AND write the accompanying
    alembic migration in the same PR.
    """
    expected = "origin IN ('hebbian', 'semantic', 'declared')"

    valid_edge_origin_check = next(
        c
        for c in NeuralMemoryEdge.__table_args__
        if getattr(c, "name", None) == "valid_edge_origin"
    )
    # Use ``.text`` (raw TextClause attribute) instead of ``str()`` which
    # would route through SQLAlchemy compilation and could shift across
    # SQLAlchemy/dialect versions. ``.text`` is the original CheckConstraint
    # string, so byte-identical comparison stays stable across upgrades.
    assert valid_edge_origin_check.sqltext.text == expected
