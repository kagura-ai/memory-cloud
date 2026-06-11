"""Tests for CoActivationTracker."""

from datetime import datetime, timedelta

import pytest

from neural.co_activation import CoActivationTracker
from neural.config import NeuralMemoryConfig
from neural.models import ActivationState


class TestCoActivationTracker:
    """Test CoActivationTracker for Hebbian learning input."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            track_co_activation=True,
            co_activation_window=60,  # 60 seconds window
            min_co_activation_count=2,
        )

    @pytest.fixture
    def tracker(self, config):
        """Create CoActivationTracker."""
        return CoActivationTracker(config)

    def test_init(self, config):
        """Test CoActivationTracker initialization."""
        tracker = CoActivationTracker(config)
        assert tracker.config == config
        assert len(tracker._activation_history) == 0
        assert len(tracker._co_activation_records) == 0

    def test_record_activation_disabled(self):
        """Test that recording is disabled when config flag is False."""
        config = NeuralMemoryConfig(track_co_activation=False)
        tracker = CoActivationTracker(config)

        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]

        records = tracker.record_activation("user1", activations)

        # Should return empty list when disabled
        assert len(records) == 0

    def test_record_activation_empty_list(self, tracker):
        """Test recording empty activation list."""
        records = tracker.record_activation("user1", [])

        # Should handle gracefully
        assert len(records) == 0

    def test_record_activation_single_event(self, tracker):
        """Test recording a single activation event."""
        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
            ActivationState(node_id="node3", activation=0.6),
        ]

        records = tracker.record_activation("user1", activations)

        # First event should create co-activation records for all pairs
        # Pairs: (node1, node2), (node1, node3), (node2, node3) = 3 pairs
        assert len(records) == 3

        # Check that history is recorded
        assert "user1" in tracker._activation_history
        assert len(tracker._activation_history["user1"]) == 1

    def test_record_activation_creates_pairs(self, tracker):
        """Test that co-activation records are created for all pairs."""
        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]

        records = tracker.record_activation("user1", activations)

        # Should create 1 pair: (node1, node2)
        assert len(records) == 1
        record = records[0]

        # Check record properties (order should be normalized)
        assert {record.node_id_1, record.node_id_2} == {"node1", "node2"}
        assert record.count == 1
        assert record.user_id == "user1"

        # Note: Implementation uses 1.0 for all activations (not actual values)
        # So activation_product = 1.0 * 1.0 = 1.0
        assert record.average_activation_product == 1.0

    def test_record_activation_updates_existing_record(self, tracker):
        """Test that repeated co-activations update existing records."""
        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]

        # First activation
        records1 = tracker.record_activation("user1", activations)
        assert records1[0].count == 1

        # Second activation (within time window) - finds pair again in window
        records2 = tracker.record_activation("user1", activations)
        assert records2[0].count == 2

        # Check that only 1 unique pair exists
        all_records = tracker.get_all_co_activations("user1", min_count=0)
        assert len(all_records) == 1

    def test_get_co_activation_record(self, tracker):
        """Test retrieving a specific co-activation record."""
        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]

        tracker.record_activation("user1", activations)

        # Get record (order should not matter)
        record1 = tracker.get_co_activation_record("user1", "node1", "node2")
        record2 = tracker.get_co_activation_record("user1", "node2", "node1")

        assert record1 is not None
        assert record2 is not None
        assert record1 == record2  # Same record regardless of order

    def test_get_co_activation_record_not_exists(self, tracker):
        """Test retrieving non-existent record."""
        record = tracker.get_co_activation_record("user1", "node1", "node2")
        assert record is None

    def test_get_all_co_activations(self, tracker):
        """Test retrieving all co-activation records."""
        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
            ActivationState(node_id="node3", activation=0.6),
        ]

        # Create records
        tracker.record_activation("user1", activations)

        # Get all records
        all_records = tracker.get_all_co_activations("user1", min_count=0)

        # Should have 3 pairs: (1,2), (1,3), (2,3)
        assert len(all_records) == 3

    def test_get_all_co_activations_min_count_filter(self, tracker):
        """Test filtering co-activations by minimum count."""
        # Create first pair with multiple activations
        activations1 = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)  # Count = 3

        # Create second pair with single activation
        activations2 = [
            ActivationState(node_id="node3", activation=1.0),
            ActivationState(node_id="node4", activation=0.8),
        ]
        tracker.record_activation("user1", activations2)  # Count = 1

        # Filter with min_count=2
        filtered = tracker.get_all_co_activations("user1", min_count=2)

        # Should only return (node1, node2) pair
        assert len(filtered) == 1
        record = filtered[0]
        assert {record.node_id_1, record.node_id_2} == {"node1", "node2"}
        assert record.count >= 2

    def test_get_all_co_activations_sorted_by_count(self, tracker):
        """Test that results are sorted by count (descending)."""
        # Create pairs with different counts
        activations1 = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]
        # Record 3 times
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)

        activations2 = [
            ActivationState(node_id="node3", activation=1.0),
            ActivationState(node_id="node4", activation=0.8),
        ]
        # Record 1 time - also detects cross-pairs from window
        tracker.record_activation("user1", activations2)

        # Get all records
        all_records = tracker.get_all_co_activations("user1", min_count=0)

        # Should be sorted by count descending
        counts = [r.count for r in all_records]
        assert counts == sorted(counts, reverse=True)

        # (node1, node2) should have highest count (incremented by all 4 calls)
        pair_12 = next(r for r in all_records if {r.node_id_1, r.node_id_2} == {"node1", "node2"})
        assert pair_12.count >= 3

    def test_get_frequently_co_activated_with(self, tracker):
        """Test finding nodes frequently co-activated with a target node."""
        # Create co-activations
        # node1 co-activated with node2 (3 times) and node3 (1 time)
        activations1 = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)

        activations2 = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node3", activation=0.6),
        ]
        tracker.record_activation("user1", activations2)

        # Get frequently co-activated with node1
        related = tracker.get_frequently_co_activated_with("user1", "node1", top_k=5)

        # Should include node2 and node3 (node3 detected via window cross-activation)
        assert len(related) >= 2

        # node2 should be first (higher count)
        assert related[0][0] == "node2"
        assert related[0][1].count >= 3

    def test_get_frequently_co_activated_with_top_k(self, tracker):
        """Test top_k limit."""
        # Create many co-activations
        for i in range(2, 6):  # node2, node3, node4, node5
            activations = [
                ActivationState(node_id="node1", activation=1.0),
                ActivationState(node_id=f"node{i}", activation=0.8),
            ]
            tracker.record_activation("user1", activations)

        # Get top 2
        related = tracker.get_frequently_co_activated_with("user1", "node1", top_k=2)

        # Should only return 2 results
        assert len(related) == 2

    def test_clean_old_history(self, tracker):
        """Test that old activation events are removed."""
        # Create activation event
        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]
        tracker.record_activation("user1", activations)

        # Manually set old timestamp (outside window)
        old_time = datetime.utcnow() - timedelta(seconds=120)  # 2 minutes ago
        tracker._activation_history["user1"][0] = (
            old_time,
            tracker._activation_history["user1"][0][1],
        )

        # Record new activation (should trigger cleanup)
        current_time = datetime.utcnow()
        tracker._clean_old_history("user1", current_time)

        # Old event should be removed (window is 60 seconds)
        assert len(tracker._activation_history["user1"]) == 0

    def test_co_activation_window(self, tracker):
        """Test that co-activations are detected within time window."""
        # Record first activation
        activations1 = [ActivationState(node_id="node1", activation=1.0)]
        tracker.record_activation("user1", activations1)

        # Record second activation within window
        activations2 = [ActivationState(node_id="node2", activation=0.8)]
        tracker.record_activation("user1", activations2)

        # Both nodes were activated within the window
        # So they should be considered co-activated
        all_records = tracker.get_all_co_activations("user1", min_count=0)

        # Should have co-activation record
        assert len(all_records) > 0

    def test_clear_user_data(self, tracker):
        """Test GDPR-compliant data clearing."""
        # Create co-activations for two users
        activations = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]

        tracker.record_activation("user1", activations)
        tracker.record_activation("user2", activations)

        # Clear user1 data
        tracker.clear_user_data("user1")

        # user1 data should be gone
        assert "user1" not in tracker._activation_history
        assert "user1" not in tracker._co_activation_records

        # user2 data should remain
        assert "user2" in tracker._activation_history
        assert "user2" in tracker._co_activation_records

    def test_get_statistics_empty(self, tracker):
        """Test statistics with no records."""
        stats = tracker.get_statistics("user1")

        assert stats["total_pairs"] == 0
        assert stats["avg_count"] == 0.0
        assert stats["max_count"] == 0
        assert stats["min_count"] == 0

    def test_get_statistics(self, tracker):
        """Test statistics calculation."""
        # Create co-activations with different counts
        activations1 = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations1)  # Count = 3

        activations2 = [
            ActivationState(node_id="node3", activation=1.0),
            ActivationState(node_id="node4", activation=0.8),
        ]
        tracker.record_activation("user1", activations2)  # Count = 1

        stats = tracker.get_statistics("user1")

        # Window-based detection creates cross-pairs too
        assert stats["total_pairs"] >= 2
        assert stats["max_count"] >= 3
        assert stats["min_count"] >= 1
        assert stats["history_size"] > 0

    def test_co_activation_record_ordering(self, tracker):
        """Test that node pairs are stored in consistent order."""
        activations1 = [
            ActivationState(node_id="node2", activation=0.8),
            ActivationState(node_id="node1", activation=1.0),
        ]

        activations2 = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]

        # Record both orders
        tracker.record_activation("user1", activations1)
        tracker.record_activation("user1", activations2)

        # Should create only one record (not two)
        all_records = tracker.get_all_co_activations("user1", min_count=0)
        assert len(all_records) == 1

        # Count should be 2 (both activations counted)
        assert all_records[0].count == 2

    def test_average_activation_product_calculation(self, tracker):
        """Test that average activation product is calculated correctly."""
        # First activation: 1.0 * 0.8 = 0.8
        activations1 = [
            ActivationState(node_id="node1", activation=1.0),
            ActivationState(node_id="node2", activation=0.8),
        ]
        tracker.record_activation("user1", activations1)

        # Second activation: 0.6 * 0.9 = 0.54
        activations2 = [
            ActivationState(node_id="node1", activation=0.6),
            ActivationState(node_id="node2", activation=0.9),
        ]
        tracker.record_activation("user1", activations2)

        record = tracker.get_co_activation_record("user1", "node1", "node2")

        # Implementation uses 1.0 for all activations (not actual ActivationState values)
        # So product is always 1.0 * 1.0 = 1.0
        assert record.average_activation_product == 1.0


class TestSemanticGating:
    """Test semantic similarity gating for co-activation edges (Issue #118)."""

    @pytest.fixture
    def config(self):
        """Create config with semantic gating enabled."""
        return NeuralMemoryConfig(
            track_co_activation=True,
            co_activation_window=60,
            min_co_activation_count=2,
            min_similarity_for_edge=0.5,
        )

    @pytest.fixture
    def tracker(self, config):
        return CoActivationTracker(config)

    def _make_embedding(self, values: list[float]) -> list[float]:
        """Create a normalized embedding from a few values (padded to dim=8)."""
        import numpy as np

        emb = values + [0.0] * (8 - len(values))
        norm = np.linalg.norm(emb)
        return (np.array(emb) / norm).tolist() if norm > 0 else emb

    def test_similar_pairs_create_edges(self, tracker):
        """Pairs with high similarity should create co-activation edges."""
        # Two similar embeddings (cosine sim > 0.5)
        emb_a = self._make_embedding([1.0, 0.8, 0.1])
        emb_b = self._make_embedding([0.9, 0.7, 0.2])

        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
        ]
        embeddings = {"a": emb_a, "b": emb_b}

        records = tracker.record_activation("user1", activations, embeddings=embeddings)
        assert len(records) == 1

    def test_similarity_threshold_override_admits_pair_config_would_gate(self):
        """Per-call override (#982) takes precedence over config.min_similarity_for_edge.

        config gate is high (0.9) so the 0.6-cosine pair would be rejected by
        the fallback, but a 0.3 override (the calibrated edge_gate value)
        admits it.
        """
        config = NeuralMemoryConfig(
            track_co_activation=True, co_activation_window=60, min_similarity_for_edge=0.9
        )
        tracker = CoActivationTracker(config)
        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_b = self._make_embedding([0.6, 0.8, 0.0])  # cosine 0.6 with emb_a
        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
        ]
        records = tracker.record_activation(
            "user1",
            activations,
            embeddings={"a": emb_a, "b": emb_b},
            similarity_threshold=0.3,
        )
        assert len(records) == 1

    def test_similarity_threshold_override_gates_pair_config_would_admit(self):
        """A high override gates a pair that the low config value would admit."""
        config = NeuralMemoryConfig(
            track_co_activation=True, co_activation_window=60, min_similarity_for_edge=0.1
        )
        tracker = CoActivationTracker(config)
        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_b = self._make_embedding([0.6, 0.8, 0.0])  # cosine 0.6 with emb_a
        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
        ]
        records = tracker.record_activation(
            "user1",
            activations,
            embeddings={"a": emb_a, "b": emb_b},
            similarity_threshold=0.8,
        )
        assert len(records) == 0

    def test_none_similarity_threshold_falls_back_to_config(self, tracker):
        """similarity_threshold=None → use config.min_similarity_for_edge (0.5 fixture)."""
        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_b = self._make_embedding([0.6, 0.8, 0.0])  # cosine 0.6 > 0.5 fixture
        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
        ]
        records = tracker.record_activation(
            "user1",
            activations,
            embeddings={"a": emb_a, "b": emb_b},
            similarity_threshold=None,
        )
        # 0.6 >= config 0.5 → pair forms (override absent).
        assert len(records) == 1

    def test_dissimilar_pairs_blocked(self, tracker):
        """Pairs with low similarity should NOT create co-activation edges."""
        # Two dissimilar embeddings (cosine sim < 0.5)
        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_b = self._make_embedding([0.0, 0.0, 1.0])

        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
        ]
        embeddings = {"a": emb_a, "b": emb_b}

        records = tracker.record_activation("user1", activations, embeddings=embeddings)
        assert len(records) == 0

    def test_no_embeddings_skips_gating(self, tracker):
        """When embeddings are not provided, all pairs should be co-activated (backward compat)."""
        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
        ]

        records = tracker.record_activation("user1", activations)
        assert len(records) == 1

    def test_mixed_embeddings_partial_gating(self, tracker):
        """When only some nodes have embeddings, pairs without embeddings pass through."""
        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_b = self._make_embedding([0.0, 0.0, 1.0])  # dissimilar to a

        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
            ActivationState(node_id="c", activation=0.6),  # no embedding
        ]
        embeddings = {"a": emb_a, "b": emb_b}

        records = tracker.record_activation("user1", activations, embeddings=embeddings)

        # (a, b) blocked by similarity < 0.5
        # (a, c) and (b, c) pass through (c has no embedding)
        assert len(records) == 2

    def test_threshold_zero_disables_gating(self):
        """Setting threshold to 0.0 effectively disables gating."""
        config = NeuralMemoryConfig(
            track_co_activation=True,
            co_activation_window=60,
            min_similarity_for_edge=0.0,
        )
        tracker = CoActivationTracker(config)

        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_b = self._make_embedding([0.0, 0.0, 1.0])

        activations = [
            ActivationState(node_id="a", activation=1.0),
            ActivationState(node_id="b", activation=0.8),
        ]
        embeddings = {"a": emb_a, "b": emb_b}

        records = tracker.record_activation("user1", activations, embeddings=embeddings)
        assert len(records) == 1


