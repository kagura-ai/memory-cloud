"""Database configuration.

Database URLs are read directly from environment variables (.env.local).
This allows for simple configuration without Pydantic validation.
"""

import os


def get_database_url() -> str:
    """Get PostgreSQL database URL from environment.

    Returns:
        Database URL (default for dev if not set)

    Raises:
        ConfigurationError: If DATABASE_URL not set in production
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        # Development default
        return "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura"
    return url


def get_qdrant_url() -> str:
    """Get Qdrant server URL from environment.

    Returns:
        Qdrant URL (default for dev if not set)
    """
    url = os.getenv("QDRANT_URL")
    if not url:
        return "http://localhost:6333"
    return url


def get_redis_url() -> str:
    """Get Redis server URL from environment.

    Returns:
        Redis URL (default for dev if not set)
    """
    url = os.getenv("REDIS_URL")
    if not url:
        return "redis://localhost:6379"
    return url


# Database connection URLs
DATABASE_URL = get_database_url()
QDRANT_URL = get_qdrant_url()
REDIS_URL = get_redis_url()
