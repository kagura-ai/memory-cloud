"""Shared database utilities for CLI tools."""

# The CLI tools import the sync-URL builder from here; the normalization itself
# lives next to ``get_database_url`` so the OAuth2 engine shares it (#1695).
from config.database import get_sync_database_url

__all__ = ["get_sync_database_url"]
