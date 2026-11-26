"""#3014: a context's ``user_facts_lane`` search setting (a boolean, off by default).

The column and its models, the REST response, reset, the portability export,
MCP ``update_search_config`` (sent only when the caller sends it, the owner /
editor check of every search setting) and ``get_context_info``. The lane itself
is pinned in tests/services/test_user_facts_lane.py and
tests/integration/test_3014_user_facts_lane_db.py.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.config import ContextSearchConfig
from models.schemas import (
    ContextSearchConfigResponse,
    ContextSearchConfigUpdate,
    ExportedSearchConfig,
    RecallResponse,
)
from tests.api.test_context_fusion_setting import (
    _BASE,
    _call_update,
    _context_info,
    _get,
    _info_row,
    _mcp_config,
    _row,
)


def test_column_is_not_null_with_server_default_false():
    col = ContextSearchConfig.__table__.c.user_facts_lane
    assert col.nullable is False
    assert str(col.server_default.arg) == "false"
    assert col.default.arg is False


def test_update_default_false_and_unset():
    update = ContextSearchConfigUpdate(**_BASE)
    assert update.user_facts_lane is False
    assert "user_facts_lane" not in update.model_dump(exclude_unset=True)
    assert ContextSearchConfigUpdate(**_BASE, user_facts_lane=True).user_facts_lane is True


def test_openapi_shapes():
    update = ContextSearchConfigUpdate.model_json_schema()["properties"]["user_facts_lane"]
    assert update["type"] == "boolean" and update["default"] is False
    assert ContextSearchConfigResponse.model_fields["user_facts_lane"].default is False
    recall = RecallResponse.model_json_schema()["properties"]["user_facts"]
    assert "user_facts" not in RecallResponse.model_json_schema().get("required", [])
    assert recall["anyOf"][0]["type"] == "array"


def test_an_unset_recall_key_is_left_out_of_the_body():
    assert "user_facts" not in RecallResponse(results=[]).model_dump(exclude_none=True)
    assert (
        RecallResponse(results=[], user_facts=[]).model_dump(exclude_none=True)["user_facts"] == []
    )


@pytest.mark.asyncio
async def test_a_partial_update_keeps_the_stored_switch():
    from repositories.config_repository import ContextSearchConfigRepository

    config = SimpleNamespace(user_facts_lane=True)
    repo = ContextSearchConfigRepository(AsyncMock())
    repo.get_by_context = AsyncMock(return_value=config)
    await repo.update(uuid4(), ContextSearchConfigUpdate(**{**_BASE, "fetch_factor": 4}))
    assert config.user_facts_lane is True
    await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, user_facts_lane=False))
    assert config.user_facts_lane is False


@pytest.mark.asyncio
async def test_reset_to_default_turns_it_off():
    from repositories.config_repository import ContextSearchConfigRepository

    repo = ContextSearchConfigRepository(AsyncMock())
    repo.update = AsyncMock(return_value=SimpleNamespace())
    await repo.reset_to_default(uuid4())
    applied = repo.update.await_args.args[1]
    assert applied.user_facts_lane is False
    assert "user_facts_lane" in applied.model_fields_set


def test_an_older_export_parses_as_off():
    doc = {**_BASE, "embedding_model": "text-embedding-3-small", "embedding_dimensions": 512}
    assert ExportedSearchConfig(**doc).user_facts_lane is False
    assert ExportedSearchConfig(**doc, user_facts_lane=True).user_facts_lane is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_rest_get_reports_the_stored_switch(stored):
    response = await _get(_row(user_facts_lane=stored), deployment="weighted")
    assert response.model_dump(mode="json")["user_facts_lane"] is stored


@pytest.mark.asyncio
async def test_rest_get_of_an_older_row_reads_off():
    response = await _get(_row(), deployment="weighted")
    assert response.user_facts_lane is False


# ---------------------------------------------------------------------------
# MCP update_search_config / get_context_info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_an_omitted_switch_is_not_sent():
    payload, repo = await _call_update({"use_rerank": True})
    assert payload["status"] == "success"
    assert "user_facts_lane" not in repo.update.await_args.args[1].model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_mcp_a_sent_switch_is_applied_and_reported(value):
    after = _mcp_config()
    after.user_facts_lane = value
    payload, repo = await _call_update({"user_facts_lane": value}, after=after)
    applied = repo.update.await_args.args[1]
    assert applied.user_facts_lane is value
    assert "user_facts_lane" in applied.model_fields_set
    assert payload["config"]["user_facts_lane"] is value


@pytest.mark.asyncio
async def test_mcp_a_caller_without_write_access_is_refused():
    """The owner / editor check every search setting has (a viewer cannot)."""
    from mcp_server.tools.search_config import handle_update_search_config
    from utils.exceptions import AuthorizationError

    db = MagicMock()
    db.rollback = AsyncMock()

    async def fake_get_db():
        yield db

    perms = MagicMock()
    perms.check_context_write = AsyncMock(side_effect=AuthorizationError("viewer"))
    repo = MagicMock()
    repo.update = AsyncMock()
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch("services.permission_service.PermissionService", return_value=perms),
        patch("repositories.config_repository.ContextSearchConfigRepository", return_value=repo),
    ):
        result = await handle_update_search_config(
            {"context_id": str(uuid4()), "user_facts_lane": True}, "user-1", uuid4()
        )
    assert json.loads(result[0].text)["error"] == "permission_denied"
    repo.update.assert_not_awaited()


def test_mcp_tool_schema_exposes_the_switch():
    from mcp_server.tools._definitions import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "update_search_config")
    prop = tool["inputSchema"]["properties"]["user_facts_lane"]
    assert prop["type"] == "boolean"
    assert "user_facts_lane" in tool["description"]
    assert "user_facts_lane" not in tool["inputSchema"]["required"]


def test_recall_returns_line_names_user_facts():
    from mcp_server.tools._definitions import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "recall")
    assert "order?, user_facts?," in tool["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False), (None, False)])
async def test_get_context_info_reports_the_switch(value, expected):
    row = _info_row("auto")
    if value is not None:
        row.user_facts_lane = value
    config = await _context_info(row, deployment="weighted")
    assert config["user_facts_lane"] is expected


@pytest.mark.asyncio
async def test_get_context_info_without_a_config_row_reads_off():
    config = await _context_info(None, deployment="weighted")
    assert config["user_facts_lane"] is False


@pytest.mark.asyncio
async def test_rest_recall_opts_in_and_returns_the_key():
    from api.routes.memory import recall
    from models.schemas import RecallRequest

    service = MagicMock()
    service.recall = AsyncMock(return_value=RecallResponse(results=[], user_facts=[]))
    out = await recall(
        RecallRequest(query="q"),
        {"user_id": "u-1", "current_workspace_id": uuid4()},
        service,
        db=AsyncMock(),
    )
    assert service.recall.await_args.kwargs["user_facts_lane"] is True
    assert out.model_dump(exclude_none=True)["user_facts"] == []
