"""Neural Memory Network configuration.

This module defines the configuration dataclass for the Neural Memory system,
including hyperparameters for Hebbian learning, activation spreading, scoring,
and forgetting mechanisms.
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Cache TTL in seconds (5 minutes)
_CONFIG_CACHE_TTL = 300


@dataclass
class NeuralMemoryConfig:
    """Configuration for Neural Memory Network.

    Note: User sharding is ALWAYS enabled at the database schema level.
    All memory operations are isolated by user_id for GDPR compliance.
    This cannot be disabled via configuration.

    Class-level cache is used for from_db() to avoid repeated database queries.
    Cache TTL is 5 minutes (_CONFIG_CACHE_TTL).

    Attributes:
        # Hebbian Learning
        learning_rate: Learning rate (η) for Hebbian updates (0.0-1.0)
        decay_lambda: L2 decay coefficient (λ) to prevent weight explosion
        weight_max: Maximum edge weight (clipping threshold)
        top_m_edges: Keep only top-M strongest edges per node (sparsity)

        # Activation Spreading
        spread_hops: Number of hops for activation propagation (1-3)
        spread_decay: Decay factor (λ) for each hop (0.0-1.0)
        spread_threshold: Minimum activation value to continue spreading

        # Scoring Weights (must sum to ~1.0 for semantic+graph+temporal+trust)
        alpha: Weight for semantic similarity (embedding cosine)
        beta: Weight for graph association (activation spreading)
        gamma: Weight for recency (temporal decay)
        delta: Weight for importance (use count + LLM score)
        epsilon: Weight for trust (confidence score)
        zeta: Weight for redundancy penalty (MMR-style)

        # Temporal Parameters
        recency_tau_days: Time constant (τ) for exponential recency decay
        importance_ema_alpha: EMA smoothing for importance updates

        # Co-Activation Tracking
        track_co_activation: Enable co-activation tracking
        co_activation_window: Time window (seconds) for same-session tracking
        min_co_activation_count: Minimum count to create/strengthen edge

        # Forgetting/Decay
        enable_decay: Enable automatic edge weight decay
        decay_background_interval: Interval (seconds) for background decay task
        decay_rate: DEPRECATED (Issue #970) — the old per-second exp decay rate.
            No longer used for edge decay; retained only so existing env/DB
            neural_config values load without error. Use
            hebbian_decay_half_life_days instead.
        hebbian_decay_half_life_days: Hebbian edge weight half-life in days
            (Issue #970). After this many days an unreinforced edge's weight
            halves. Matches the recency_tau_days "days" convention.
        prune_threshold: Remove edges below this weight

        # Consolidation (Short-term → Long-term)
        consolidation_use_count_min: Min use_count for long-term promotion
        consolidation_importance_min: Min importance for long-term promotion
        consolidation_diversity_min: Min diversity score for long-term promotion

        # Privacy & Security (SISA-compliant)
        enable_trust_modulation: Modulate learning rate by confidence score
        gradient_clipping: Clip total Δw per update (DP-SGD style)

        # Performance
        batch_update_size: Batch size for delayed Hebbian updates
        async_update_delay_ms: Delay (ms) before applying updates
        max_candidates_k: Maximum candidates for primary retrieval

        # Sleep Maintenance (Issue #101)
        sleep_enabled: Master switch for Sleep Maintenance (env-only)
        sleep_cron_hour: UTC hour for scheduled sleep run (env-only)
        sleep_cron_minute: UTC minute for scheduled sleep run (env-only)
        sleep_llm_provider: LLM provider (openai / ollama)
        sleep_llm_model: LLM model name for sleep judgments
        sleep_max_memories_per_run: Max memories processed per run
        sleep_max_llm_calls_per_run: LLM call budget per run
        sleep_dedup_enabled: Enable dedup/merge phase
        sleep_dedup_similarity_threshold: Cosine threshold for duplicate detection
        sleep_edge_discovery_enabled: Enable edge discovery phase
        sleep_edge_discovery_sample_size: Memories sampled for edge discovery
        sleep_importance_reeval_enabled: Enable importance re-evaluation phase

        # Tag Co-Occurrence Cold-Start Seeding (Issue #223, Tier 2)
        tag_cooccurrence_enabled: Master switch for tag-cooccurrence seeding
        tag_cooccurrence_min_shared: Minimum shared tags to create an edge (1-10)
        tag_cooccurrence_max_per_remember: Top-N matches per remember() (1-100)
        tag_cooccurrence_hub_threshold: Frequency above which a tag is "hub"
            and excluded from co-occurrence (0.0-1.0)
        tag_cooccurrence_max_degree_per_node: Per-source-node degree cap (1-1000)
    """

    # Class-level cache for from_db() (not serialized as instance fields)
    _db_cache: ClassVar[dict[str, "NeuralMemoryConfig"]] = {}
    _db_cache_time: ClassVar[float] = 0.0

    # Hebbian Learning
    learning_rate: float = 0.05
    decay_lambda: float = 0.01
    weight_max: float = 3.0
    top_m_edges: int = 8  # Changed from 32 to 8 (Issue #107)

    # Activation Spreading
    spread_hops: int = 1
    spread_decay: float = 0.6
    spread_threshold: float = 0.01

    # Scoring Weights (Research paper defaults)
    alpha: float = 0.55  # Semantic similarity
    beta: float = 0.20  # Graph association
    gamma: float = 0.10  # Recency
    delta: float = 0.10  # Importance
    epsilon: float = 0.05  # Trust
    zeta: float = 0.25  # Redundancy penalty

    # Temporal Parameters
    recency_tau_days: float = 14.0  # 2-week half-life
    importance_ema_alpha: float = 0.3

    # Co-Activation Tracking
    track_co_activation: bool = True
    co_activation_window: int = 300  # 5 minutes
    min_co_activation_count: int = 2
    min_similarity_for_edge: float = 0.5  # Semantic gating threshold
    max_assoc_score: float = 0.5  # Cap graph association score per node
    top_k_coactivation: int = 3  # Only co-activate top-k results

    # Forgetting/Decay
    enable_decay: bool = True
    decay_background_interval: int = 3600  # 1 hour
    decay_rate: float = 0.001  # DEPRECATED (Issue #970): unused; see hebbian_decay_half_life_days
    hebbian_decay_half_life_days: float = 14.0  # Issue #970: Hebbian edge half-life (days)
    prune_threshold: float = 0.01  # Lowered to allow weak initial edges (was 0.05)

    # Consolidation
    consolidation_use_count_min: int = 3
    consolidation_importance_min: float = 0.65
    consolidation_diversity_min: float = 0.2

    # Privacy & Security
    enable_trust_modulation: bool = True  # Poisoning defense
    gradient_clipping: float = 0.5  # Max total Δw per node per update

    # Performance
    batch_update_size: int = 100
    async_update_delay_ms: int = 2000  # 2 seconds
    max_candidates_k: int = 64

    # k-NN Cold-Start Seeding (Issue #221, #406 Phase B)
    # On remember(), search for k nearest neighbors and create weak
    # `semantic_similarity` edges so new memories are not born as isolated nodes.
    knn_seed_enabled: bool = True
    knn_seed_k: int = 5  # Neighbors per new memory (1-20)
    # Cosine threshold override. None (default) → use `embedding_calibrations`
    # percentile path; non-None → raw operator override (D6). Prior to #406,
    # this was hardcoded 0.4, which is over-permissive for text-embedding-3-
    # small (production p90 = 0.6322 per Phase A acceptance) and wrong-
    # dimension for other models (qwen3-8b top-k p90 = 0.5816 per #407 Task 1).
    # See #240 D1 for why the calibration path uses top-k neighbor distribution
    # rather than random-pair baseline.
    knn_seed_min_similarity: float | None = None
    # Percentile of the top-k neighbor distribution that serves as the default
    # runtime threshold when `knn_seed_min_similarity` is None (#240 D2, D4).
    # 90.0 = top decile = reject bottom 90% as "not a high-quality seeding
    # candidate". Validated across text-embedding-3-small (512d) and qwen3-
    # embedding:8b (4096d) in Phase A and #407 Task 1 respectively.
    knn_seed_min_percentile: float = 90.0
    # Hard lower bound for the calibration-derived threshold (#240 D2). Protects
    # sparse/anomalous corpora where p90 < 0.3 would mean "accept everything".
    # Applied as `max(percentile(p90), floor)` in `resolve_knn_threshold`.
    knn_seed_min_similarity_floor: float = 0.3
    # TTL for embedding_calibrations rows (#240 D7, gate1 review). Kept in code
    # config rather than hardcoded in migration so #407 Task 2's drift signal
    # can tune it via env var without a schema change.
    calibration_ttl_days: int = 30
    knn_seed_weight: float = (
        0.3  # Intentionally low — synthetic signal, Sleep Maintenance prunes if unused
    )

    # Tag Co-Occurrence Cold-Start Seeding (Issue #223, Tier 2)
    # On remember(), find existing memories sharing 2+ tags within the same
    # (user, workspace, context) and create weak ``tag_cooccurrence`` edges.
    # Complementary to k-NN: deterministic, free (no embedding cost), and
    # surfaces structure that semantic similarity alone misses (e.g., two
    # memories about different topics that share the same project tags).
    tag_cooccurrence_enabled: bool = True
    tag_cooccurrence_min_shared: int = 2  # Minimum shared tags to create edge (1-10)
    tag_cooccurrence_max_per_remember: int = 10  # Top-N matches per remember() (1-100)
    # Hub-tag exclusion: tags appearing on > threshold * memory_count in a
    # (workspace, context) are excluded from co-occurrence. Computed nightly by
    # Sleep Maintenance and cached in ``hub_tag_cache``.
    tag_cooccurrence_hub_threshold: float = 0.30  # 0.0-1.0
    tag_cooccurrence_max_degree_per_node: int = 50  # Per-node degree cap (1-1000)

    # Sleep Maintenance (Issue #101)
    sleep_enabled: bool = False  # Feature flag (env-only)
    sleep_cron_hour: int = 2  # UTC hour for cron schedule (env-only)
    sleep_cron_minute: int = 0  # UTC minute for cron schedule (env-only)
    sleep_llm_provider: str = "openai"  # LLM provider: openai / ollama
    sleep_llm_model: str = "gpt-5-nano"  # LLM model name
    sleep_max_memories_per_run: int = 200  # Batch size cap per run
    sleep_max_llm_calls_per_run: int = 50  # LLM call budget per run
    sleep_dedup_enabled: bool = True  # Phase 2 on/off
    sleep_dedup_similarity_threshold: float = 0.92  # Cosine similarity for dedup
    sleep_edge_discovery_enabled: bool = True  # Phase 1 on/off
    sleep_edge_discovery_sample_size: int = 30  # Memories sampled per run
    sleep_importance_reeval_enabled: bool = True  # Phase 3 on/off

    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        # Validate ranges
        if not (0.0 <= self.learning_rate <= 1.0):
            raise ValueError(f"learning_rate must be in [0, 1], got {self.learning_rate}")
        if not (0.0 <= self.decay_lambda <= 1.0):
            raise ValueError(f"decay_lambda must be in [0, 1], got {self.decay_lambda}")
        if not self.weight_max > 0:
            raise ValueError(f"weight_max must be positive, got {self.weight_max}")
        if not self.top_m_edges > 0:
            raise ValueError(f"top_m_edges must be positive, got {self.top_m_edges}")

        if not (1 <= self.spread_hops <= 3):
            raise ValueError(f"spread_hops must be in [1, 3], got {self.spread_hops}")
        if not (0.0 <= self.spread_decay <= 1.0):
            raise ValueError(f"spread_decay must be in [0, 1], got {self.spread_decay}")

        # Validate scoring weights (should sum to ~1.0 for primary signals)
        primary_sum = self.alpha + self.beta + self.gamma + self.delta + self.epsilon
        if not (0.9 <= primary_sum <= 1.1):
            import warnings

            warnings.warn(
                f"Scoring weights sum to {primary_sum:.2f}, expected ~1.0. "
                "This may cause score normalization issues.",
                stacklevel=2,
            )

        if not self.recency_tau_days > 0:
            raise ValueError(f"recency_tau_days must be positive, got {self.recency_tau_days}")
        if not (0.0 <= self.importance_ema_alpha <= 1.0):
            raise ValueError(
                f"importance_ema_alpha must be in [0, 1], got {self.importance_ema_alpha}"
            )

        if not self.co_activation_window > 0:
            raise ValueError(
                f"co_activation_window must be positive, got {self.co_activation_window}"
            )
        if not self.min_co_activation_count > 0:
            raise ValueError(
                f"min_co_activation_count must be positive, got {self.min_co_activation_count}"
            )
        if not (0.0 <= self.min_similarity_for_edge <= 1.0):
            raise ValueError(
                f"min_similarity_for_edge must be in [0, 1], got {self.min_similarity_for_edge}"
            )
        if not self.max_assoc_score > 0:
            raise ValueError(f"max_assoc_score must be positive, got {self.max_assoc_score}")
        if not self.top_k_coactivation > 0:
            raise ValueError(f"top_k_coactivation must be positive, got {self.top_k_coactivation}")

        if not self.decay_rate >= 0:
            raise ValueError(f"decay_rate must be non-negative, got {self.decay_rate}")
        if not self.hebbian_decay_half_life_days > 0:
            raise ValueError(
                "hebbian_decay_half_life_days must be positive, got "
                f"{self.hebbian_decay_half_life_days}"
            )
        if not (0.0 <= self.prune_threshold <= 1.0):
            raise ValueError(f"prune_threshold must be in [0, 1], got {self.prune_threshold}")

        if not self.consolidation_use_count_min > 0:
            raise ValueError(
                f"consolidation_use_count_min must be positive, "
                f"got {self.consolidation_use_count_min}"
            )
        if not (0.0 <= self.consolidation_importance_min <= 1.0):
            raise ValueError(
                f"consolidation_importance_min must be in [0, 1], "
                f"got {self.consolidation_importance_min}"
            )

        if not self.gradient_clipping > 0:
            raise ValueError(f"gradient_clipping must be positive, got {self.gradient_clipping}")

        if not self.batch_update_size > 0:
            raise ValueError(f"batch_update_size must be positive, got {self.batch_update_size}")
        if not self.async_update_delay_ms >= 0:
            raise ValueError(
                f"async_update_delay_ms must be non-negative, got {self.async_update_delay_ms}"
            )
        if not self.max_candidates_k > 0:
            raise ValueError(f"max_candidates_k must be positive, got {self.max_candidates_k}")

        # k-NN Cold-Start Seeding validation (Issue #221, #406)
        if not (1 <= self.knn_seed_k <= 20):
            raise ValueError(f"knn_seed_k must be in [1, 20], got {self.knn_seed_k}")
        if self.knn_seed_min_similarity is not None and not (
            0.0 <= self.knn_seed_min_similarity <= 1.0
        ):
            raise ValueError(
                f"knn_seed_min_similarity must be in [0, 1] or None, "
                f"got {self.knn_seed_min_similarity}"
            )
        if not (0.0 < self.knn_seed_min_percentile < 100.0):
            raise ValueError(
                f"knn_seed_min_percentile must be in (0, 100), got {self.knn_seed_min_percentile}"
            )
        if not (0.0 <= self.knn_seed_min_similarity_floor <= 1.0):
            raise ValueError(
                f"knn_seed_min_similarity_floor must be in [0, 1], "
                f"got {self.knn_seed_min_similarity_floor}"
            )
        if not self.calibration_ttl_days > 0:
            raise ValueError(
                f"calibration_ttl_days must be positive, got {self.calibration_ttl_days}"
            )
        if not (0.0 <= self.knn_seed_weight <= 1.0):
            raise ValueError(f"knn_seed_weight must be in [0, 1], got {self.knn_seed_weight}")

        # Tag Co-Occurrence Cold-Start Seeding validation (Issue #223)
        if not (1 <= self.tag_cooccurrence_min_shared <= 10):
            raise ValueError(
                f"tag_cooccurrence_min_shared must be in [1, 10], "
                f"got {self.tag_cooccurrence_min_shared}"
            )
        if not (1 <= self.tag_cooccurrence_max_per_remember <= 100):
            raise ValueError(
                f"tag_cooccurrence_max_per_remember must be in [1, 100], "
                f"got {self.tag_cooccurrence_max_per_remember}"
            )
        if not (0.0 <= self.tag_cooccurrence_hub_threshold <= 1.0):
            raise ValueError(
                f"tag_cooccurrence_hub_threshold must be in [0, 1], "
                f"got {self.tag_cooccurrence_hub_threshold}"
            )
        if not (1 <= self.tag_cooccurrence_max_degree_per_node <= 1000):
            raise ValueError(
                f"tag_cooccurrence_max_degree_per_node must be in [1, 1000], "
                f"got {self.tag_cooccurrence_max_degree_per_node}"
            )

        # Sleep Maintenance validation
        if not (0 <= self.sleep_cron_hour <= 23):
            raise ValueError(f"sleep_cron_hour must be in [0, 23], got {self.sleep_cron_hour}")
        if not (0 <= self.sleep_cron_minute <= 59):
            raise ValueError(f"sleep_cron_minute must be in [0, 59], got {self.sleep_cron_minute}")
        if self.sleep_llm_provider not in ("openai", "ollama", ""):
            raise ValueError(
                f"sleep_llm_provider must be 'openai', 'ollama', or '', got '{self.sleep_llm_provider}'"
            )
        if not self.sleep_max_memories_per_run > 0:
            raise ValueError(
                f"sleep_max_memories_per_run must be positive, "
                f"got {self.sleep_max_memories_per_run}"
            )
        if not self.sleep_max_llm_calls_per_run > 0:
            raise ValueError(
                f"sleep_max_llm_calls_per_run must be positive, "
                f"got {self.sleep_max_llm_calls_per_run}"
            )
        if not (0.5 <= self.sleep_dedup_similarity_threshold <= 1.0):
            raise ValueError(
                f"sleep_dedup_similarity_threshold must be in [0.5, 1.0], "
                f"got {self.sleep_dedup_similarity_threshold}"
            )
        if not self.sleep_edge_discovery_sample_size > 0:
            raise ValueError(
                f"sleep_edge_discovery_sample_size must be positive, "
                f"got {self.sleep_edge_discovery_sample_size}"
            )

    @property
    def scoring_weights_normalized(self) -> dict[str, float]:
        """Get normalized scoring weights (sum=1.0)."""
        total = self.alpha + self.beta + self.gamma + self.delta + self.epsilon
        return {
            "alpha": self.alpha / total,
            "beta": self.beta / total,
            "gamma": self.gamma / total,
            "delta": self.delta / total,
            "epsilon": self.epsilon / total,
            "zeta": self.zeta,  # Penalty, not normalized
        }

    @classmethod
    def from_env(cls) -> "NeuralMemoryConfig":
        """Load configuration from environment variables with fallback to defaults.

        Returns:
            NeuralMemoryConfig instance with values from env or defaults
        """
        import os

        def get_float(key: str, default: float) -> float:
            return float(os.getenv(key, str(default)))

        def get_float_or_none(key: str, default: float | None = None) -> float | None:
            """Parse a float-or-None env var.

            - Unset env var → ``default`` (``None`` unless caller overrides).
            - Empty / ``"none"`` / ``"null"`` / ``"auto"`` (case-insensitive)
              → ``None`` explicitly.
            - Any other string → parsed as float.

            Used for config fields where ``None`` means "delegate to calibration
            path" (Issue #406). The ``default`` parameter lets callers pick
            their own "unset" sentinel — the knn_seed_min_similarity call site
            passes ``None`` so that unset means "use calibration", but future
            optional-float fields can override. (Copilot review PR #420 loop 8.)
            """
            raw = os.getenv(key)
            if raw is None:
                return default
            stripped = raw.strip().lower()
            if stripped in ("", "none", "null", "auto"):
                return None
            return float(raw)

        def get_int(key: str, default: int) -> int:
            return int(os.getenv(key, str(default)))

        def get_bool(key: str, default: bool) -> bool:
            return os.getenv(key, str(default)).lower() == "true"

        return cls(
            # Hebbian Learning
            learning_rate=get_float("LEARNING_RATE", 0.05),
            decay_lambda=get_float("DECAY_LAMBDA", 0.01),
            weight_max=get_float("WEIGHT_MAX", 3.0),
            top_m_edges=get_int("TOP_M_EDGES", 8),  # Changed default from 32 to 8
            # Activation Spreading
            spread_hops=get_int("SPREAD_HOPS", 1),
            spread_decay=get_float("SPREAD_DECAY", 0.6),
            spread_threshold=get_float("SPREAD_THRESHOLD", 0.01),
            # Scoring Weights
            alpha=get_float("ALPHA", 0.55),
            beta=get_float("BETA", 0.20),
            gamma=get_float("GAMMA", 0.10),
            delta=get_float("DELTA", 0.10),
            epsilon=get_float("EPSILON", 0.05),
            zeta=get_float("ZETA", 0.25),
            # Temporal
            recency_tau_days=get_float("RECENCY_TAU_DAYS", 14.0),
            importance_ema_alpha=get_float("IMPORTANCE_EMA_ALPHA", 0.3),
            # Co-Activation
            track_co_activation=get_bool("TRACK_CO_ACTIVATION", True),
            co_activation_window=get_int("CO_ACTIVATION_WINDOW", 300),
            min_co_activation_count=get_int("MIN_CO_ACTIVATION_COUNT", 2),
            min_similarity_for_edge=get_float("MIN_SIMILARITY_FOR_EDGE", 0.5),
            max_assoc_score=get_float("MAX_ASSOC_SCORE", 0.5),
            top_k_coactivation=get_int("TOP_K_COACTIVATION", 3),
            # Forgetting/Decay
            enable_decay=get_bool("ENABLE_DECAY", True),
            decay_background_interval=get_int("DECAY_BACKGROUND_INTERVAL", 3600),
            decay_rate=get_float("DECAY_RATE", 0.001),
            hebbian_decay_half_life_days=get_float("HEBBIAN_DECAY_HALF_LIFE_DAYS", 14.0),
            prune_threshold=get_float("PRUNE_THRESHOLD", 0.01),
            # Consolidation
            consolidation_use_count_min=get_int("CONSOLIDATION_USE_COUNT_MIN", 3),
            consolidation_importance_min=get_float("CONSOLIDATION_IMPORTANCE_MIN", 0.65),
            consolidation_diversity_min=get_float("CONSOLIDATION_DIVERSITY_MIN", 0.2),
            # Privacy & Security
            enable_trust_modulation=get_bool("ENABLE_TRUST_MODULATION", True),
            gradient_clipping=get_float("GRADIENT_CLIPPING", 0.5),
            # Performance
            batch_update_size=get_int("BATCH_UPDATE_SIZE", 100),
            async_update_delay_ms=get_int("ASYNC_UPDATE_DELAY_MS", 2000),
            max_candidates_k=get_int("MAX_CANDIDATES_K", 64),
            # k-NN Cold-Start Seeding (Issue #221, #406)
            knn_seed_enabled=get_bool("KNN_SEED_ENABLED", True),
            knn_seed_k=get_int("KNN_SEED_K", 5),
            knn_seed_min_similarity=get_float_or_none("KNN_SEED_MIN_SIMILARITY"),
            knn_seed_min_percentile=get_float("KNN_SEED_MIN_PERCENTILE", 90.0),
            knn_seed_min_similarity_floor=get_float("KNN_SEED_MIN_SIMILARITY_FLOOR", 0.3),
            calibration_ttl_days=get_int("CALIBRATION_TTL_DAYS", 30),
            knn_seed_weight=get_float("KNN_SEED_WEIGHT", 0.3),
            # Tag Co-Occurrence Cold-Start Seeding (Issue #223)
            tag_cooccurrence_enabled=get_bool("TAG_COOCCURRENCE_ENABLED", True),
            tag_cooccurrence_min_shared=get_int("TAG_COOCCURRENCE_MIN_SHARED", 2),
            tag_cooccurrence_max_per_remember=get_int("TAG_COOCCURRENCE_MAX_PER_REMEMBER", 10),
            tag_cooccurrence_hub_threshold=get_float("TAG_COOCCURRENCE_HUB_THRESHOLD", 0.30),
            tag_cooccurrence_max_degree_per_node=get_int(
                "TAG_COOCCURRENCE_MAX_DEGREE_PER_NODE", 50
            ),
            # Sleep Maintenance (env-only flags + DB-configurable params)
            sleep_enabled=get_bool("SLEEP_ENABLED", False),
            sleep_cron_hour=get_int("SLEEP_CRON_HOUR", 2),
            sleep_cron_minute=get_int("SLEEP_CRON_MINUTE", 0),
            sleep_llm_provider=os.getenv("SLEEP_LLM_PROVIDER", "openai"),
            sleep_llm_model=os.getenv("SLEEP_LLM_MODEL", "gpt-5-nano"),
            sleep_max_memories_per_run=get_int("SLEEP_MAX_MEMORIES_PER_RUN", 200),
            sleep_max_llm_calls_per_run=get_int("SLEEP_MAX_LLM_CALLS_PER_RUN", 50),
            sleep_dedup_enabled=get_bool("SLEEP_DEDUP_ENABLED", True),
            sleep_dedup_similarity_threshold=get_float("SLEEP_DEDUP_SIMILARITY_THRESHOLD", 0.92),
            sleep_edge_discovery_enabled=get_bool("SLEEP_EDGE_DISCOVERY_ENABLED", True),
            sleep_edge_discovery_sample_size=get_int("SLEEP_EDGE_DISCOVERY_SAMPLE_SIZE", 30),
            sleep_importance_reeval_enabled=get_bool("SLEEP_IMPORTANCE_REEVAL_ENABLED", True),
        )

    @classmethod
    async def from_db(cls, db: "AsyncSession") -> "NeuralMemoryConfig":
        """Load configuration from database with fallback to defaults.

        Issue #107: Database-driven configuration for admin management.
        Uses class-level cache with 5-minute TTL to avoid repeated DB queries.

        Args:
            db: AsyncSession for database access

        Returns:
            NeuralMemoryConfig instance with values from DB or defaults
        """
        # Check cache first
        cache_key = "default"
        if cache_key in cls._db_cache and time.time() - cls._db_cache_time < _CONFIG_CACHE_TTL:
            return cls._db_cache[cache_key]

        from sqlalchemy import select

        from models.neural import NeuralConfig

        # Get all config from database
        result = await db.execute(select(NeuralConfig))
        configs = {c.key: c.get_typed_value() for c in result.scalars().all()}

        # Start with env-based config (which has all defaults)
        base_config = cls.from_env()

        # Override with DB values where available
        config = cls(
            # Hebbian Learning
            learning_rate=configs.get("learning_rate", base_config.learning_rate),
            decay_lambda=configs.get("decay_lambda", base_config.decay_lambda),
            weight_max=configs.get("weight_max", base_config.weight_max),
            top_m_edges=configs.get("top_m_edges", base_config.top_m_edges),
            # Activation Spreading
            spread_hops=configs.get("spread_hops", base_config.spread_hops),
            spread_decay=configs.get("spread_decay", base_config.spread_decay),
            spread_threshold=configs.get("spread_threshold", base_config.spread_threshold),
            # Scoring Weights
            alpha=configs.get("alpha", base_config.alpha),
            beta=configs.get("beta", base_config.beta),
            gamma=configs.get("gamma", base_config.gamma),
            delta=configs.get("delta", base_config.delta),
            epsilon=configs.get("epsilon", base_config.epsilon),
            zeta=configs.get("zeta", base_config.zeta),
            # Temporal
            recency_tau_days=configs.get("recency_tau_days", base_config.recency_tau_days),
            importance_ema_alpha=configs.get(
                "importance_ema_alpha", base_config.importance_ema_alpha
            ),
            # Co-Activation (env-only, not in DB)
            track_co_activation=base_config.track_co_activation,
            co_activation_window=configs.get(
                "co_activation_window", base_config.co_activation_window
            ),
            min_co_activation_count=configs.get(
                "min_co_activation_count", base_config.min_co_activation_count
            ),
            min_similarity_for_edge=configs.get(
                "min_similarity_for_edge", base_config.min_similarity_for_edge
            ),
            max_assoc_score=configs.get("max_assoc_score", base_config.max_assoc_score),
            top_k_coactivation=configs.get("top_k_coactivation", base_config.top_k_coactivation),
            # Forgetting/Decay
            enable_decay=base_config.enable_decay,
            decay_background_interval=configs.get(
                "decay_background_interval", base_config.decay_background_interval
            ),
            decay_rate=configs.get("decay_rate", base_config.decay_rate),
            hebbian_decay_half_life_days=configs.get(
                "hebbian_decay_half_life_days", base_config.hebbian_decay_half_life_days
            ),
            prune_threshold=configs.get("prune_threshold", base_config.prune_threshold),
            # Consolidation
            consolidation_use_count_min=configs.get(
                "consolidation_use_count_min", base_config.consolidation_use_count_min
            ),
            consolidation_importance_min=configs.get(
                "consolidation_importance_min", base_config.consolidation_importance_min
            ),
            consolidation_diversity_min=configs.get(
                "consolidation_diversity_min", base_config.consolidation_diversity_min
            ),
            # Privacy & Security
            enable_trust_modulation=base_config.enable_trust_modulation,  # Feature flag (env-only)
            gradient_clipping=configs.get("gradient_clipping", base_config.gradient_clipping),
            # Performance
            batch_update_size=configs.get("batch_update_size", base_config.batch_update_size),
            async_update_delay_ms=configs.get(
                "async_update_delay_ms", base_config.async_update_delay_ms
            ),
            max_candidates_k=configs.get("max_candidates_k", base_config.max_candidates_k),
            # k-NN Cold-Start Seeding (Issue #221, #406) — DB-overridable
            knn_seed_enabled=configs.get("knn_seed_enabled", base_config.knn_seed_enabled),
            knn_seed_k=configs.get("knn_seed_k", base_config.knn_seed_k),
            # A DB row for knn_seed_min_similarity is an operator override that
            # wins over calibration (D6). Post-#406 migration deletes rows with
            # value=0.4, so only genuinely-tuned values survive here; absent
            # row → base_config default (None) → calibration path.
            knn_seed_min_similarity=configs.get(
                "knn_seed_min_similarity", base_config.knn_seed_min_similarity
            ),
            knn_seed_min_percentile=configs.get(
                "knn_seed_min_percentile", base_config.knn_seed_min_percentile
            ),
            knn_seed_min_similarity_floor=configs.get(
                "knn_seed_min_similarity_floor", base_config.knn_seed_min_similarity_floor
            ),
            calibration_ttl_days=configs.get(
                "calibration_ttl_days", base_config.calibration_ttl_days
            ),
            knn_seed_weight=configs.get("knn_seed_weight", base_config.knn_seed_weight),
            # Tag Co-Occurrence Cold-Start Seeding (Issue #223) — DB-overridable
            tag_cooccurrence_enabled=configs.get(
                "tag_cooccurrence_enabled", base_config.tag_cooccurrence_enabled
            ),
            tag_cooccurrence_min_shared=configs.get(
                "tag_cooccurrence_min_shared", base_config.tag_cooccurrence_min_shared
            ),
            tag_cooccurrence_max_per_remember=configs.get(
                "tag_cooccurrence_max_per_remember",
                base_config.tag_cooccurrence_max_per_remember,
            ),
            tag_cooccurrence_hub_threshold=configs.get(
                "tag_cooccurrence_hub_threshold",
                base_config.tag_cooccurrence_hub_threshold,
            ),
            tag_cooccurrence_max_degree_per_node=configs.get(
                "tag_cooccurrence_max_degree_per_node",
                base_config.tag_cooccurrence_max_degree_per_node,
            ),
            # Sleep Maintenance (env-only flags use base_config, DB params use configs)
            sleep_enabled=base_config.sleep_enabled,  # Feature flag (env-only)
            sleep_cron_hour=base_config.sleep_cron_hour,  # Schedule (env-only)
            sleep_cron_minute=base_config.sleep_cron_minute,  # Schedule (env-only)
            sleep_llm_provider=configs.get("sleep_llm_provider", base_config.sleep_llm_provider),
            sleep_llm_model=configs.get("sleep_llm_model", base_config.sleep_llm_model),
            sleep_max_memories_per_run=configs.get(
                "sleep_max_memories_per_run", base_config.sleep_max_memories_per_run
            ),
            sleep_max_llm_calls_per_run=configs.get(
                "sleep_max_llm_calls_per_run", base_config.sleep_max_llm_calls_per_run
            ),
            sleep_dedup_enabled=configs.get("sleep_dedup_enabled", base_config.sleep_dedup_enabled),
            sleep_dedup_similarity_threshold=configs.get(
                "sleep_dedup_similarity_threshold",
                base_config.sleep_dedup_similarity_threshold,
            ),
            sleep_edge_discovery_enabled=configs.get(
                "sleep_edge_discovery_enabled", base_config.sleep_edge_discovery_enabled
            ),
            sleep_edge_discovery_sample_size=configs.get(
                "sleep_edge_discovery_sample_size",
                base_config.sleep_edge_discovery_sample_size,
            ),
            sleep_importance_reeval_enabled=configs.get(
                "sleep_importance_reeval_enabled",
                base_config.sleep_importance_reeval_enabled,
            ),
        )

        # Store in cache
        cls._db_cache[cache_key] = config
        cls._db_cache_time = time.time()

        return config

    @classmethod
    def invalidate_cache(cls) -> None:
        """Invalidate the config cache.

        Call this when config values are updated via admin UI.
        """
        cls._db_cache.clear()
        cls._db_cache_time = 0.0
