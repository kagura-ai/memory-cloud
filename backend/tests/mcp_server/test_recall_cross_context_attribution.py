"""#1228: cross-context recall must hand the ADDITIONAL listed contexts to
the usage logger for diagnostic read-attribution.

Follows the mock-db handler convention of tests/mcp_server/ (patch
db.base.get_db + the context resolver + the service, then inspect the
_log_tool_usage call). The row-writing behavior itself is covered in
test_helpers.py::TestLogToolUsageAttribution; this suite pins the handler
side of the contract: WHICH context ids get attributed.
"""

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_recall


def _recall_result():
    result = MagicMock()
    result.results = []
    result.related_tags = []
    result.explore_hints = None
    result.confidence = None
    return result


@contextlib.contextmanager
def _patched(log_tool_usage: AsyncMock):
    """Patch the handler's collaborators so the test exercises pure handler
    logic: same workspace/privacy/embedding-model for every listed context
    (the #708 mismatch guards all pass)."""

    async def mock_get_db():
        yield AsyncMock()

    # last_used_at must be a real sentinel (not a MagicMock attribute): #1257
    # touches the resolved context's timestamp with datetime math.
    shared_context = MagicMock(last_used_at=None)

    service = MagicMock()
    service.recall = AsyncMock(return_value=_recall_result())
    service_cls = MagicMock(return_value=service)

    config = MagicMock()
    config.embedding_model = "text-embedding-3-small"
    config_repo = MagicMock()
    config_repo.create_or_get = AsyncMock(return_value=config)
    config_repo_cls = MagicMock(return_value=config_repo)

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=shared_context),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=log_tool_usage),
        patch("services.memory_service.MemoryService", new=service_cls),
        patch(
            "repositories.config_repository.ContextSearchConfigRepository",
            new=config_repo_cls,
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_cross_context_recall_attributes_additional_contexts():
    ctx_a, ctx_b, ctx_c = uuid4(), uuid4(), uuid4()
    log_tool_usage = AsyncMock()

    with _patched(log_tool_usage):
        result = await handle_recall(
            {"query": "q", "context_ids": [str(ctx_a), str(ctx_b), str(ctx_c)]},
            user_id="u1",
            workspace_id=None,
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    log_tool_usage.assert_awaited_once()
    call = log_tool_usage.await_args
    # Primary context carries the single billable UsageStats row...
    assert call.args[5] == ctx_a
    # ...and every ADDITIONAL listed context is attributed, in order.
    assert call.kwargs["attributed_context_ids"] == [ctx_b, ctx_c]


@pytest.mark.asyncio
async def test_single_context_recall_attributes_nothing():
    ctx_a = uuid4()
    log_tool_usage = AsyncMock()

    with _patched(log_tool_usage):
        result = await handle_recall(
            {"query": "q", "context_id": str(ctx_a)},
            user_id="u1",
            workspace_id=None,
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    log_tool_usage.assert_awaited_once()
    call = log_tool_usage.await_args
    assert call.args[5] == ctx_a
    assert not call.kwargs.get("attributed_context_ids")


@pytest.mark.asyncio
async def test_duplicate_context_ids_are_deduplicated():
    """#1228 review: duplicated ids would each burn a permission resolve and
    write an attribution row — recall(context_ids=[A, B, A, B]) must
    attribute B exactly once and never attribute the primary A."""
    ctx_a, ctx_b = uuid4(), uuid4()
    log_tool_usage = AsyncMock()

    with _patched(log_tool_usage):
        result = await handle_recall(
            {"query": "q", "context_ids": [str(ctx_a), str(ctx_b), str(ctx_a), str(ctx_b)]},
            user_id="u1",
            workspace_id=None,
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    call = log_tool_usage.await_args
    assert call.args[5] == ctx_a
    assert call.kwargs["attributed_context_ids"] == [ctx_b]


@pytest.mark.asyncio
async def test_context_ids_capped_at_schema_max():
    """#1228 review: the inputSchema's maxItems:20 is advisory for
    non-validating clients — the handler must enforce it server-side so a
    single billable call cannot write unbounded attribution rows."""
    ids = [str(uuid4()) for _ in range(21)]
    log_tool_usage = AsyncMock()

    with _patched(log_tool_usage):
        result = await handle_recall(
            {"query": "q", "context_ids": ids}, user_id="u1", workspace_id=None
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "error"
    assert payload["error"] == "too_many_context_ids"
    log_tool_usage.assert_not_awaited()
