"""Shared database utilities for CLI tools."""

from config.database import get_database_url


def get_sync_database_url() -> str:
    """Get synchronous database URL (psycopg2) for CLI tools."""
    url = get_database_url()
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg2://")
