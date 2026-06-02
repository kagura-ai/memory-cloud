"""Tests for the Time Memory (type="time") branch of MemoryService.remember (Issue #877).

Mirrors the mocked-DB pattern in test_async_remember.py: the time-branch is pure
validation + window derivation that runs before any DB write, so a MagicMock db +
mocked context isolation is sufficient and avoids a real seeded context.
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
    svc._mock_context = mock_context
    return svc


def _req(details):
    return RememberRequest(
        summary="運動会の準備を確認する",
        content="運動会前に去年の反省を見直す",
        details=details,
        type="time",
    )


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


@pytest.mark.asyncio
async def test_time_memory_derives_window_into_details(service):
    """A valid time memory has its details.trigger normalized with from/until."""
    req = _req({"trigger": {"year": 2026, "month": 7}})
    result = await _call(service, req)
    assert result.memory_id is not None
    # The request's details were normalized in place before persistence.
    assert req.details["trigger"]["from"] == "2026-07-01T00:00:00"
    assert req.details["trigger"]["until"] == "2026-07-31T23:59:59"


@pytest.mark.asyncio
async def test_time_memory_missing_trigger_raises(service):
    req = _req({})  # no trigger
    with pytest.raises(ValueError, match="details.trigger"):
        await _call(service, req)


@pytest.mark.asyncio
async def test_time_memory_invalid_trigger_raises(service):
    req = _req({"trigger": {"year": 2026, "month": 13}})
    with pytest.raises(ValueError, match="invalid details.trigger"):
        await _call(service, req)
