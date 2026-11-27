"""#3033: reference() says when a memory is not searchable yet.

The state comes from the row's embedding columns. It rides on
``ReferenceResponse.index_state``, which is excluded from serialization, so the
REST body (and the OpenAPI document) do not change; the MCP handler turns it
into the ``search_index`` key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from config.constants import MAX_EMBEDDING_RETRIES
from models.schemas import ReferenceResponse
from services.memory_service import MemoryService, embedding_index_state


@pytest.mark.parametrize(
    ("status", "retries", "suppressed", "expected"),
    [
        ("success", 0, False, None),
        ("success", MAX_EMBEDDING_RETRIES, True, None),
        ("pending", 0, False, "indexing"),
        ("processing", 1, False, "indexing"),
        ("failed", 0, False, "retrying"),
        ("failed", MAX_EMBEDDING_RETRIES - 1, False, "retrying"),
        ("failed", MAX_EMBEDDING_RETRIES, False, "failed"),
        ("failed", MAX_EMBEDDING_RETRIES + 2, False, "failed"),
        ("failed", 0, True, "failed"),
        ("pending", 0, True, "failed"),
        ("processing", 0, True, "failed"),
    ],
)
def test_index_state_from_the_embedding_columns(status, retries, suppressed, expected):
    suppressed_at = datetime(2026, 1, 1, tzinfo=UTC) if suppressed else None
    assert embedding_index_state(status, retries, suppressed_at) == expected


def test_an_unknown_status_reads_as_searchable():
    """A value outside the check constraint never invents a state."""
    assert embedding_index_state("weird", 0, None) is None


def test_a_missing_retry_count_counts_as_zero():
    assert embedding_index_state("failed", None, None) == "retrying"


def _response(**extra) -> ReferenceResponse:
    now = datetime(2026, 6, 1, 12, 0, 0)
    return ReferenceResponse(
        memory_id=uuid4(),
        summary="s",
        context_summary=None,
        content="c",
        details=None,
        type="note",
        scope="working",
        importance=0.5,
        tags=[],
        context=None,
        created_at=now,
        updated_at=now,
        version=1,
        client="api",
        **extra,
    )


def test_index_state_is_not_serialized_for_rest():
    response = _response(index_state="indexing")
    assert response.index_state == "indexing"
    assert "index_state" not in response.model_dump()
    assert "index_state" not in response.model_dump(mode="json")
    assert (
        "index_state" not in ReferenceResponse.model_json_schema(mode="serialization")["properties"]
    )


def _memory(status: str, retries: int = 0, suppressed_at=None):
    now = datetime(2026, 6, 1, 12, 0, 0)
    return SimpleNamespace(
        id=uuid4(),
        user_id="u",
        summary="s",
        context_summary=None,
        content="c",
        details=None,
        type="note",
        scope="working",
        importance=0.5,
        tags=[],
        context=None,
        created_at=now,
        updated_at=now,
        version=2,
        client="api",
        source_uri=None,
        source_type=None,
        workspace_id=uuid4(),
        context_id=uuid4(),
        embedding_status=status,
        embedding_retry_count=retries,
        embedding_suppressed_at=suppressed_at,
    )


def _service(memory) -> MemoryService:
    service = MemoryService(MagicMock())
    service._load_readable_memory = AsyncMock(return_value=memory)  # type: ignore[method-assign]
    service.memory_repo.update_access_stats = AsyncMock()
    service.db.commit = AsyncMock()
    service._audit_reference_read = AsyncMock()  # type: ignore[method-assign]
    service._fetch_declared_link_refs = AsyncMock(  # type: ignore[method-assign]
        return_value=([], False, [], False)
    )
    service._supersede_candidate_for = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return service


@pytest.mark.parametrize(
    ("status", "retries", "expected"),
    [
        ("success", 0, None),
        ("pending", 0, "indexing"),
        ("processing", 0, "indexing"),
        ("failed", 0, "retrying"),
        ("failed", MAX_EMBEDDING_RETRIES, "failed"),
    ],
)
async def test_reference_carries_the_rows_index_state(status, retries, expected):
    memory = _memory(status, retries)
    response = await _service(memory).reference(memory.id, "u")
    assert response.index_state == expected


async def test_index_state_is_read_with_the_row_before_the_access_commit():
    """Read with the row, like ``version``: a status the commit's refresh
    would show is not mixed into this read."""
    memory = _memory("pending")
    service = _service(memory)

    async def _commit():
        memory.embedding_status = "success"

    service.db.commit = AsyncMock(side_effect=_commit)
    response = await service.reference(memory.id, "u")
    assert response.index_state == "indexing"
