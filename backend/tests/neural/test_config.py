"""Tests for NeuralMemoryConfig."""

import pytest

from neural.config import NeuralMemoryConfig


class TestNeuralMemoryConfig:
    """Test NeuralMemoryConfig dataclass."""

    def test_default_values(self):
        """Test default configuration is valid."""
        config = NeuralMemoryConfig()
        assert config.spread_hops >= 1
        assert 0 < config.spread_decay <= 1.0
        assert config.spread_threshold >= 0
        assert config.learning_rate > 0
        assert isinstance(config.enable_decay, bool)
        assert config.weight_max > 0

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


class TestSleepMaintenanceConfig:
    """Test Sleep Maintenance configuration fields (Issue #101)."""

    def test_sleep_defaults(self):
        """Test sleep config defaults are valid."""
        config = NeuralMemoryConfig()
        assert config.sleep_enabled is False
        assert config.sleep_cron_hour == 2
        assert config.sleep_cron_minute == 0
        assert config.sleep_llm_provider == "openai"
        assert config.sleep_llm_model == "gpt-5-nano"
        assert config.sleep_max_memories_per_run == 200
        assert config.sleep_max_llm_calls_per_run == 50
        assert config.sleep_dedup_enabled is True
        assert config.sleep_dedup_similarity_threshold == 0.92
        assert config.sleep_edge_discovery_enabled is True
        assert config.sleep_edge_discovery_sample_size == 30
        assert config.sleep_importance_reeval_enabled is True

    def test_sleep_custom_values(self):
        """Test sleep config with custom values."""
        config = NeuralMemoryConfig(
            sleep_enabled=True,
            sleep_cron_hour=4,
            sleep_llm_provider="ollama",
            sleep_llm_model="qwen2.5:7b",
            sleep_max_memories_per_run=500,
            sleep_max_llm_calls_per_run=100,
            sleep_dedup_similarity_threshold=0.95,
            sleep_edge_discovery_sample_size=50,
        )
        assert config.sleep_enabled is True
        assert config.sleep_cron_hour == 4
        assert config.sleep_llm_provider == "ollama"
        assert config.sleep_llm_model == "qwen2.5:7b"
        assert config.sleep_max_memories_per_run == 500
        assert config.sleep_dedup_similarity_threshold == 0.95

    def test_sleep_cron_hour_validation(self):
        """Test cron hour must be 0-23."""
        with pytest.raises(ValueError, match="sleep_cron_hour"):
            NeuralMemoryConfig(sleep_cron_hour=24)
        with pytest.raises(ValueError, match="sleep_cron_hour"):
            NeuralMemoryConfig(sleep_cron_hour=-1)

    def test_sleep_cron_minute_validation(self):
        """Test cron minute must be 0-59."""
        with pytest.raises(ValueError, match="sleep_cron_minute"):
            NeuralMemoryConfig(sleep_cron_minute=60)

    def test_sleep_llm_provider_validation(self):
        """Test LLM provider must be openai or ollama."""
        with pytest.raises(ValueError, match="sleep_llm_provider"):
            NeuralMemoryConfig(sleep_llm_provider="anthropic")

    def test_sleep_max_memories_validation(self):
        """Test max memories must be positive."""
        with pytest.raises(ValueError, match="sleep_max_memories_per_run"):
            NeuralMemoryConfig(sleep_max_memories_per_run=0)

    def test_sleep_max_llm_calls_validation(self):
        """Test max LLM calls must be positive."""
        with pytest.raises(ValueError, match="sleep_max_llm_calls_per_run"):
            NeuralMemoryConfig(sleep_max_llm_calls_per_run=0)

    def test_sleep_dedup_threshold_validation(self):
        """Test dedup threshold must be in [0.5, 1.0]."""
        with pytest.raises(ValueError, match="sleep_dedup_similarity_threshold"):
            NeuralMemoryConfig(sleep_dedup_similarity_threshold=0.3)
        with pytest.raises(ValueError, match="sleep_dedup_similarity_threshold"):
            NeuralMemoryConfig(sleep_dedup_similarity_threshold=1.1)

    def test_sleep_sample_size_validation(self):
        """Test sample size must be positive."""
        with pytest.raises(ValueError, match="sleep_edge_discovery_sample_size"):
            NeuralMemoryConfig(sleep_edge_discovery_sample_size=0)

    def test_from_env_includes_sleep_defaults(self):
        """Test from_env() includes sleep params with defaults."""
        config = NeuralMemoryConfig.from_env()
        assert config.sleep_enabled is False
        assert config.sleep_llm_provider == "openai"
        assert config.sleep_max_memories_per_run == 200

    def test_from_env_with_sleep_env_vars(self, monkeypatch):
        """Test from_env() reads SLEEP_* environment variables."""
        monkeypatch.setenv("SLEEP_ENABLED", "true")
        monkeypatch.setenv("SLEEP_CRON_HOUR", "5")
        monkeypatch.setenv("SLEEP_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("SLEEP_LLM_MODEL", "llama3:8b")
        monkeypatch.setenv("SLEEP_MAX_MEMORIES_PER_RUN", "1000")
        monkeypatch.setenv("SLEEP_DEDUP_SIMILARITY_THRESHOLD", "0.95")

        config = NeuralMemoryConfig.from_env()
        assert config.sleep_enabled is True
        assert config.sleep_cron_hour == 5
        assert config.sleep_llm_provider == "ollama"
        assert config.sleep_llm_model == "llama3:8b"
        assert config.sleep_max_memories_per_run == 1000
        assert config.sleep_dedup_similarity_threshold == 0.95
