"""Tests for the delivery_mode + pin-on-write write path of remember() (#886).

Mirrors the mocked-DB pattern in test_remember_time_memory.py: memory_repo.create
is mocked, so we inspect the Memory entity handed to it to assert delivery_mode
and the pin-on-write scope transition — no real DB needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import RememberRequest
from services.memory_service import MemoryService


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def service(mock_db):
    svc = MemoryService(mock_db)
    mock_context = MagicMock()
    mock_context.id = uuid4()
    mock_context.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(
        return_value=(mock_context, str(mock_context.workspace_id), str(mock_context.id))
    )
    svc.memory_repo = MagicMock()
    svc.memory_repo.create = AsyncMock()
    svc._create_declared_links = AsyncMock()
    svc._mock_context = mock_context
    return svc


def _req(**kwargs):
    base = {"summary": "agent goal: ship #886", "content": "deliver delivery_mode", "type": "note"}
    base.update(kwargs)
    return RememberRequest(**base)


async def _call(service, req):
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService"),
    ):
        return await service.remember(
            req,
            user_id="test_user",
            client="test",
            current_context_id=service._mock_context.id,
            current_workspace_id=None,
        )


def _created_memory(service):
    return service.memory_repo.create.call_args.args[0]


@pytest.mark.asyncio
async def test_default_delivery_mode_is_on_recall_and_working(service):
    """No delivery_mode supplied → on_recall + working scope (legacy behavior)."""
    result = await _call(service, _req())
    mem = _created_memory(service)
    assert mem.delivery_mode == "on_recall"
    assert mem.scope == "working"
    assert mem.promoted_at is None
    assert result.scope == "working"


@pytest.mark.asyncio
async def test_explicit_on_recall_stays_working(service):
    result = await _call(service, _req(delivery_mode="on_recall"))
    mem = _created_memory(service)
    assert mem.delivery_mode == "on_recall"
    assert mem.scope == "working"
    assert result.scope == "working"


@pytest.mark.asyncio
async def test_always_pins_to_persistent_on_write(service):
    """delivery_mode='always' pins to persistent immediately (no sleep wait)."""
    result = await _call(service, _req(delivery_mode="always"))
    mem = _created_memory(service)
    assert mem.delivery_mode == "always"
    # Pin-on-write: persistent immediately, with promoted_at stamped (mirrors
    # promote_to_persistent), so consolidation (scope='working' only) skips it.
    assert mem.scope == "persistent"
    assert mem.promoted_at is not None
    assert result.scope == "persistent"


def test_remember_request_delivery_mode_defaults_to_on_recall():
    assert RememberRequest(summary="x" * 10, content="c", type="note").delivery_mode == "on_recall"


def test_remember_request_rejects_unknown_delivery_mode():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        RememberRequest(summary="x" * 10, content="c", type="note", delivery_mode="eventually")
