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


class TestEdgeGateCalibrationConfig:
    """Edge-gate percentile calibration config fields (Issue #982).

    ``min_similarity_for_edge`` becomes the absolute fallback when no
    calibration row exists; the percentile + floor drive the calibration
    path (random-pair baseline distribution).
    """

    def test_edge_calibration_defaults(self):
        """Defaults: high percentile (random-pair upper tail) + floor."""
        config = NeuralMemoryConfig()
        assert config.min_similarity_for_edge_percentile == 95.0
        assert config.min_similarity_for_edge_floor == 0.3
        # Absolute fallback unchanged from prior behavior.
        assert config.min_similarity_for_edge == 0.5

    def test_edge_calibration_custom_values(self):
        config = NeuralMemoryConfig(
            min_similarity_for_edge_percentile=99.0,
            min_similarity_for_edge_floor=0.25,
        )
        assert config.min_similarity_for_edge_percentile == 99.0
        assert config.min_similarity_for_edge_floor == 0.25

    def test_edge_percentile_must_be_in_open_unit_interval(self):
        with pytest.raises(ValueError, match="min_similarity_for_edge_percentile"):
            NeuralMemoryConfig(min_similarity_for_edge_percentile=0.0)
        with pytest.raises(ValueError, match="min_similarity_for_edge_percentile"):
            NeuralMemoryConfig(min_similarity_for_edge_percentile=100.0)

    def test_edge_floor_must_be_in_unit_interval(self):
        with pytest.raises(ValueError, match="min_similarity_for_edge_floor"):
            NeuralMemoryConfig(min_similarity_for_edge_floor=1.5)
        with pytest.raises(ValueError, match="min_similarity_for_edge_floor"):
            NeuralMemoryConfig(min_similarity_for_edge_floor=-0.1)

    def test_edge_calibration_from_env(self, monkeypatch):
        monkeypatch.setenv("MIN_SIMILARITY_FOR_EDGE_PERCENTILE", "97.5")
        monkeypatch.setenv("MIN_SIMILARITY_FOR_EDGE_FLOOR", "0.35")
        config = NeuralMemoryConfig.from_env()
        assert config.min_similarity_for_edge_percentile == 97.5
        assert config.min_similarity_for_edge_floor == 0.35


class TestEdgeGateRepetitionConfig:
    """2D edge gate repetition axis (Issue #983).

    ``edge_gate_repetition_enabled`` is the rollback switch for the second
    gate axis: pairs in the [floor, calibrated-threshold) cosine band may
    form edges once their co-activation count reaches
    ``min_co_activation_count`` (the existing knob, previously unconsulted
    on the edge-formation path).
    """

    def test_repetition_gate_default_enabled(self):
        config = NeuralMemoryConfig()
        assert config.edge_gate_repetition_enabled is True
        # Default 4 from the measured #983 tradeoff curve: evidence >= 4 is
        # the point where recovery@10 lift stays > 0 while the noise-side
        # non_gold_form_rate returns to the p95 baseline.
        assert config.edge_gate_min_evidence == 4

    def test_repetition_gate_can_be_disabled(self):
        config = NeuralMemoryConfig(edge_gate_repetition_enabled=False)
        assert config.edge_gate_repetition_enabled is False

    def test_repetition_gate_from_env(self, monkeypatch):
        monkeypatch.setenv("EDGE_GATE_REPETITION_ENABLED", "false")
        monkeypatch.setenv("EDGE_GATE_MIN_EVIDENCE", "2")
        config = NeuralMemoryConfig.from_env()
        assert config.edge_gate_repetition_enabled is False
        assert config.edge_gate_min_evidence == 2

    def test_min_evidence_must_be_positive(self):
        with pytest.raises(ValueError, match="edge_gate_min_evidence"):
            NeuralMemoryConfig(edge_gate_min_evidence=0)


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


