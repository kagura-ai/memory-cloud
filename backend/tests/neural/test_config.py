"""Tests for NeuralMemoryConfig."""

import pytest

from neural.config import NeuralMemoryConfig


class TestNeuralMemoryConfig:
    """Test NeuralMemoryConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = NeuralMemoryConfig()
        assert config.spread_hops == 2
        assert config.spread_decay == 0.8
        assert config.spread_threshold == 0.1
        assert config.learning_rate == 0.1
        assert config.enable_decay is True
        assert config.weight_max == 3.0

    def test_custom_values(self):
        """Test custom configuration."""
        config = NeuralMemoryConfig(
            spread_hops=3,
            spread_decay=0.5,
            learning_rate=0.05,
        )
        assert config.spread_hops == 3
        assert config.spread_decay == 0.5
        assert config.learning_rate == 0.05

    def test_scoring_weights_normalized(self):
        """Test weight normalization."""
        config = NeuralMemoryConfig(
            alpha=0.4, beta=0.2, gamma=0.15, delta=0.15, epsilon=0.1, zeta=0.1
        )
        weights = config.scoring_weights_normalized
        # alpha through epsilon sum to 1.0 (zeta is separate penalty)
        scoring_sum = sum(weights[k] for k in ["alpha", "beta", "gamma", "delta", "epsilon"])
        assert abs(scoring_sum - 1.0) < 0.001

    def test_scoring_weights_proportional(self):
        """Test that normalized weights maintain relative proportions."""
        config = NeuralMemoryConfig(alpha=0.8, beta=0.2, gamma=0.0, delta=0.0, epsilon=0.0)
        weights = config.scoring_weights_normalized
        assert weights["alpha"] == pytest.approx(0.8)
        assert weights["beta"] == pytest.approx(0.2)

    def test_from_env_defaults(self):
        """Test loading config from environment with defaults."""
        config = NeuralMemoryConfig.from_env()
        assert isinstance(config, NeuralMemoryConfig)
        assert config.spread_hops >= 1

    def test_enable_trust_modulation_default(self):
        """Test trust modulation default."""
        config = NeuralMemoryConfig()
        assert isinstance(config.enable_trust_modulation, bool)

    def test_gradient_clipping_default(self):
        """Test gradient clipping default."""
        config = NeuralMemoryConfig()
        assert config.gradient_clipping >= 0
