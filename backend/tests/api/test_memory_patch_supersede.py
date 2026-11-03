"""``PATCH /api/v1/memory/{id}`` with ``accept_supersede_candidate`` /
``dismiss_supersede_candidate`` (#2600).

The web dialog accepted a suggestion through ``POST /graph/edges``, which keeps
the edge under the caller; ``update_memory(accept_supersede_candidate=true)``
keeps it under the carrier's author, where supersede detection looks it up.
The PATCH flag runs the update path's accept, so both writers agree. The route
runs the real ``MemoryService.patch_memory`` with its I/O edges stubbed; the
Postgres behaviour is in
``tests/integration/test_2600_patch_accept_supersede_db.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.edge_service import EdgeWriteResult
from services.memory_service import MemoryService
from services.supersede_acceptance import PairOwnership
from services.supersede_dismissal import dismissed_entry
from utils.exceptions import AuthorizationError

CALLER = "u1"
# The carrier's author: an account linked to the caller in a private context.
AUTHOR = "u1-linked"
TARGET_ID = uuid4()
STORED_AT = datetime(2026, 11, 1, 10, 0, 0)


def _candidate() -> dict[str, Any]:
    return {"memory_id": str(TARGET_ID), "similarity": 0.93, "detected_at": "2026-11-01T00:00:00Z"}


def _stored_row(**overrides):
    memory = MagicMock()
    memory.id = uuid4()
    memory.point_id = memory.id
    memory.user_id = AUTHOR
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "stored summary text"
    memory.context_summary = None
    memory.content = "stored content"
    memory.details = None
    memory.type = "fact"
    memory.importance = 0.5
    memory.tags = []
    memory.context = None
    memory.scope = "working"
    memory.client = "mcp"
    memory.created_at = STORED_AT
    memory.updated_at = STORED_AT
    memory.deleted_at = None
    memory.embedding_status = "success"
    memory.source_uri = None
    memory.source_type = None
    memory.version = 2
    memory.supersede_candidate = _candidate()
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


def _pair(**overrides) -> PairOwnership:
    values: dict[str, Any] = {
        "carrier_own": True,
        "target_own": True,
        "carrier_created_at": STORED_AT,
        "target_created_at": datetime(2026, 10, 1),
        "target_exists": True,
    }
    values.update(overrides)
    return PairOwnership(**values)


def _client(memory, *, resolved: dict | None = None):
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
    service._resolve_supersede_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value={memory.id: SimpleNamespace(memory_id=TARGET_ID)}
        if resolved is None
        else resolved
    )

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/memory")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: {"user_id": CALLER}
    app.dependency_overrides[get_memory_service] = lambda: service
    return TestClient(app), service, db


def _written(memory) -> AsyncMock:
    """``create_declared_edge`` as it succeeds: the edge, and the suggestion cleared."""

    async def write(db, **kwargs):
        memory.supersede_candidate = None
        return EdgeWriteResult(operation="created", edge={"edge_type": kwargs["edge_type"]})

    return AsyncMock(side_effect=write)


def _patch(memory, body, *, edge_writer=None, pair=None, resolved=None):
    client, service, db = _client(memory, resolved=resolved)
    writer = edge_writer if edge_writer is not None else _written(memory)
    audit = AsyncMock()
    with (
        client,
        patch("services.memory_access_event_writer.emit_memory_access_event", new=audit),
        patch("services.edge_service.create_declared_edge", new=writer),
        patch(
            "services.supersede_acceptance.pair_ownership",
            new=AsyncMock(return_value=pair if pair is not None else _pair()),
        ),
    ):
        response = client.patch(f"/api/v1/memory/{memory.id}", json=body)
    return SimpleNamespace(response=response, service=service, db=db, writer=writer, audit=audit)


def test_an_accept_writes_the_edge_under_the_carriers_author():
    memory = _stored_row()

    result = _patch(memory, {"accept_supersede_candidate": True})

    assert result.response.status_code == 200, result.response.text
    kwargs = result.writer.await_args.kwargs
    assert kwargs["user_id"] == AUTHOR  # not the caller: detection looks it up there
    assert kwargs["source_id"] == memory.id
    assert kwargs["target_id"] == TARGET_ID
    assert kwargs["edge_type"] == "supersedes"
    assert kwargs["overwrite"] is False
    result.db.commit.assert_awaited()
    body = result.response.json()
    assert body["memory_id"] == str(memory.id)
    assert body["supersede_candidate"] is None
    # A graph action, not a content edit: no new version, no fresher timestamp.
    assert body["version"] == 2
    assert memory.updated_at == STORED_AT
    assert result.audit.await_args.kwargs["operation"] == "update"
    assert result.audit.await_args.kwargs["user_id"] == CALLER


def test_an_accept_may_carry_expected_version():
    memory = _stored_row()

    result = _patch(memory, {"accept_supersede_candidate": True, "expected_version": 2})

    assert result.response.status_code == 200, result.response.text
    result.writer.assert_awaited_once()


def test_a_stale_expected_version_is_a_409_before_the_accept():
    memory = _stored_row()

    result = _patch(memory, {"accept_supersede_candidate": True, "expected_version": 1})

    assert result.response.status_code == 409
    assert result.response.json()["details"]["reason"] == "version_conflict"
    result.writer.assert_not_awaited()
    result.db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("resolved", "pair", "message"),
    [
        ({}, None, "no supersede_candidate"),
        (None, _pair(carrier_own=False), "on your own memory"),
        (None, _pair(target_own=False), "another member's memory"),
        (None, _pair(target_created_at=datetime(2026, 11, 2)), "written after it"),
    ],
    ids=["no_live_suggestion", "foreign_carrier", "foreign_target", "newer_candidate"],
)
def test_a_refused_accept_is_a_422_and_writes_nothing(resolved, pair, message):
    memory = _stored_row()

    result = _patch(memory, {"accept_supersede_candidate": True}, pair=pair, resolved=resolved)

    assert result.response.status_code == 422, result.response.text
    assert message in result.response.json()["detail"]
    result.writer.assert_not_awaited()
    result.db.commit.assert_not_awaited()
    assert memory.supersede_candidate == _candidate()


def test_a_different_declared_edge_is_a_409_with_the_existing_edge():
    memory = _stored_row()
    existing = {"edge_type": "related_to", "weight": 1.0, "confidence": 1.0}
    conflict = AsyncMock(
        return_value=EdgeWriteResult(operation="conflict", edge=None, existing=existing)
    )

    result = _patch(memory, {"accept_supersede_candidate": True}, edge_writer=conflict)

    assert result.response.status_code == 409, result.response.text
    body = result.response.json()
    assert body["error"] == "RES-002"
    assert body["details"] == {"reason": "edge_exists", "existing_edge": existing}
    result.db.commit.assert_not_awaited()
    assert memory.supersede_candidate == _candidate()


def test_an_endpoint_lost_before_the_write_is_a_403():
    """The edge writer's final ownership check (#2313): the uniform deny."""
    memory = _stored_row()
    denied = AsyncMock(side_effect=AuthorizationError("A supersedes edge requires ..."))

    result = _patch(memory, {"accept_supersede_candidate": True}, edge_writer=denied)

    assert result.response.status_code == 403
    assert result.response.json()["details"] == {}
    result.db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"accept_supersede_candidate": True, "importance": 0.9},
        {"accept_supersede_candidate": True, "details": None},
        {"accept_supersede_candidate": True, "dismiss_supersede_candidate": True},
        {"accept_supersede_candidate": "true"},
        {"accept_supersede_candidate": False},
    ],
    ids=["with_an_edit", "with_null_details", "with_dismiss", "string_flag", "false_flag_alone"],
)
def test_a_refused_body_is_a_422_before_the_service(body):
    memory = _stored_row()

    result = _patch(memory, body)

    assert result.response.status_code == 422, result.response.text
    result.service._lock_for_edit.assert_not_awaited()
    result.writer.assert_not_awaited()
    result.db.commit.assert_not_awaited()


