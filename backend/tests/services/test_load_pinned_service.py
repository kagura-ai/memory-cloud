"""Tests for MemoryService.load_pinned — the deterministic always-load wrapper (#886).

The repo query (ordering/exclusion/bounding) is pinned in
tests/integration/test_load_pinned_repo.py. Here we pin the service contract:
layered output (L1+L2 only, never L3 content), the truncated / total_available
reporting, and the settings-driven default cap. memory_repo.list_pinned is
mocked so no DB is needed.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.schemas import LoadPinnedResponse, PinnedMemoryItem
from services.memory_service import MemoryService
from utils.datetime import utcnow


@pytest.fixture
def service():
    svc = MemoryService(MagicMock())
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(
        return_value=(ctx, str(ctx.workspace_id), str(ctx.id))
    )
    svc.memory_repo = MagicMock()
    svc._mock_context = ctx
    return svc


def _row(**overrides):
    m = MagicMock()
    m.id = overrides.get("id", uuid4())
    m.summary = overrides.get("summary", "agent goal")
    m.context_summary = overrides.get("context_summary", "why it matters")
    m.content = "FULL CONTENT — must not appear in the pinned item"
    m.type = overrides.get("type", "note")
    m.importance = overrides.get("importance", 0.9)
    m.delivery_mode = "always"
    m.created_at = utcnow()
    return m


async def _call(service, cap=None):
    return await service.load_pinned(
        user_id="u1",
        current_context_id=service._mock_context.id,
        current_workspace_id=service._mock_context.workspace_id,
        cap=cap,
    )


@pytest.mark.asyncio
async def test_load_pinned_returns_layered_items_without_content(service):
    rows = [_row(summary="goal A"), _row(summary="guardrail B")]
    service.memory_repo.list_pinned = AsyncMock(return_value=(rows, 2))

    result = await _call(service)
    assert isinstance(result, LoadPinnedResponse)
    assert len(result.memories) == 2
    assert all(isinstance(m, PinnedMemoryItem) for m in result.memories)
    # L1 + L2 are present; L3 content is structurally absent from the item.
    assert result.memories[0].summary == "goal A"
    assert result.memories[0].context_summary == "why it matters"
    assert not hasattr(result.memories[0], "content")


@pytest.mark.asyncio
async def test_load_pinned_not_truncated_when_total_within_cap(service):
    rows = [_row(), _row()]
    service.memory_repo.list_pinned = AsyncMock(return_value=(rows, 2))
    result = await _call(service, cap=10)
    assert result.truncated is False
    assert result.total_available == 2
    assert result.cap == 10


@pytest.mark.asyncio
async def test_load_pinned_flags_truncated_with_true_total(service):
    rows = [_row() for _ in range(3)]  # bounded to cap
    service.memory_repo.list_pinned = AsyncMock(return_value=(rows, 12))
    result = await _call(service, cap=3)
    assert result.truncated is True
    assert result.total_available == 12  # true total, not silently dropped
    assert len(result.memories) == 3


@pytest.mark.asyncio
async def test_load_pinned_uses_settings_cap_by_default(service):
    service.memory_repo.list_pinned = AsyncMock(return_value=([], 0))
    result = await _call(service)  # no explicit cap
    # The default cap from settings (100) is passed to the repo and echoed back.
    service.memory_repo.list_pinned.assert_awaited_once()
    assert service.memory_repo.list_pinned.await_args.args[2] == 100
    assert result.cap == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 1),  # falsy-zero is a real value (not None); clamp up to 1, never LIMIT 0
        (-5, 1),  # negative would be Postgres LIMIT -1 (no cap) — clamp up to 1
        (5000, 1000),  # clamp down to the upper bound (matches REST le=1000)
        ("50", 50),  # MCP sends JSON; coerce numeric string
    ],
)
async def test_load_pinned_coerces_and_clamps_cap(service, raw, expected):
    service.memory_repo.list_pinned = AsyncMock(return_value=([], 0))
    result = await _call(service, cap=raw)
    assert service.memory_repo.list_pinned.await_args.args[2] == expected
    assert result.cap == expected


@pytest.mark.asyncio
async def test_load_pinned_rejects_non_integer_cap(service):
    service.memory_repo.list_pinned = AsyncMock(return_value=([], 0))
    with pytest.raises(ValueError):
        await _call(service, cap="abc")
