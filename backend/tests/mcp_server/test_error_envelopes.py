"""MCP error-envelope consistency (#1323, #1622).

Pins these contracts:
- pydantic ValidationError from tool-request construction surfaces as a
  structured ``invalid_argument`` envelope (no pydantic internals/URLs)
  via the dispatch-level arm in ``execute_tool_call``;
- ``_format_validation_error`` renders field/constraint summaries;
- ``handle_update_memory`` returns the ``memory_not_found`` envelope
  (slug + help) instead of falling through to the generic handler;
- every ``{"status": "error"}`` envelope under ``mcp_server/tools/`` is built
  by ``_error_response`` and carries the ``is_error`` marker the transports
  turn into ``CallToolResult.isError`` (#1622).
"""

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mcp_server.tools import _helpers
from mcp_server.tools._helpers import (
    ToolErrorContent,
    _error_response,
    _format_validation_error,
    _success_response,
)

TOOLS_DIR = Path(_helpers.__file__).parent


def _build_validation_error() -> ValidationError:
    from models.schemas import RememberRequest

    try:
        RememberRequest(
            summary="a summary long enough",
            content="c",
            type="note",
            importance=1.5,
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


class TestFormatValidationError:
    def test_names_field_and_constraint_without_pydantic_internals(self):
        message = _format_validation_error(_build_validation_error())

        assert "importance" in message
        assert "less than or equal to 1" in message
        assert "pydantic" not in message.lower()
        assert "RememberRequest" not in message

    def test_non_pydantic_exception_falls_back_to_str(self):
        assert _format_validation_error(RuntimeError("boom")) == "boom"


class TestDispatchValidationArm:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            (
                "remember",
                {
                    "summary": "a summary long enough",
                    "content": "c",
                    "type": "note",
                    "importance": 1.5,
                },
            ),
            ("recall", {"query": "q", "k": 0}),
        ],
    )
    async def test_out_of_range_input_returns_invalid_argument(self, tool, args):
        """#1323: client typos must not leak the raw pydantic dump."""
        from mcp_server.tools import execute_tool_call

        with patch(
            "mcp_server.tools._check_rate_limit",
            new_callable=AsyncMock,
            return_value=(True, 0, 100),
        ):
            result = await execute_tool_call(
                tool,
                {"context_id": str(uuid4()), **args},
                "test_user",
                uuid4(),
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "invalid_argument"
        assert "pydantic" not in payload["message"].lower()
        assert "https://" not in payload["message"]


class TestUpdateMemoryNotFoundEnvelope:
    @pytest.mark.asyncio
    async def test_missing_memory_returns_structured_envelope(self):
        """#1323: update_memory mirrors handle_reference's memory_not_found."""
        from mcp_server.tools.memory import handle_update_memory
        from utils.exceptions import NotFoundException

        mock_db = MagicMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        memory_id = uuid4()
        mock_service = MagicMock()
        mock_service.update_memory = AsyncMock(
            side_effect=NotFoundException("Memory", str(memory_id))
        )

        mock_ctx = MagicMock()
        mock_ctx.id = uuid4()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.memory._check_viewer_permission",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "mcp_server.tools.memory._resolve_context",
                new=AsyncMock(return_value=mock_ctx),
            ),
            patch(
                "services.memory_service.MemoryService",
                new=MagicMock(return_value=mock_service),
            ),
        ):
            result = await handle_update_memory(
                {
                    "context_id": str(uuid4()),
                    "memory_id": str(memory_id),
                    "importance": 0.9,
                },
                "test_user",
                uuid4(),
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "memory_not_found"
        assert str(memory_id) in payload["message"]
        assert "help" in payload


class TestRememberQuotaExceededEnvelope:
    """#1549 gate2: a ``QuotaExceededError`` out of ``MemoryService.remember``
    (total ``memory_limit`` or the daily ``memories_per_day`` reservation) is a
    429, not a crash. ``handle_remember`` must return the ``quota_exceeded``
    envelope that analysis / files already use — structured details forwarded
    — and log the call as 429 instead of falling into the generic 500 arm.
    """

    async def _remember_raising(self, exc):
        from mcp_server.tools.memory import handle_remember

        mock_db = MagicMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_service = MagicMock()
        mock_service.remember = AsyncMock(side_effect=exc)
        mock_ctx = MagicMock()
        mock_ctx.id = uuid4()
        log_usage = AsyncMock()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.memory._check_viewer_permission",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "mcp_server.tools.memory._resolve_context",
                new=AsyncMock(return_value=mock_ctx),
            ),
            patch("mcp_server.tools.memory._log_tool_usage", new=log_usage),
            patch(
                "services.memory_service.MemoryService",
                new=MagicMock(return_value=mock_service),
            ),
        ):
            result = await handle_remember(
                {
                    "context_id": str(uuid4()),
                    "summary": "a summary long enough",
                    "content": "c",
                    "type": "note",
                },
                "test_user",
                uuid4(),
            )
        return json.loads(result[0].text), mock_db, log_usage

    @pytest.mark.asyncio
    async def test_daily_quota_refusal_carries_structured_details_and_logs_429(self):
        from utils.exceptions import QuotaExceededError

        exc = QuotaExceededError(
            "Daily memory-creation quota exceeded. Limit: 50/day (free plan), "
            "created today: 50, requested: 1. Resets at 2026-09-19T00:00:00Z.",
            quota_type="memories_per_day",
            limit=50,
            used_today=50,
            requested=1,
            resets_at="2026-09-19T00:00:00Z",
        )

        payload, mock_db, log_usage = await self._remember_raising(exc)

        assert payload["status"] == "error"
        assert payload["error"] == "quota_exceeded"
        assert payload["quota_type"] == "memories_per_day"
        assert payload["resets_at"] == "2026-09-19T00:00:00Z"
        assert (payload["limit"], payload["used_today"], payload["requested"]) == (50, 50, 1)
        assert payload["message"].startswith("Daily memory-creation quota exceeded")
        mock_db.rollback.assert_awaited_once()
        # (db, user_id, tool, start_time, status_code, ...)
        assert log_usage.await_args.args[4] == 429

    @pytest.mark.asyncio
    async def test_total_count_refusal_uses_the_same_envelope_without_null_keys(self):
        """``check_memory_quota`` raises with no details — same envelope, and
        ``quota_type: null`` must not leak into the payload."""
        from utils.exceptions import QuotaExceededError

        payload, _db, log_usage = await self._remember_raising(
            QuotaExceededError("Memory quota exceeded. Current: 1000, Limit: 1000")
        )

        assert payload["error"] == "quota_exceeded"
        assert "quota_type" not in payload
        assert payload["message"].startswith("Memory quota exceeded")
        assert log_usage.await_args.args[4] == 429


