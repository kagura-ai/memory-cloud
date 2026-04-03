"""Utility functions for neural memory system.

This module provides shared helper functions used across neural memory components,
reducing code duplication and improving testability.
"""

from datetime import UTC, datetime

import numpy as np


def get_current_utc_time() -> datetime:
    """Get current UTC time (centralized for consistency and testing).

    Using datetime.now(timezone.utc) instead of deprecated datetime.utcnow().

    Returns:
        Current UTC datetime (timezone-aware)
    """
    return datetime.now(UTC)


def cosine_similarity(emb1: list[float], emb2: list[float]) -> float:
    """Calculate cosine similarity between two embeddings.

    Args:
        emb1: First embedding vector
        emb2: Second embedding vector

    Returns:
        Cosine similarity clamped to [0, 1]
    """
    v1 = np.array(emb1)
    v2 = np.array(emb2)

    norm_v1 = np.linalg.norm(v1)
    norm_v2 = np.linalg.norm(v2)

    if norm_v1 == 0 or norm_v2 == 0:
        return 0.0

    sim = float(np.dot(v1, v2) / (norm_v1 * norm_v2))
    return max(0.0, sim)


# Constants for magic numbers used across the system

# Scoring constants
LOG_FREQUENCY_REFERENCE_COUNT = 100  # Reference count for log-scaled frequency
IMPORTANCE_STORED_WEIGHT = 0.7  # Weight for stored importance in importance score
IMPORTANCE_FREQUENCY_WEIGHT = 0.3  # Weight for use frequency in importance score

# Time constants
SECONDS_PER_DAY = 86400  # Seconds in a day (for age calculations)

# Distance/Similarity conversion
COSINE_SIM_NORMALIZATION_OFFSET = 1.0  # Offset for cosine similarity normalization
DISTANCE_TO_SIMILARITY_DIVISOR = 2.0  # Divisor for distance-to-similarity conversion
