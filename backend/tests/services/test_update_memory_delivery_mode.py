"""Tests for delivery_mode on the update path (#886): unpin + pin-via-update.

unpin = flip delivery_mode back to 'on_recall' (the memory stays persistent —
delivery_mode controls *loading*, scope controls *lifecycle*). Updating to
'always' pins to persistent, mirroring remember()'s pin-on-write.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import UpdateMemoryRequest
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
    return MemoryService(mock_db)


def _make_memory(**overrides):
    memory = MagicMock()
    memory.id = overrides.get("id", uuid4())
    memory.user_id = "test_user"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "Original summary for testing"
    memory.context_summary = None
    memory.content = "Original content"
    memory.details = None
    memory.type = "note"
    memory.importance = 0.5
    memory.tags = ["original"]
    memory.context = None
    memory.scope = overrides.get("scope", "working")
    memory.delivery_mode = overrides.get("delivery_mode", "on_recall")
    memory.promoted_at = overrides.get("promoted_at", None)
    memory.client = "mcp"
    memory.created_at = None
    memory.updated_at = None
    memory.deleted_at = None
    memory.embedding_status = "success"
    return memory


async def _update(service, memory, **fields):
    service.memory_repo.get = AsyncMock(return_value=memory)
    with (
        patch("services.permission_service.PermissionService") as mock_perm_cls,
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch(
            "services.memory_service.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
    ):
        mock_perm_cls.return_value.can_access_memory = AsyncMock(return_value=True)
        request = UpdateMemoryRequest(memory_id=memory.id, **fields)
        return await service._update_in_place(request, user_id="test_user")


@pytest.mark.asyncio
async def test_unpin_resets_delivery_mode_but_keeps_persistent(service):
    """Unpin: 'always' → 'on_recall'. Memory stays persistent (lifecycle intact)."""
    memory = _make_memory(delivery_mode="always", scope="persistent")
    await _update(service, memory, delivery_mode="on_recall")
    assert memory.delivery_mode == "on_recall"
    assert memory.scope == "persistent"


@pytest.mark.asyncio
async def test_update_to_always_pins_to_persistent(service):
    """Updating delivery_mode to 'always' on a working memory pins it persistent."""
    memory = _make_memory(delivery_mode="on_recall", scope="working")
    await _update(service, memory, delivery_mode="always")
    assert memory.delivery_mode == "always"
    assert memory.scope == "persistent"
    assert memory.promoted_at is not None


@pytest.mark.asyncio
async def test_update_without_delivery_mode_leaves_it_unchanged(service):
    """Omitting delivery_mode does not touch it (only non-None fields apply)."""
    memory = _make_memory(delivery_mode="always", scope="persistent")
    await _update(service, memory, importance=0.9)
    assert memory.delivery_mode == "always"


def test_update_memory_request_accepts_delivery_mode():
    req = UpdateMemoryRequest(memory_id=uuid4(), delivery_mode="always")
    assert req.delivery_mode == "always"


def test_update_memory_request_rejects_unknown_delivery_mode():
    with pytest.raises(ValueError):
        UpdateMemoryRequest(memory_id=uuid4(), delivery_mode="eventually")
