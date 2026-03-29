"""Regression test for unique_edge constraint on neural_memory_edges.

Issue #4: Missing unique_edge constraint caused InFailedSQLTransactionError.
The ON CONFLICT ON CONSTRAINT unique_edge clause requires this constraint to exist.
"""

from sqlalchemy import UniqueConstraint

from models.memory import NeuralMemoryEdge


class TestNeuralMemoryEdgeConstraints:
    """Verify NeuralMemoryEdge model has required constraints."""

    def test_unique_edge_constraint_exists(self):
        """unique_edge constraint must exist for ON CONFLICT upsert."""
        constraints = NeuralMemoryEdge.__table_args__
        unique_constraints = [
            c for c in constraints if isinstance(c, UniqueConstraint) and c.name == "unique_edge"
        ]
        assert len(unique_constraints) == 1, "Missing UniqueConstraint named 'unique_edge'"

    def test_unique_edge_columns(self):
        """unique_edge must cover (user_id, src_id, dst_id)."""
        constraints = NeuralMemoryEdge.__table_args__
        unique = next(
            c for c in constraints if isinstance(c, UniqueConstraint) and c.name == "unique_edge"
        )
        col_names = [col.name for col in unique.columns]
        assert col_names == ["user_id", "src_id", "dst_id"]
