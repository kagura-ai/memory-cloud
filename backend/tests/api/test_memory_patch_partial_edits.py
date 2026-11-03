"""``PATCH /api/v1/memory/{id}`` with add_tags / remove_tags / details_patch (#2585).

The route runs the real ``MemoryService.patch_memory`` with only its I/O
edges stubbed, so the response shows what the merge made of the stored row; a
conflict, an explicit null, an added tag over 64 characters or a result over
100 tags is a 422 and writes nothing.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.memory_service import MemoryService


def _stored_row(**overrides):
    memory = MagicMock()
    memory.id = uuid4()
    memory.point_id = memory.id
    memory.user_id = "u1"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "stored summary text"
    memory.context_summary = None
    memory.content = "stored content"
    memory.details = {"file": "a.py", "pr": 1}
    memory.type = "tech-debt"
    memory.importance = 0.45
    memory.tags = ["tech-debt", "status:open", "area:api"]
    memory.context = None
    memory.scope = "working"
    memory.client = "mcp"
    memory.created_at = datetime(2026, 11, 1, 10, 0, 0)
    memory.updated_at = datetime(2026, 11, 1, 10, 0, 0)
    memory.deleted_at = None
    memory.embedding_status = "success"
    memory.source_uri = None
    memory.source_type = None
    memory.version = 2
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


def _client(memory):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.main import memory_cloud_exception_handler
    from api.routes.memory import get_memory_service, router
    from auth.dependencies import get_user_from_api_key_or_session
    from utils.exceptions import MemoryCloudException

    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    service = MemoryService(db)
    service._patch_load_authorized = AsyncMock(return_value=memory)  # type: ignore[method-assign]
    service._lock_for_edit = AsyncMock(return_value=memory)  # type: ignore[method-assign]
    service._ensure_capacity = AsyncMock()  # type: ignore[method-assign]
    service._require_guardrail_author = AsyncMock()  # type: ignore[method-assign]
    service._sync_metadata_payload = AsyncMock()  # type: ignore[method-assign]
    service._fetch_declared_link_refs = AsyncMock(return_value=([], False, [], False))  # type: ignore[method-assign]
    service._supersede_candidate_for = AsyncMock(return_value=None)  # type: ignore[method-assign]

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/memory")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: {"user_id": "u1"}
    app.dependency_overrides[get_memory_service] = lambda: service
    return TestClient(app), service, db


def _patch(memory, body):
    client, service, db = _client(memory)
    with (
        client,
        patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
    ):
        response = client.patch(f"/api/v1/memory/{memory.id}", json=body)
    return response, service, db


def test_a_status_flip_edits_only_the_tags():
    memory = _stored_row()
    response, _service, db = _patch(
        memory, {"remove_tags": ["status:open"], "add_tags": ["status:done"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tags"] == ["tech-debt", "area:api", "status:done"]
    assert body["details"] == {"file": "a.py", "pr": 1}
    assert body["version"] == 3
    db.commit.assert_awaited()


def test_details_patch_merges_and_null_deletes():
    memory = _stored_row()
    response, _service, _db = _patch(memory, {"details_patch": {"pr": None, "issue": 2585}})
    assert response.status_code == 200, response.text
    assert response.json()["details"] == {"file": "a.py", "issue": 2585}
    assert response.json()["tags"] == ["tech-debt", "status:open", "area:api"]


def test_details_patch_on_a_null_column_starts_from_an_empty_object():
    memory = _stored_row(details=None)
    response, _service, _db = _patch(memory, {"details_patch": {"k": 1}})
    assert response.status_code == 200, response.text
    assert response.json()["details"] == {"k": 1}


@pytest.mark.parametrize(
    "body",
    [
        {"tags": ["a"], "add_tags": ["b"]},
        {"tags": ["a"], "remove_tags": ["b"]},
        {"details": {"a": 1}, "details_patch": {"b": 2}},
        {"details": None, "details_patch": {"b": 2}},
        {"add_tags": None},
        {"remove_tags": None},
        {"details_patch": None},
        {"add_tags": ["x" * 65]},
    ],
    ids=[
        "tags+add_tags",
        "tags+remove_tags",
        "details+details_patch",
        "null-details+details_patch",
        "null-add_tags",
        "null-remove_tags",
        "null-details_patch",
        "add_tags-too-long",
    ],
)
def test_a_refused_body_is_a_422_before_the_service(body):
    memory = _stored_row()
    response, service, db = _patch(memory, body)
    assert response.status_code == 422, response.text
    service._lock_for_edit.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_a_result_over_the_patch_caps_is_a_422_and_writes_nothing():
    stored = [f"t{i}" for i in range(100)]
    memory = _stored_row(tags=list(stored))
    response, _service, db = _patch(memory, {"add_tags": ["one-more"]})
    assert response.status_code == 422
    assert "at most 100 tags (got 101)" in response.json()["detail"]
    assert memory.tags == stored
    db.commit.assert_not_awaited()


def test_a_stored_tag_over_64_characters_survives_a_status_flip():
    """A memo's ``file:`` tag written through MCP (100-character cap) is longer
    than PATCH allows; the 64-character cap applies to the added tags only."""
    long_tag = "file:backend/tests/integration/test_consolidation_point_deletes_db.py"
    assert len(long_tag) > 64
    memory = _stored_row(tags=["tech-debt", long_tag, "status:open"])
    response, _service, db = _patch(
        memory, {"remove_tags": ["status:open"], "add_tags": ["status:done"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tags"] == ["tech-debt", long_tag, "status:done"]
    db.commit.assert_awaited()


def test_an_added_tag_over_64_characters_is_still_a_422():
    memory = _stored_row(tags=["tech-debt", "x" * 65])
    response, service, db = _patch(memory, {"add_tags": ["y" * 65]})
    assert response.status_code == 422, response.text
    service._lock_for_edit.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_an_invalid_reserved_key_through_a_patch_is_a_422():
    memory = _stored_row()
    response, _service, db = _patch(memory, {"details_patch": {"location": {"lat": 999, "lon": 0}}})
    assert response.status_code == 422
    assert "invalid details.location" in response.json()["detail"]
    assert memory.details == {"file": "a.py", "pr": 1}
    db.commit.assert_not_awaited()