class TestTagCooccurrenceConfig:
    """Test tag co-occurrence seeding configuration fields (Issue #223)."""

    def test_defaults(self):
        config = NeuralMemoryConfig()
        assert config.tag_cooccurrence_enabled is True
        assert config.tag_cooccurrence_min_shared == 2
        assert config.tag_cooccurrence_max_per_remember == 10
        assert config.tag_cooccurrence_hub_threshold == 0.30
        assert config.tag_cooccurrence_max_degree_per_node == 50

    def test_custom_values(self):
        config = NeuralMemoryConfig(
            tag_cooccurrence_enabled=False,
            tag_cooccurrence_min_shared=3,
            tag_cooccurrence_max_per_remember=20,
            tag_cooccurrence_hub_threshold=0.25,
            tag_cooccurrence_max_degree_per_node=100,
        )
        assert config.tag_cooccurrence_enabled is False
        assert config.tag_cooccurrence_min_shared == 3
        assert config.tag_cooccurrence_max_per_remember == 20
        assert config.tag_cooccurrence_hub_threshold == 0.25
        assert config.tag_cooccurrence_max_degree_per_node == 100

    def test_min_shared_validation(self):
        with pytest.raises(ValueError, match="tag_cooccurrence_min_shared"):
            NeuralMemoryConfig(tag_cooccurrence_min_shared=0)
        with pytest.raises(ValueError, match="tag_cooccurrence_min_shared"):
            NeuralMemoryConfig(tag_cooccurrence_min_shared=11)

    def test_max_per_remember_validation(self):
        with pytest.raises(ValueError, match="tag_cooccurrence_max_per_remember"):
            NeuralMemoryConfig(tag_cooccurrence_max_per_remember=0)
        with pytest.raises(ValueError, match="tag_cooccurrence_max_per_remember"):
            NeuralMemoryConfig(tag_cooccurrence_max_per_remember=101)

    def test_hub_threshold_validation(self):
        with pytest.raises(ValueError, match="tag_cooccurrence_hub_threshold"):
            NeuralMemoryConfig(tag_cooccurrence_hub_threshold=-0.1)
        with pytest.raises(ValueError, match="tag_cooccurrence_hub_threshold"):
            NeuralMemoryConfig(tag_cooccurrence_hub_threshold=1.5)

    def test_max_degree_validation(self):
        with pytest.raises(ValueError, match="tag_cooccurrence_max_degree_per_node"):
            NeuralMemoryConfig(tag_cooccurrence_max_degree_per_node=0)
        with pytest.raises(ValueError, match="tag_cooccurrence_max_degree_per_node"):
            NeuralMemoryConfig(tag_cooccurrence_max_degree_per_node=1001)

    def test_from_env_defaults(self):
        config = NeuralMemoryConfig.from_env()
        assert config.tag_cooccurrence_enabled is True
        assert config.tag_cooccurrence_min_shared == 2
        assert config.tag_cooccurrence_max_per_remember == 10
        assert config.tag_cooccurrence_hub_threshold == 0.30
        assert config.tag_cooccurrence_max_degree_per_node == 50

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("TAG_COOCCURRENCE_ENABLED", "false")
        monkeypatch.setenv("TAG_COOCCURRENCE_MIN_SHARED", "3")
        monkeypatch.setenv("TAG_COOCCURRENCE_MAX_PER_REMEMBER", "25")
        monkeypatch.setenv("TAG_COOCCURRENCE_HUB_THRESHOLD", "0.40")
        monkeypatch.setenv("TAG_COOCCURRENCE_MAX_DEGREE_PER_NODE", "75")

        config = NeuralMemoryConfig.from_env()
        assert config.tag_cooccurrence_enabled is False
        assert config.tag_cooccurrence_min_shared == 3
        assert config.tag_cooccurrence_max_per_remember == 25
        assert config.tag_cooccurrence_hub_threshold == 0.40
        assert config.tag_cooccurrence_max_degree_per_node == 75
