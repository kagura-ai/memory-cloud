"""#2144: ContextSearchConfig.llm_selector_enabled — the per-context opt-in.

Mirrors the MCP half of ``test_content_chunks_config.py`` (no store check:
the selector stores nothing). Nothing reads the flag at recall time yet.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.config import ContextSearchConfig
from models.schemas import ContextSearchConfigResponse, ContextSearchConfigUpdate

_BASE = {
    "semantic_weight": 0.6,
    "bm25_weight": 0.4,
    "fetch_factor": 3,
    "use_rerank": False,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2",
}


def test_column_defaults_off():
    col = ContextSearchConfig.__table__.c.llm_selector_enabled
    assert col.nullable is False
    assert str(col.server_default.arg) == "false"
    assert col.default.arg is False
    stamp = ContextSearchConfig.__table__.c.llm_selector_enabled_at
    assert stamp.nullable is True
    assert stamp.server_default is None


def test_update_default_off_and_unset():
    config = ContextSearchConfigUpdate(**_BASE)
    assert config.llm_selector_enabled is False
    assert "llm_selector_enabled" not in config.model_dump(exclude_unset=True)


def test_response_carries_the_stored_flag_and_the_computed_fields():
    fields = ContextSearchConfigResponse.model_fields
    assert fields["llm_selector_enabled"].default is False
    assert fields["llm_selector_effective"].default is False
    assert fields["llm_selector_limit"].default is None
    assert fields["llm_selector_in_use"].default == 0
    assert fields["llm_selector_disabled_reason"].default is None
    assert fields["llm_selector_can_change"].default is False


def test_mcp_tool_schema_exposes_the_flag():
    from mcp_server.tools._definitions import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "update_search_config")
    prop = tool["inputSchema"]["properties"]["llm_selector_enabled"]
    assert prop["type"] == "boolean"
    assert "llm_selector_effective" in prop["description"]
    assert "llm_selector_enabled" in tool["description"]
    assert "llm_selector_effective" in tool["description"]


# ---------------------------------------------------------------------------
# Changing the flag takes the context owner (MCP twin of the REST gate)
# ---------------------------------------------------------------------------


def _mcp_config(enabled: bool):
    return SimpleNamespace(
        content_chunks_enabled=False,
        llm_selector_enabled=enabled,
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
        reinforce_enabled=True,
        reinforce_max_boost=0.15,
        reinforce_require_host_arbitration=False,
        routing_mode="off",
    )


async def _call_update(args: dict, *, stored: bool, owner: bool):
    from mcp_server.tools.search_config import handle_update_search_config
    from utils.exceptions import AuthorizationError

    db = MagicMock()
    db.rollback = AsyncMock()

    async def fake_get_db():
        yield db

    perms = MagicMock()
    perms.check_context_write = AsyncMock()
    perms.check_context_owner = AsyncMock(side_effect=None if owner else AuthorizationError())
    config = _mcp_config(stored)
    repo = MagicMock()
    repo.get_by_context = AsyncMock(return_value=config)
    repo.update = AsyncMock(return_value=config)
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch("services.permission_service.PermissionService", return_value=perms),
        patch("repositories.config_repository.ContextSearchConfigRepository", return_value=repo),
        patch("mcp_server.tools.search_config._log_tool_usage", AsyncMock()),
    ):
        result = await handle_update_search_config(
            {"context_id": str(uuid4()), **args}, "user-1", uuid4()
        )
    return json.loads(result[0].text), perms, repo, db


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "requested"), [(False, True), (True, False)])
async def test_mcp_an_editor_cannot_change_the_flag(stored, requested):
    payload, _, repo, _ = await _call_update(
        {"llm_selector_enabled": requested}, stored=stored, owner=False
    )
    assert payload["error"] == "permission_denied"
    assert "llm_selector_enabled" in payload["message"]
    assert payload["required_role"] == "owner"
    assert "llm_selector_enabled" in payload["help"]
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_an_editor_can_change_other_fields():
    payload, perms, repo, _ = await _call_update({"use_rerank": True}, stored=True, owner=False)
    assert payload["status"] == "success"
    perms.check_context_owner.assert_not_awaited()
    applied = repo.update.await_args.args[1]
    assert applied.use_rerank is True
    # Omitted: not round-tripped, so a concurrent owner change is never undone.
    assert "llm_selector_enabled" not in applied.model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_mcp_an_editor_sending_the_stored_value_is_a_no_op(stored):
    payload, perms, repo, _ = await _call_update(
        {"llm_selector_enabled": stored}, stored=stored, owner=False
    )
    assert payload["status"] == "success"
    perms.check_context_owner.assert_not_awaited()
    assert "llm_selector_enabled" not in repo.update.await_args.args[1].model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "requested"), [(False, True), (True, False)])
async def test_mcp_an_owner_or_workspace_admin_can_change_the_flag(stored, requested):
    payload, perms, repo, _ = await _call_update(
        {"llm_selector_enabled": requested}, stored=stored, owner=True
    )
    assert payload["status"] == "success"
    perms.check_context_owner.assert_awaited_once()
    assert repo.update.await_args.args[1].llm_selector_enabled is requested


@pytest.mark.asyncio
async def test_mcp_the_lancedb_store_does_not_block_the_selector():
    """The selector stores nothing: no store check, unlike content chunks."""
    with patch("services.permission_service.chunk_points_supported", return_value=False):
        payload, _, repo, _ = await _call_update(
            {"llm_selector_enabled": True}, stored=False, owner=True
        )
    assert payload["status"] == "success"
    assert repo.update.await_args.args[1].llm_selector_enabled is True


@pytest.mark.asyncio
async def test_mcp_both_flags_in_one_call_take_both_gates():
    payload, perms, repo, _ = await _call_update(
        {"content_chunks_enabled": True, "llm_selector_enabled": True}, stored=False, owner=True
    )
    assert payload["status"] == "success"
    assert perms.check_context_owner.await_count == 2
    applied = repo.update.await_args.args[1]
    assert (applied.content_chunks_enabled, applied.llm_selector_enabled) == (True, True)


# ---------------------------------------------------------------------------
# The plan feature and the cap, on the MCP door
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_a_plan_without_the_selector_is_feature_not_available():
    from utils.exceptions import FeatureNotAvailableError

    refusal = FeatureNotAvailableError.for_feature("free", "llm_selector")
    with patch(
        "services.llm_selector_policy.ensure_llm_selector_plan",
        AsyncMock(side_effect=refusal),
    ):
        payload, perms, repo, db = await _call_update(
            {"llm_selector_enabled": True}, stored=False, owner=True
        )
    assert payload["error"] == "feature_not_available"
    assert (payload["gate"], payload["feature"]) == ("plan", "llm_selector")
    assert (payload["required_plan"], payload["required_plan_display"]) == ("basic", "M")
    perms.check_context_owner.assert_not_awaited()
    repo.update.assert_not_awaited()
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_mcp_the_cap_is_quota_exceeded_with_the_counts():
    from services.llm_selector_policy import _raise_cap
    from utils.exceptions import QuotaExceededError

    with pytest.raises(QuotaExceededError) as exc:
        _raise_cap("basic", 1, 1)
    with patch(
        "services.llm_selector_policy.ensure_llm_selector_capacity",
        AsyncMock(side_effect=exc.value),
    ):
        payload, _, repo, db = await _call_update(
            {"llm_selector_enabled": True}, stored=False, owner=True
        )
    assert payload["error"] == "quota_exceeded"
    assert payload["quota_type"] == "llm_selector_contexts"
    assert (payload["current"], payload["limit"], payload["required_plan"]) == (1, 1, "pro")
    assert payload["feature"] == "llm_selector"
    repo.update.assert_not_awaited()
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_mcp_turning_off_never_asks_the_plan():
    plan, cap = AsyncMock(), AsyncMock()
    with (
        patch("services.llm_selector_policy.ensure_llm_selector_plan", plan),
        patch("services.llm_selector_policy.ensure_llm_selector_capacity", cap),
    ):
        payload, _, _, _ = await _call_update(
            {"llm_selector_enabled": False}, stored=True, owner=True
        )
    assert payload["status"] == "success"
    plan.assert_not_awaited()
    cap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_mcp_the_result_says_whether_the_flag_is_acted_on(active):
    with patch(
        "services.llm_selector_policy.llm_selector_effective",
        AsyncMock(return_value=active),
    ):
        payload, _, _, _ = await _call_update({"use_rerank": True}, stored=True, owner=False)
    assert payload["config"]["llm_selector_enabled"] is True
    assert payload["config"]["llm_selector_effective"] is active


@pytest.mark.asyncio
async def test_mcp_with_the_gate_off_an_enabled_flag_is_effective():
    """Default deployment: no plan query, the stored flag is the answer."""
    payload, _, _, _ = await _call_update({"llm_selector_enabled": True}, stored=False, owner=True)
    # The fake repo returns the (stored off) row; the stored value decides.
    assert payload["config"]["llm_selector_effective"] is False
    payload, _, _, _ = await _call_update({"use_rerank": True}, stored=True, owner=False)
    assert payload["config"]["llm_selector_effective"] is True


# ---------------------------------------------------------------------------
# get_context_info reports the flag, stored and acted on
# ---------------------------------------------------------------------------


async def _context_info(row, *, effective=None):
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
        # The search-config row for its own SELECT; nothing for the others.
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
    patches = [
        patch("db.base.get_db", new=fake_get_db),
        patch(
            "mcp_server.tools.context._resolve_context_for_read",
            new=AsyncMock(return_value=ctx),
        ),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch("mcp_server.tools.context._guardrails_field", new=AsyncMock(return_value={})),
    ]
    effective_mock = AsyncMock(return_value=effective)
    if effective is not None:
        patches.append(patch("services.llm_selector_policy.llm_selector_effective", effective_mock))
    for p in patches:
        p.start()
    try:
        result = await handle_get_context_info(
            {"context_id": str(ctx.id)}, user_id="u1", workspace_id=None
        )
    finally:
        for p in reversed(patches):
            p.stop()
    return json.loads(result[0].text)["context"]["search_config"], effective_mock


def _row(enabled: bool):
    return SimpleNamespace(
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=512,
        llm_selector_enabled=enabled,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_get_context_info_reports_stored_and_effective(active):
    config, effective = await _context_info(_row(True), effective=active)
    assert config["llm_selector_enabled"] is True
    assert config["llm_selector_effective"] is active
    assert effective.await_args.kwargs == {"stored": True}


@pytest.mark.asyncio
async def test_get_context_info_off_and_without_a_row_reads_off():
    for row in (_row(False), None):
        config, _ = await _context_info(row)
        assert (config["llm_selector_enabled"], config["llm_selector_effective"]) == (
            False,
            False,
        )