class TestDispatchResponseModelErrorsStayLoud:
    @pytest.mark.asyncio
    async def test_response_model_validation_error_is_not_invalid_argument(self):
        """Review finding on #1323: a ValidationError from building a
        RESPONSE model (server data-integrity bug, e.g. a DB row that no
        longer fits the schema) must NOT be misattributed to the caller —
        it keeps the generic loud path."""
        import mcp_server.tools as tools_mod
        from mcp_server.tools import execute_tool_call

        def _response_model_error():
            from models.schemas import ReferenceResponse

            ReferenceResponse(memory_id="not-a-uuid")  # raises ValidationError

        async def broken_handler(args, user_id, workspace_id):
            _response_model_error()

        registry = tools_mod._build_registry()
        registry = dict(registry)
        registry["recall"] = broken_handler

        with (
            patch.object(tools_mod, "_TOOL_REGISTRY", registry),
            patch(
                "mcp_server.tools._check_rate_limit",
                new_callable=AsyncMock,
                return_value=(True, 0, 100),
            ),
        ):
            result = await execute_tool_call(
                "recall",
                {"context_id": str(uuid4()), "query": "q"},
                "test_user",
                uuid4(),
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload.get("error") != "invalid_argument"
        # #1622: the generic arm is flagged like every other envelope.
        assert isinstance(result, ToolErrorContent)


# ============================================================================
# #1622: the error marker, and the guard that keeps every envelope behind it
# ============================================================================


class TestErrorMarker:
    def test_error_response_is_a_marked_list_of_text_content(self):
        result = _error_response("not_found", "Memory not found", help="x")

        assert isinstance(result, ToolErrorContent)
        assert result.is_error is True
        # Still a plain list to every existing caller and test.
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].type == "text"
        assert result == [result[0]]
        assert json.loads(result[0].text) == {
            "status": "error",
            "error": "not_found",
            "message": "Memory not found",
            "help": "x",
        }

    def test_success_response_is_not_marked(self):
        result = _success_response(ok=True)
        assert not isinstance(result, ToolErrorContent)
        assert getattr(result, "is_error", False) is False

    def test_message_is_omitted_when_not_given(self):
        """The dispatch catch-alls ship ``{"status":"error","error":str(e)}``
        without a ``message``; routing them through the helper must not add
        one (the wire shape is frozen by the acceptance criteria)."""
        result = _error_response("boom")
        assert isinstance(result, ToolErrorContent)
        assert json.loads(result[0].text) == {"status": "error", "error": "boom"}


def _hand_built_error_envelopes(path: Path) -> list[tuple[str, int]]:
    """Return ``(enclosing_function, lineno)`` for each dict literal in
    ``path`` that spells out an error envelope (``"status": "error"``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "status"
                    and isinstance(value, ast.Constant)
                    and value.value == "error"
                ):
                    hits.append((func, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, "<module>")
    return hits


def test_no_hand_built_error_envelope_in_tools_package():
    """Every error envelope goes through ``_error_response``.

    The transports read the ``is_error`` marker that only the helper attaches;
    a hand-built ``{"status": "error", ...}`` payload would reach the client
    as a *successful* ``CallToolResult`` again (#1622). The only permitted
    literal is the one inside ``_helpers._error_response`` itself.
    """
    offenders = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        for func, lineno in _hand_built_error_envelopes(path):
            if path.name == "_helpers.py" and func == "_error_response":
                continue
            offenders.append(f"{path.name}:{lineno} (in {func})")
    assert not offenders, (
        "hand-built error envelope in mcp_server/tools — return _error_response(...) "
        "so the transport can set isError (#1622): " + ", ".join(offenders)
    )


def test_envelope_guard_detects_a_hand_built_literal(tmp_path):
    # The guard must actually see what it claims to forbid.
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def handler():\n"
        '    return {"status": "error", "error": "x"}\n'
        "def fine():\n"
        '    return {"status": "success"}\n'
        "def nested():\n"
        '    return [dict(text=_dumps({"status": "error"}))]\n'
    )
    assert _hand_built_error_envelopes(sample) == [("handler", 2), ("nested", 6)]