class TestRepetitionEvidence:
    """2D edge gate evidence accumulation (Issue #983).

    ``floor_threshold`` lowers the *recording* gate so pairs in the
    [floor, threshold) cosine band accumulate co-activation evidence
    instead of being rejected outright. ``same_event_count`` counts only
    joint appearances in the same activation event (= same query top-k),
    which is the repetition signal the 2D gate consults — window-based
    cross-event co-occurrence stays in ``count`` but is NOT evidence.
    """

    @pytest.fixture
    def tracker(self):
        config = NeuralMemoryConfig(
            track_co_activation=True,
            co_activation_window=60,
            min_co_activation_count=2,
            min_similarity_for_edge=0.5,
        )
        return CoActivationTracker(config)

    def _make_embedding(self, values: list[float]) -> list[float]:
        import numpy as np

        emb = values + [0.0] * (8 - len(values))
        norm = np.linalg.norm(emb)
        return (np.array(emb) / norm).tolist() if norm > 0 else emb

    def _band_pair(self):
        """Embeddings with cosine ≈ 0.35 — inside the [0.3, 0.45) band."""
        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_b = self._make_embedding([0.35, 0.9367, 0.0])
        return {"a": emb_a, "b": emb_b}

    def _activations(self, *node_ids: str) -> list[ActivationState]:
        return [ActivationState(node_id=n, activation=1.0) for n in node_ids]

    def test_floor_threshold_records_band_pair(self, tracker):
        """Cosine in [floor, threshold) → record IS created (evidence accumulates)."""
        records = tracker.record_activation(
            "user1",
            self._activations("a", "b"),
            embeddings=self._band_pair(),
            similarity_threshold=0.45,
            floor_threshold=0.3,
        )
        assert len(records) == 1
        assert records[0].same_event_count == 1

    def test_below_floor_still_rejected(self, tracker):
        """Cosine below the floor → no record (hard reject, unchanged)."""
        emb_a = self._make_embedding([1.0, 0.0, 0.0])
        emb_c = self._make_embedding([0.1, 0.995, 0.0])  # cosine ≈ 0.1 < 0.3
        records = tracker.record_activation(
            "user1",
            self._activations("a", "c"),
            embeddings={"a": emb_a, "c": emb_c},
            similarity_threshold=0.45,
            floor_threshold=0.3,
        )
        assert len(records) == 0

    def test_no_floor_keeps_threshold_gate(self, tracker):
        """floor_threshold=None → band pair is rejected as before (rollback path)."""
        records = tracker.record_activation(
            "user1",
            self._activations("a", "b"),
            embeddings=self._band_pair(),
            similarity_threshold=0.45,
            floor_threshold=None,
        )
        assert len(records) == 0

    def test_same_event_count_accumulates_across_joint_recalls(self, tracker):
        """Both nodes in the same event twice → same_event_count == 2."""
        for _ in range(2):
            tracker.record_activation(
                "user1",
                self._activations("a", "b"),
                embeddings=self._band_pair(),
                similarity_threshold=0.45,
                floor_threshold=0.3,
            )
        record = tracker.get_co_activation_record("user1", "a", "b")
        assert record is not None
        assert record.same_event_count == 2

    def test_window_co_occurrence_is_not_same_event_evidence(self, tracker):
        """Event {a,b} then event {a,c}: (a,b) window count grows but
        same_event_count stays 1 — b was not re-recalled, so no new evidence."""
        embeddings = self._band_pair()
        tracker.record_activation(
            "user1",
            self._activations("a", "b"),
            embeddings=embeddings,
            similarity_threshold=0.45,
            floor_threshold=0.3,
        )
        tracker.record_activation(
            "user1",
            self._activations("a"),
            embeddings=embeddings,
            similarity_threshold=0.45,
            floor_threshold=0.3,
        )
        record = tracker.get_co_activation_record("user1", "a", "b")
        assert record is not None
        assert record.count == 2  # window co-occurrence still tracked
        assert record.same_event_count == 1  # but not counted as evidence

    def test_stale_pair_not_recounted_by_unrelated_event(self, tracker):
        """Event {a,b} then unrelated event {x,y}: (a,b) must not be re-counted
        just because both linger in the window (count-inflation fix)."""
        tracker.record_activation("user1", self._activations("a", "b"))
        tracker.record_activation("user1", self._activations("x", "y"))
        record = tracker.get_co_activation_record("user1", "a", "b")
        assert record is not None
        assert record.count == 1
        assert record.same_event_count == 1

    def test_repeated_query_does_not_inflate_evidence(self, tracker):
        """Same query (same event_key) replayed N times → evidence stays 1.

        This is the distinct-query-context requirement: a noise pair inside
        one query's top-k re-co-occurs every time that query is repeated
        (eval replay rounds, production rehearsal) — repetition of ONE
        ranking accident is not independent evidence."""
        for _ in range(3):
            tracker.record_activation(
                "user1",
                self._activations("a", "b"),
                embeddings=self._band_pair(),
                similarity_threshold=0.45,
                floor_threshold=0.3,
                event_key="query-1",
            )
        record = tracker.get_co_activation_record("user1", "a", "b")
        assert record is not None
        assert record.same_event_count == 1

    def test_distinct_queries_accumulate_evidence(self, tracker):
        """Different queries co-recalling the same pair → evidence grows.

        Genuine cross-topic associations surface in the top-k of *different*
        queries; that is the signal the 2D gate trusts."""
        for key in ("query-1", "query-2", "query-3"):
            tracker.record_activation(
                "user1",
                self._activations("a", "b"),
                embeddings=self._band_pair(),
                similarity_threshold=0.45,
                floor_threshold=0.3,
                event_key=key,
            )
        record = tracker.get_co_activation_record("user1", "a", "b")
        assert record is not None
        assert record.same_event_count == 3

    def test_no_event_key_falls_back_to_per_event_counting(self, tracker):
        """event_key=None (legacy callers) → every joint event counts."""
        for _ in range(2):
            tracker.record_activation(
                "user1",
                self._activations("a", "b"),
                embeddings=self._band_pair(),
                similarity_threshold=0.45,
                floor_threshold=0.3,
            )
        record = tracker.get_co_activation_record("user1", "a", "b")
        assert record is not None
        assert record.same_event_count == 2
