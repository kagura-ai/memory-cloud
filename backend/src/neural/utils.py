"""Utility functions for neural memory system.

This module provides shared helper functions used across neural memory components,
reducing code duplication and improving testability.
"""

from datetime import UTC, datetime


def get_current_utc_time() -> datetime:
    """Get current UTC time (centralized for consistency and testing).

    Using datetime.now(timezone.utc) instead of deprecated datetime.utcnow().

    Returns:
        Current UTC datetime (timezone-aware)
    """
    return datetime.now(UTC)


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
