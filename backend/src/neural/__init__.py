"""Neural Memory Network implementation.

This module implements a Hebbian learning-based neural memory system
that enables adaptive association learning through user interactions.

Key Components:
- Hebbian Learning: "Cells that fire together, wire together"
- Activation Spreading: Graph-based associative retrieval
- Co-Activation Tracking: Automatic association discovery
- Unified Scoring: Semantic + Temporal + Graph + Trust signals
- Forgetting Mechanism: Decay and selective pruning

References:
- Hopfield Networks is All You Need (arXiv:2008.02217)
- kNN-LM (arXiv:1911.00172)
- RETRO (arXiv:2112.04426)
- Memorizing Transformers (arXiv:2203.08913)
"""

# ============================================================================
# BUG FIX #83-6: Remove NeuralMemoryEngine (broken/incompatible)
# ============================================================================
# Problem: NeuralMemoryEngine (neural/engine.py) had multiple critical issues:
#          1. Interface mismatch with MemoryService (different signatures)
#          2. Missing await on async calls
#          3. Wrong parameter passing to GraphService.add_node()
#          4. NotImplementedError in _get_embedding()
#
#          Result: Any usage would crash immediately.
#
# Root Cause: Dual implementation
#          - MemoryService.recall() has working Neural Memory integration
#          - NeuralMemoryEngine was experimental/old code never updated
#
# Solution: Delete neural/engine.py entirely. Neural Memory integration is
#           complete and working in MemoryService (services/memory_service.py).
#
# Impact: Removes broken code, eliminates confusion about which implementation
#         to use. Neural Memory works correctly via MemoryService.
# ============================================================================

from .activation import ActivationSpreader
from .co_activation import CoActivationTracker
from .config import NeuralMemoryConfig
from .decay import DecayManager

# Removed: from .engine import NeuralMemoryEngine  # Deleted - use MemoryService instead
from .hebbian import HebbianLearner
from .models import (
    ActivationState,
    CoActivationRecord,
    HebbianUpdate,
    MemoryKind,
    NeuralMemoryNode,
    RecallResult,
    SourceKind,
)
from .scoring import UnifiedScorer

__all__ = [
    # Main components
    "NeuralMemoryConfig",
    # Removed: "NeuralMemoryEngine",  # Use MemoryService.recall() instead
    # Subcomponents
    "ActivationSpreader",
    "CoActivationTracker",
    "DecayManager",
    "HebbianLearner",
    "UnifiedScorer",
    # Models
    "NeuralMemoryNode",
    "ActivationState",
    "CoActivationRecord",
    "HebbianUpdate",
    "RecallResult",
    "MemoryKind",
    "SourceKind",
]

__version__ = "0.1.0"
