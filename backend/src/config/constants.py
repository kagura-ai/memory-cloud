"""Global constants for Kagura Memory Cloud.

Issue #273: Extract magic numbers to centralized configuration for maintainability.
All constants are grouped by category with clear documentation.
"""

# ============================================================================
# Application Version (single source of truth for runtime)
# ============================================================================

APP_VERSION = "0.30.0"

# ============================================================================
# Memory Content Limits
# ============================================================================

# Maximum size for a single memory (1MB)
# Rationale: Without per-workspace storage quotas, we enforce per-memory limits
#            to prevent DoS attacks and ensure reasonable memory sizes.
#            1MB is sufficient for most use cases while preventing abuse.
MAX_CONTENT_SIZE = 1_000_000  # bytes (1MB)

# ============================================================================
# Neural Memory / Graph Parameters
# ============================================================================

# Activation spread decay factor (0.0 - 1.0)
# Controls how much activation energy decreases as it spreads through the graph.
# Formula: new_weight = current_weight * edge_weight * SPREAD_DECAY
# - 0.6: Moderate decay (default) - balances exploration vs relevance
# - Higher (0.8-0.9): More exploration, weaker decay
# - Lower (0.3-0.5): Stronger decay, more focused results
SPREAD_DECAY = 0.6

# Maximum graph traversal depth (hops)
# Prevents infinite loops and memory exhaustion in graph traversal.
# - Max recommended: 10 hops (covers most real-world use cases)
# - Typical usage: 1-3 hops for explore() API
MAX_GRAPH_DEPTH = 10

# Minimum edge weight threshold for pruning weak connections
# Edges below this weight are considered noise and can be pruned.
MIN_EDGE_WEIGHT_THRESHOLD = 0.01

# Hebbian weight decay factor for temporal forgetting
# Applied periodically to all edges to simulate memory decay.
# Formula: w_new = w_old * HEBBIAN_DECAY_FACTOR
HEBBIAN_DECAY_FACTOR = 0.95  # 5% decay per application

# Edge weight bounds [min, max]
# All edge weights are clamped to this range.
MIN_EDGE_WEIGHT = 0.0
MAX_EDGE_WEIGHT = 3.0

# ============================================================================
# Search & Retrieval Parameters
# ============================================================================

# Default number of results for recall() API
DEFAULT_RECALL_LIMIT = 5

# Maximum number of results for recall() API
MAX_RECALL_LIMIT = 100

# Default number of neighbors for explore() API
DEFAULT_EXPLORE_LIMIT = 10

# Maximum number of neighbors for explore() API
MAX_EXPLORE_LIMIT = 100

# ============================================================================
# Database & Performance
# ============================================================================

# Maximum queue size for BFS graph traversal
# Prevents memory exhaustion during large graph traversals.
MAX_BFS_QUEUE_SIZE = 10_000

# Connection pool size for database (used in database.py)
DB_POOL_SIZE = 20
DB_MAX_OVERFLOW = 10

# ============================================================================
# Migration & Validation
# ============================================================================

# Legacy migration dependency map (historical reference).
# Migrations are now managed by Alembic. See backend/alembic/.
MIGRATION_DEPENDENCIES = {
    "067": [
        "062",
        "063",
    ],  # 067 (drop collection_name) requires 062/063 (add workspace_id/context_id)
}

# ============================================================================
# Error Messages (Standardized)
# ============================================================================

# 3-level isolation error message (used across all services)
ERROR_MSG_3_LEVEL_ISOLATION = (
    "3-level isolation requires workspace_id, context_id, and user_id. "
    "Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
)

# 2-level isolation error message (workspace + context required)
ERROR_MSG_2_LEVEL_ISOLATION = (
    "2-level isolation requires workspace_id and context_id. "
    "Got workspace_id={workspace_id}, context_id={context_id}"
)

# Invalid UUID format error
ERROR_MSG_INVALID_UUID = (
    "Invalid UUID format for {field}: {value}. "
    "Expected valid UUID v4 format (e.g., '123e4567-e89b-12d3-a456-426614174000')"
)

# ============================================================================
# Qdrant Constants
# ============================================================================

# Minimum weight for Qdrant search filters
QDRANT_MIN_WEIGHT_FILTER = 0.0

# Vector dimension for embeddings (OpenAI text-embedding-3-small)
EMBEDDING_DIMENSION = 512

# Supported embedding models: model_name → (dimensions, provider)
EMBEDDING_MODEL_REGISTRY: dict[str, tuple[int, str]] = {
    # OpenAI
    "text-embedding-3-small": (512, "openai"),
    "text-embedding-3-large": (3072, "openai"),
    # Ollama (qwen3-embedding)
    "qwen3-embedding:0.6b": (1024, "ollama"),
    "qwen3-embedding:4b": (2560, "ollama"),
    "qwen3-embedding:8b": (4096, "ollama"),
    # Ollama (other)
    "nomic-embed-text": (768, "ollama"),
    "mxbai-embed-large": (1024, "ollama"),
}

# ============================================================================
# Session & Cache TTL
# ============================================================================

# Session cookie expiration (7 days)
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 604800 seconds

# Redis cache TTL for context metadata (1 hour)
CONTEXT_CACHE_TTL_SECONDS = 60 * 60  # 3600 seconds

# Redis cache TTL for workspace metadata (1 hour)
ORG_CACHE_TTL_SECONDS = 60 * 60  # 3600 seconds
