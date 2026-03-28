"""Data models for Neural Memory Network.

This module defines the core data structures used in the neural memory system,
including memory nodes, activation states, and co-activation records.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from utils.datetime import utcnow


class MemoryKind(str, Enum):
    """Types of memory nodes."""

    FACT = "fact"
    PREFERENCE = "preference"
    TASK = "task"
    DIALOGUE = "dialogue"
    SUMMARY = "summary"
    TOOL_LOG = "tool-log"
    FILE_CHANGE = "file_change"
    ERROR = "error"
    DECISION = "decision"


class SourceKind(str, Enum):
    """Sources of memory information."""

    USER = "user"
    SYSTEM = "system"
    WEB = "web"
    FILE = "file"
    TOOL = "tool"
    LLM = "llm"


@dataclass
class NeuralMemoryNode:
    """A memory node in the neural network.

    Attributes:
        id: Unique identifier (UUID)
        user_id: Owner (for SISA-compliant sharding)
        kind: Type of memory (fact, preference, etc.)
        text: Original text content or summary
        embedding: d-dimensional vector (e.g., 1024-dim from E5)
        created_at: Timestamp of creation
        last_used_at: Timestamp of last retrieval/use
        use_count: Number of times this node has been activated
        importance: Importance score [0, 1] (LLM self-rating + EMA updates)
        confidence: Confidence/trust score [0, 1] (source + verification)
        source: Origin of this memory
        source_ref: Reference (URL, file path, message ID, etc.)
        long_term: Whether this node has been promoted to long-term memory
        quarantine: Whether this node is quarantined (poisoning defense)
        meta: Additional metadata (tags, entities, etc.)
    """

    id: str
    user_id: str
    kind: MemoryKind
    text: str
    embedding: list[float]
    created_at: datetime
    last_used_at: datetime | None = None
    use_count: int = 0
    importance: float = 0.5  # Default mid-range
    confidence: float = 1.0  # Default full confidence
    source: SourceKind = SourceKind.USER
    source_ref: str | None = None
    long_term: bool = False
    quarantine: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate field values."""
        if not (0.0 <= self.importance <= 1.0):
            raise ValueError(f"importance must be in [0, 1], got {self.importance}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        if not self.use_count >= 0:
            raise ValueError(f"use_count must be non-negative, got {self.use_count}")
        if not len(self.embedding) > 0:
            raise ValueError("embedding must not be empty")


@dataclass
class ActivationState:
    """Activation state during retrieval.

    Tracks which nodes are activated and their activation strengths
    during a single retrieval operation.

    Attributes:
        node_id: ID of the activated node
        activation: Activation strength [0, 1]
        hop: Distance from seed nodes (0=primary, 1=1-hop, etc.)
        source_node_id: ID of the node that activated this one (for spreading)
        timestamp: When this activation occurred
    """

    node_id: str
    activation: float
    hop: int = 0
    source_node_id: str | None = None
    timestamp: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        """Validate activation value."""
        if not (0.0 <= self.activation <= 1.0):
            raise ValueError(f"activation must be in [0, 1], got {self.activation}")
        if not self.hop >= 0:
            raise ValueError(f"hop must be non-negative, got {self.hop}")


@dataclass
class CoActivationRecord:
    """Record of two nodes being co-activated.

    Used for Hebbian learning: when two nodes are retrieved/used together,
    their association should be strengthened.

    Attributes:
        node_id_1: First node ID (ordered: id_1 < id_2)
        node_id_2: Second node ID
        count: Number of times these nodes were co-activated
        last_co_activation: Timestamp of most recent co-activation
        total_activation_product: Sum of (a_i * a_j) across all co-activations
        user_id: Owner (for sharding)
        first_seen: Timestamp of first co-activation (Issue #84 Phase 2C)
    """

    node_id_1: str
    node_id_2: str
    count: int = 1
    last_co_activation: datetime = field(default_factory=utcnow)
    total_activation_product: float = 0.0
    user_id: str = ""
    first_seen: datetime = field(default_factory=utcnow)  # Issue #84 Phase 2C

    def __post_init__(self) -> None:
        """Ensure node IDs are ordered."""
        if self.node_id_1 > self.node_id_2:
            # Swap to maintain ordering
            self.node_id_1, self.node_id_2 = self.node_id_2, self.node_id_1

    def update(self, activation_1: float, activation_2: float) -> None:
        """Update co-activation statistics.

        Args:
            activation_1: Activation strength of node 1
            activation_2: Activation strength of node 2
        """
        self.count += 1
        self.total_activation_product += activation_1 * activation_2
        self.last_co_activation = utcnow()  # Issue #84 Phase 2C

    @property
    def average_activation_product(self) -> float:
        """Get average activation product (for Hebbian weight calculation)."""
        return self.total_activation_product / self.count if self.count > 0 else 0.0


@dataclass
class RecallResult:
    """Result of a neural memory recall operation.

    Attributes:
        node: The retrieved memory node
        score: Composite score (0-1, higher is better)
        components: Breakdown of score components for debugging/analysis
    """

    node: NeuralMemoryNode
    score: float
    components: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate score."""
        if not self.score >= 0.0:
            raise ValueError(f"score must be non-negative, got {self.score}")


@dataclass
class HebbianUpdate:
    """A pending Hebbian weight update (for batch processing).

    Attributes:
        user_id: Owner (for sharding)
        src_id: Source node ID
        dst_id: Destination node ID
        delta_weight: Change in edge weight (can be negative)
        timestamp: When this update was queued
    """

    user_id: str
    src_id: str
    dst_id: str
    delta_weight: float
    timestamp: datetime = field(default_factory=utcnow)
