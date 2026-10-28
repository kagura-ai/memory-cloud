"""Shared fixtures for the REST route unit tests."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import true


@pytest.fixture
def reach_everything() -> Iterator[AsyncMock]:
    """Stub the #2325 reach read of the context-less memory views.

    Without ``context_id``, ``GET /memory/list`` also keeps to the contexts
    the caller reaches (one membership read). Mocked-DB tests of the other
    filters stub that read to reach everything; the real rule runs against
    PostgreSQL in ``tests/integration/test_2325_context_less_views_reach_db.py``.
    """
    stub = AsyncMock(return_value=true())
    with patch("api.routes.memory.reachable_memory_filter", stub):
        yield stub
