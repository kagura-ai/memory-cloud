"""Write-path tests for the WHERE axis (#1331): MemoryService._apply_location.

Mirrors test_remember_time_memory.py's mocked-DB pattern — the location gate
is pure validation that runs before any DB write. The orthogonal-gate
contract (fires on details.location presence, any type) and the
caller-supplied-details-only rule (legacy rows must not 422 on untouched
updates) are the load-bearing pins.
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


def _req(details, mtype="note"):
    return RememberRequest(
        summary="現場訪問の記録（テスト用サマリー）",
        content="訪問メモ本文",
        details=details,
        type=mtype,
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
async def test_remember_normalizes_location(service):
    req = _req({"location": {"lat": 35.68123456789, "lon": 139.7671, "label": "office"}})
    result = await _call(service, req)
    assert result.memory_id is not None
    assert req.details["location"]["lat"] == 35.6812346  # 7-decimal write-back
    assert req.details["location"]["lon"] == 139.7671


@pytest.mark.asyncio
async def test_remember_location_gate_is_type_agnostic(service):
    # Orthogonal attribute: a troubleshooting memory may carry a place.
    req = _req({"location": {"lat": 1.0, "lon": 2.0}}, mtype="troubleshooting")
    result = await _call(service, req)
    assert result.memory_id is not None


@pytest.mark.asyncio
async def test_remember_invalid_location_raises(service):
    req = _req({"location": {"lat": "35.6", "lon": 139.7}})
    with pytest.raises(ValueError, match="invalid details.location"):
        await _call(service, req)


@pytest.mark.asyncio
async def test_remember_without_location_passthrough(service):
    req = _req({"other": "data"})
    result = await _call(service, req)
    assert result.memory_id is not None
    assert "location" not in req.details


# _apply_location is the centralized helper used by remember(),
# _update_in_place(), and patch_memory() (the latter two apply it only to
# caller-supplied details) — the pure contract is pinned directly.


def test_apply_location_passthrough_without_key():
    details = {"foo": "bar"}
    assert MemoryService._apply_location(details) is details


def test_apply_location_none_details():
    assert MemoryService._apply_location(None) is None


def test_apply_location_normalizes():
    out = MemoryService._apply_location({"location": {"lat": 1.23456789, "lon": -3}})
    assert out["location"] == {"lat": 1.2345679, "lon": -3.0}


def test_apply_location_maps_to_value_error():
    with pytest.raises(ValueError, match="invalid details.location"):
        MemoryService._apply_location({"location": {"lat": 91, "lon": 0}})
