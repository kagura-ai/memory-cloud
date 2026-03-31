"""Tests for neural memory models."""

from datetime import datetime

import pytest

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
