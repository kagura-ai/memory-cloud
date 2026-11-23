"""#2980: a context's ``fusion`` search setting (``auto`` | ``rrf`` | ``weighted``).

The column and its models, the REST response (stored value plus the
server-computed ``fusion_effective``), reset, the portability export, MCP
``update_search_config`` (sent only when the caller sends it) and
``get_context_info``. The resolution itself is pinned in
tests/services/test_search_fusion.py.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.config import ContextSearchConfig
from models.schemas import (
    ContextSearchConfigResponse,
    ContextSearchConfigUpdate,
    ExportedSearchConfig,
)

_MODULE = "api.routes.context_search_config"

_BASE = {
    "semantic_weight": 0.6,
    "bm25_weight": 0.4,
    "fetch_factor": 3,
    "use_rerank": False,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2",
}


# ---------------------------------------------------------------------------
# Column and models
# ---------------------------------------------------------------------------


def test_column_is_not_null_with_server_default_auto():
    col = ContextSearchConfig.__table__.c.fusion
    assert col.nullable is False
    assert str(col.server_default.arg) == "auto"
    assert col.default.arg == "auto"
    assert col.type.length == 20


def test_check_constraint_allows_the_three_values():
    checks = {
        c.name: str(c.sqltext)
        for c in ContextSearchConfig.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert checks["fusion_check"] == "fusion IN ('auto', 'rrf', 'weighted')"


def test_update_default_auto_and_unset():
    update = ContextSearchConfigUpdate(**_BASE)
    assert update.fusion == "auto"
    assert "fusion" not in update.model_dump(exclude_unset=True)


@pytest.mark.parametrize("value", ["auto", "rrf", "weighted"])
def test_update_accepts_the_three_values(value):
    assert ContextSearchConfigUpdate(**_BASE, fusion=value).fusion == value


@pytest.mark.parametrize("value", ["keyword_floor", "RRF", "union", "", None, 1])
def test_update_refuses_anything_else(value):
    with pytest.raises(ValidationError):
        ContextSearchConfigUpdate(**_BASE, fusion=value)


def test_openapi_update_schema_carries_the_enum():
    schema = ContextSearchConfigUpdate.model_json_schema()
    assert schema["properties"]["fusion"]["enum"] == ["auto", "rrf", "weighted"]
    assert schema["properties"]["fusion"]["default"] == "auto"
    assert "fusion" not in schema.get("required", [])


def test_response_defaults():
    fields = ContextSearchConfigResponse.model_fields
    assert fields["fusion"].default == "auto"
    assert fields["fusion_effective"].default == "weighted"


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "kept"), [("rrf", "rrf"), ("weighted", "weighted")])
async def test_a_partial_update_keeps_the_stored_fusion(stored, kept):
    from repositories.config_repository import ContextSearchConfigRepository

    config = SimpleNamespace(fusion=stored, semantic_weight=0.6)
    repo = ContextSearchConfigRepository(AsyncMock())
    repo.get_by_context = AsyncMock(return_value=config)
    await repo.update(
        uuid4(), ContextSearchConfigUpdate(**{**_BASE, "semantic_weight": 0.7, "bm25_weight": 0.3})
    )
    assert config.fusion == kept


@pytest.mark.asyncio
async def test_an_explicit_fusion_is_applied():
    from repositories.config_repository import ContextSearchConfigRepository

    config = SimpleNamespace(fusion="auto")
    repo = ContextSearchConfigRepository(AsyncMock())
    repo.get_by_context = AsyncMock(return_value=config)
    await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, fusion="rrf"))
    assert config.fusion == "rrf"


@pytest.mark.asyncio
async def test_reset_to_default_sets_auto():
    from repositories.config_repository import ContextSearchConfigRepository

    repo = ContextSearchConfigRepository(AsyncMock())
    repo.update = AsyncMock(return_value=SimpleNamespace())
    await repo.reset_to_default(uuid4())
    applied = repo.update.await_args.args[1]
    assert applied.fusion == "auto"
    assert "fusion" in applied.model_fields_set
    assert (applied.semantic_weight, applied.bm25_weight) == (0.6, 0.4)


def test_default_weight_constants():
    from models.config import DEFAULT_BM25_WEIGHT, DEFAULT_SEMANTIC_WEIGHT

    assert (DEFAULT_SEMANTIC_WEIGHT, DEFAULT_BM25_WEIGHT) == (0.6, 0.4)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_an_older_export_without_fusion_parses_as_auto():
    doc = {
        **_BASE,
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 512,
    }
    assert ExportedSearchConfig(**doc).fusion == "auto"
    assert ExportedSearchConfig(**doc, fusion="weighted").fusion == "weighted"


# ---------------------------------------------------------------------------
# REST response: stored value and fusion_effective
# ---------------------------------------------------------------------------


def _row(**overrides):
    now = datetime(2026, 11, 23, tzinfo=UTC)
    row = {
        "context_id": uuid4(),
        "semantic_weight": Decimal("0.60"),
        "bm25_weight": Decimal("0.40"),
        "fetch_factor": 3,
        "use_rerank": False,
        "reranker_provider": "voyage",
        "reranker_model": "rerank-2",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 512,
        "reinforce_enabled": True,
        "reinforce_max_boost": 0.15,
        "reinforce_require_host_arbitration": False,
        "routing_mode": "off",
        "content_chunks_enabled": False,
        "llm_selector_enabled": False,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


async def _get(row, *, deployment: str):
    from api.routes.context_search_config import get_context_search_config
    from services.content_chunk_policy import ContentChunksStatus
    from services.llm_selector_policy import LLMSelectorStatus

    perms = MagicMock()
    perms.check_context_write = AsyncMock()
    perms.check_context_owner = AsyncMock()
    repo = MagicMock()
    repo.create_or_get = AsyncMock(return_value=row)
    repo.content_chunk_backfill_pending = AsyncMock(return_value=0)
    chunks = ContentChunksStatus(stored=False, supported=True, gated=False, in_use=0)
    selector = LLMSelectorStatus(stored=False, gated=False, in_use=0)
    settings = SimpleNamespace(hybrid_fusion=deployment, embedding_provider="openai")
    with (
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.chunk_points_supported", return_value=True),
        patch(f"{_MODULE}.content_chunks_status", AsyncMock(return_value=chunks)),
        patch(f"{_MODULE}.llm_selector_status", AsyncMock(return_value=selector)),
        patch(f"{_MODULE}.get_settings", return_value=settings),
    ):
        return await get_context_search_config(uuid4(), {"user_id": "u-1"}, db=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "deployment", "stored", "effective"),
    [
        ({"fusion": "auto"}, "rrf", "auto", "rrf"),
        ({"fusion": "auto"}, "keyword_floor", "auto", "keyword_floor"),
        (
            {"fusion": "auto", "semantic_weight": Decimal("0.70"), "bm25_weight": Decimal("0.30")},
            "rrf",
            "auto",
            "weighted",
        ),
        ({"fusion": "rrf"}, "weighted", "rrf", "rrf"),
        ({"fusion": "weighted"}, "rrf", "weighted", "weighted"),
        # A row object without the attribute (an older shape) reads as auto.
        ({}, "rrf", "auto", "rrf"),
    ],
)
async def test_get_reports_the_stored_and_the_effective_fusion(
    overrides, deployment, stored, effective
):
    response = await _get(_row(**overrides), deployment=deployment)
    assert response.fusion == stored
    assert response.fusion_effective == effective
    body = response.model_dump(mode="json")
    assert (body["fusion"], body["fusion_effective"]) == (stored, effective)


# ---------------------------------------------------------------------------
# MCP update_search_config
# ---------------------------------------------------------------------------


def _mcp_config(fusion: str | None = "auto", *, semantic: float = 0.6, bm25: float = 0.4):
    row = SimpleNamespace(
        content_chunks_enabled=False,
        llm_selector_enabled=False,
        semantic_weight=semantic,
        bm25_weight=bm25,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
        reinforce_enabled=True,
        reinforce_max_boost=0.15,
        reinforce_require_host_arbitration=False,
        routing_mode="off",
    )
    if fusion is not None:
        row.fusion = fusion
    return row


async def _call_update(args: dict, *, config=None, after=None, deployment: str = "weighted"):
    from mcp_server.tools.search_config import handle_update_search_config

    db = MagicMock()
    db.rollback = AsyncMock()

    async def fake_get_db():
        yield db

    perms = MagicMock()
    perms.check_context_write = AsyncMock()
    perms.check_context_owner = AsyncMock()
    config = config if config is not None else _mcp_config()
    repo = MagicMock()
    repo.get_by_context = AsyncMock(return_value=config)
    repo.update = AsyncMock(return_value=after if after is not None else config)
    from config.settings import get_settings

    settings = get_settings()
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch("services.permission_service.PermissionService", return_value=perms),
        patch("repositories.config_repository.ContextSearchConfigRepository", return_value=repo),
        patch("mcp_server.tools.search_config._log_tool_usage", AsyncMock()),
        patch.object(settings, "hybrid_fusion", deployment),
    ):
        result = await handle_update_search_config(
            {"context_id": str(uuid4()), **args}, "user-1", uuid4()
        )
    return json.loads(result[0].text), repo


@pytest.mark.asyncio
async def test_mcp_an_omitted_fusion_is_not_sent():
    payload, repo = await _call_update({"use_rerank": True})
    assert payload["status"] == "success"
    applied = repo.update.await_args.args[1]
    assert "fusion" not in applied.model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["auto", "rrf", "weighted"])
async def test_mcp_a_sent_fusion_is_applied(value):
    payload, repo = await _call_update({"fusion": value})
    assert payload["status"] == "success"
    applied = repo.update.await_args.args[1]
    assert applied.fusion == value
    assert "fusion" in applied.model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["keyword_floor", "union", 3])
async def test_mcp_an_unknown_fusion_is_a_validation_error(value):
    payload, repo = await _call_update({"fusion": value})
    assert payload["error"] == "invalid_search_config"
    assert "fusion" in payload["message"]
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after", "deployment", "stored", "effective"),
    [
        (_mcp_config("auto"), "rrf", "auto", "rrf"),
        (_mcp_config("auto", semantic=0.8, bm25=0.2), "rrf", "auto", "weighted"),
        (_mcp_config("rrf"), "weighted", "rrf", "rrf"),
        (_mcp_config(None), "keyword_floor", "auto", "keyword_floor"),
    ],
)
async def test_mcp_result_reports_stored_and_effective(after, deployment, stored, effective):
    payload, _ = await _call_update({"use_rerank": False}, after=after, deployment=deployment)
    assert payload["config"]["fusion"] == stored
    assert payload["config"]["fusion_effective"] == effective


def test_mcp_tool_schema_exposes_the_enum():
    from mcp_server.tools._definitions import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "update_search_config")
    prop = tool["inputSchema"]["properties"]["fusion"]
    assert prop["type"] == "string"
    assert prop["enum"] == ["auto", "rrf", "weighted"]
    assert "rrf" in prop["description"] and "weighted" in prop["description"]
    assert "fusion_effective" in tool["description"]
    assert "fusion" not in tool["inputSchema"]["required"]


def test_get_context_info_definition_text_is_unchanged():
    """The core profile has no room: the keys are listed in the guide topic."""
    from mcp_server.tools._definitions import get_tool_definitions
    from mcp_server.tools.guide import GUIDE_TOPICS

    tool = next(t for t in get_tool_definitions() if t["name"] == "get_context_info")
    assert "fusion" not in tool["description"]
    assert "fusion" in GUIDE_TOPICS["get_context_info.returns"]


# ---------------------------------------------------------------------------
# MCP get_context_info
# ---------------------------------------------------------------------------


async def _context_info(row, *, deployment: str):
    from config.settings import get_settings
    from mcp_server.tools.context import handle_get_context_info

    ctx = SimpleNamespace(
        id=uuid4(),
        name="dev",
        display_name="Dev",
        summary="s",
        usage_guide="g",
        is_private=True,
        is_locked=False,
        workspace_id=uuid4(),
    )
    db = AsyncMock()

    async def execute(stmt, *args, **kwargs):
        result = MagicMock()
        is_config = "context_search_configs" in str(stmt)
        result.scalar_one_or_none.return_value = row if is_config else None
        return result

    db.execute = AsyncMock(side_effect=execute)

    async def fake_get_db():
        yield db

    stats = SimpleNamespace(
        total_count=0,
        working_count=0,
        persistent_count=0,
        by_type={},
        by_importance={},
        recent_activity={},
    )
    service = MagicMock()
    service.get_stats = AsyncMock(return_value=stats)
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch(
            "mcp_server.tools.context._resolve_context_for_read",
            new=AsyncMock(return_value=ctx),
        ),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch("mcp_server.tools.context._guardrails_field", new=AsyncMock(return_value={})),
        patch.object(get_settings(), "hybrid_fusion", deployment),
    ):
        result = await handle_get_context_info(
            {"context_id": str(ctx.id)}, user_id="u1", workspace_id=None
        )
    return json.loads(result[0].text)["context"]["search_config"]


def _info_row(fusion: str | None, *, semantic: float = 0.6, bm25: float = 0.4):
    row = SimpleNamespace(
        semantic_weight=semantic,
        bm25_weight=bm25,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=512,
        llm_selector_enabled=False,
    )
    if fusion is not None:
        row.fusion = fusion
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "deployment", "stored", "effective"),
    [
        (_info_row("rrf"), "weighted", "rrf", "rrf"),
        (_info_row("auto", semantic=0.7, bm25=0.3), "rrf", "auto", "weighted"),
        (_info_row(None), "rrf", "auto", "rrf"),
        (None, "keyword_floor", "auto", "keyword_floor"),
    ],
)
async def test_get_context_info_reports_the_fusion(row, deployment, stored, effective):
    config = await _context_info(row, deployment=deployment)
    assert config["fusion"] == stored
    assert config["fusion_effective"] == effective
