"""#2015: ContextSearchConfig.content_chunks_enabled — the per-context opt-in."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from models.config import ContextSearchConfig
from models.schemas import (
    ContextSearchConfigResponse,
    ContextSearchConfigUpdate,
    ExportedSearchConfig,
)
from repositories.config_repository import ContextSearchConfigRepository

_BASE = {
    "semantic_weight": 0.6,
    "bm25_weight": 0.4,
    "fetch_factor": 3,
    "use_rerank": False,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2",
}


def test_column_defaults_off():
    col = ContextSearchConfig.__table__.c.content_chunks_enabled
    assert col.nullable is False
    assert str(col.server_default.arg) == "false"
    assert col.default.arg is False


def test_update_default_off_and_unset():
    config = ContextSearchConfigUpdate(**_BASE)
    assert config.content_chunks_enabled is False
    assert "content_chunks_enabled" not in config.model_dump(exclude_unset=True)


def test_response_and_export_carry_the_flag():
    assert ContextSearchConfigResponse.model_fields["content_chunks_enabled"].default is False
    assert ExportedSearchConfig.model_fields["content_chunks_enabled"].default is False


@pytest.mark.asyncio
async def test_partial_update_keeps_enabled():
    config = SimpleNamespace(content_chunks_enabled=True, semantic_weight=0.6)
    repo = ContextSearchConfigRepository(AsyncMock())
    repo.get_by_context = AsyncMock(return_value=config)
    await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE))
    assert config.content_chunks_enabled is True


@pytest.mark.asyncio
async def test_explicit_flag_is_applied():
    config = SimpleNamespace(content_chunks_enabled=False)
    repo = ContextSearchConfigRepository(AsyncMock())
    repo.get_by_context = AsyncMock(return_value=config)
    await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True))
    assert config.content_chunks_enabled is True


def test_mcp_tool_schema_exposes_the_flag():
    from mcp_server.tools._definitions import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "update_search_config")
    prop = tool["inputSchema"]["properties"]["content_chunks_enabled"]
    assert prop["type"] == "boolean"
    assert "content_chunks_enabled" in tool["description"]