def test_a_dismissal_tombstones_the_suggestion_and_keeps_the_timestamp():
    memory = _stored_row()

    result = _patch(memory, {"dismiss_supersede_candidate": True})

    assert result.response.status_code == 200, result.response.text
    entry = dismissed_entry(memory.supersede_candidate)
    assert entry is not None and entry["memory_id"] == str(TARGET_ID)
    # A dismissal is not a content edit (#1504).
    assert memory.updated_at == STORED_AT
    assert result.response.json()["version"] == 2
    result.writer.assert_not_awaited()
    result.db.commit.assert_awaited()


def test_a_dismissal_with_an_edit_applies_both():
    memory = _stored_row()

    result = _patch(memory, {"dismiss_supersede_candidate": True, "importance": 0.9})

    assert result.response.status_code == 200, result.response.text
    assert dismissed_entry(memory.supersede_candidate) is not None
    assert memory.importance == 0.9
    assert memory.updated_at != STORED_AT
    assert result.response.json()["version"] == 3


def test_a_dismissal_without_a_live_suggestion_is_a_no_op_not_an_error():
    memory = _stored_row(supersede_candidate=None)

    result = _patch(memory, {"dismiss_supersede_candidate": True})

    assert result.response.status_code == 200, result.response.text
    assert memory.supersede_candidate is None
    assert memory.updated_at == STORED_AT
