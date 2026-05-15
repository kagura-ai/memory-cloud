"""Shared fixtures and helpers for backend/tests/tasks/ test files."""

from __future__ import annotations


def mock_get_db_factory(mock_db):
    """Build an async generator that yields ``mock_db`` once.

    Used to patch ``tasks.<task_module>.get_db`` in task tests so the
    ``async for db in get_db():`` pattern receives a controlled mock
    session without touching a real database.
    """

    async def get_db():
        yield mock_db

    return get_db
