"""Shared fixtures and helpers for backend/tests/tasks/ test files."""

from __future__ import annotations

# Re-export fixtures from sibling test directories so pytest can discover
# them in this subdirectory tree.  F401 suppressed because the import
# side-effect (fixture registration) is the whole point.
from tests.neural.conftest import sample_memory_pair  # noqa: F401
from tests.repositories.conftest import two_edges_one_hebbian_one_semantic  # noqa: F401


def mock_get_db_factory(mock_db):
    """Build an async generator that yields ``mock_db`` once.

    Used to patch ``tasks.<task_module>.get_db`` in task tests so the
    ``async for db in get_db():`` pattern receives a controlled mock
    session without touching a real database.
    """

    async def get_db():
        yield mock_db

    return get_db
