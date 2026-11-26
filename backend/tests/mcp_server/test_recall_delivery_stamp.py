"""#3022: the MCP recall tool stamps the delivery of what its reply carries.

The reply is fitted after the service returns: ``max_chars`` takes content off
the lowest-ranked rows, then off the user facts rows, then cuts rows
(``_recall_reply``). So the tool asks the service not to stamp
(``stamp_delivery=False``) and stamps, after its own commit, exactly the rows
whose content the final reply carries. A row the reply ships summary-only, or
not at all, is never stamped. A failed stamp follows the user facts lane's
failure rule: the reply goes without ``user_facts`` when its rows carried
content; ``results`` are returned as they are.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from mcp_server.tools import memory as mcp_memory
from models.schemas import RecallResponse
from tests.mcp_server.test_recall_envelope import _get_db_yielding, _memory


async def _run(args: dict[str, Any], result: RecallResponse, *, stamped: bool = True):
    service = MagicMock()
    service.recall = AsyncMock(return_value=result)
    service.record_delivery = AsyncMock(return_value=stamped)
    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", new=_get_db_yielding(AsyncMock())))
        stack.enter_context(
            patch(
                "mcp_server.tools.memory._resolve_context_for_read",
                new=AsyncMock(return_value=MagicMock()),
            )
        )
        stack.enter_context(
            patch("mcp_server.tools.memory._context_response_fields", return_value={})
        )
        stack.enter_context(
            patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock())
        )
        stack.enter_context(patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()))
        stack.enter_context(
            patch("services.memory_service.MemoryService", new=MagicMock(return_value=service))
        )
        reply = await mcp_memory.handle_recall(
            {"query": "q", "context_id": str(uuid4()), **args}, "u", None
        )
    return json.loads(reply[0].text), service


def _shipped(items: list[dict[str, Any]]) -> set[UUID]:
    return {UUID(item["memory_id"]) for item in items if "content" in item}


def _stamped(service: MagicMock) -> set[UUID]:
    service.record_delivery.assert_awaited_once()
    (ids,) = service.record_delivery.await_args.args
    return set(ids)


@pytest.mark.asyncio
async def test_the_service_is_asked_not_to_stamp():
    _, service = await _run({"full": 1}, RecallResponse(results=[_memory(content="body")]))
    assert service.recall.await_args.kwargs["stamp_delivery"] is False


@pytest.mark.asyncio
async def test_rows_the_fit_left_summary_only_are_not_stamped():
    rows = [_memory(content="x" * 3_000) for _ in range(6)]
    payload, service = await _run(
        {"full": 6, "max_chars": 10_000, "verbosity": "full"}, RecallResponse(results=rows)
    )
    kept = _shipped(payload["results"])
    # The fit took content off the lowest-ranked rows; those reach the reader
    # summary-only and carry no stamp.
    assert 0 < payload["full_applied"] < len(rows)
    assert len(kept) == payload["full_applied"]
    assert _stamped(service) == kept


@pytest.mark.asyncio
async def test_user_facts_rows_are_stamped_only_with_their_content():
    fact = _memory(type="fact", content="Stairs hurt.")
    main = _memory(content="body")
    payload, service = await _run({"full": 1}, RecallResponse(results=[main], user_facts=[fact]))
    assert _stamped(service) == {main.memory_id, fact.memory_id}
    assert payload["user_facts"][0]["content"] == "Stairs hurt."

    # Over budget the results and then the lane rows give their content up:
    # nothing the reply carries has content, so nothing is stamped.
    facts = [_memory(type="fact", content="y" * 1_500) for _ in range(2)]
    rows = [_memory(summary="s" * 1_500, content="x" * 100) for _ in range(5)]
    payload, service = await _run(
        {"full": 5, "max_chars": 10_000, "verbosity": "full"},
        RecallResponse(results=rows, user_facts=facts),
    )
    assert payload["user_facts"] and not _shipped(payload["user_facts"])
    assert not _shipped(payload["results"])
    service.record_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_summary_only_reply_runs_no_stamp():
    _, service = await _run({"full": 0}, RecallResponse(results=[_memory()]))
    service.record_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_stamp_drops_lane_rows_with_content_and_keeps_results():
    fact = _memory(type="fact", content="Stairs hurt.")
    main = _memory(content="body")
    payload, _ = await _run(
        {"full": 1}, RecallResponse(results=[main], user_facts=[fact]), stamped=False
    )
    assert "user_facts" not in payload
    assert payload["results"][0]["content"] == "body"

    # Lane rows without content are not the stamp's: the key stays.
    bare_fact = _memory(type="fact")
    payload, _ = await _run(
        {"full": 1}, RecallResponse(results=[main], user_facts=[bare_fact]), stamped=False
    )
    assert payload["user_facts"][0]["memory_id"] == str(bare_fact.memory_id)
