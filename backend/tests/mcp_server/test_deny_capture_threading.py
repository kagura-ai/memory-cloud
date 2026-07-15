"""#1286 (P0-5): pin the MCP handlers' MAE audit-identity threading.

Each memory-op handler must pass its ``operation`` into the context pre-gate
(``_resolve_context`` for writes, ``_resolve_context_for_read`` for reads).
A dropped ``operation=`` silently reverts enforce-mode denies on that handler
to log-only — the pre-gate raises before any service-layer gate runs, so its
emission is the ONLY record (the review finding on the #1291/#1292 parity
class). The pre-gate emission semantics themselves are pinned in
``test_agent_binding_service`` / ``test_permission_agent_binding``; here we
pin only that every handler threads the right vocabulary value.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools import feedback as feedback_tools
from mcp_server.tools import memory as memory_tools
from mcp_server.tools._helpers import _ContextNotFoundError


async def _invoke_with_denying_pre_gate(module, handler_name, args, helper_name):
    """Run a handler with the named pre-gate helper raising the uniform deny;
    return the helper mock so the caller can assert the threaded kwargs."""
    resolver = AsyncMock(side_effect=_ContextNotFoundError(uuid4(), "denied"))

    async def gen():
        yield AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", new=gen))
        for maybe in ("_log_tool_usage", "_check_viewer_permission"):
            if hasattr(module, maybe):
                stack.enter_context(
                    patch(f"{module.__name__}.{maybe}", new=AsyncMock(return_value=None))
                )
        stack.enter_context(patch(f"{module.__name__}.{helper_name}", new=resolver))
        handler = getattr(module, handler_name)
        try:
            await handler(args=args, user_id="u", workspace_id=uuid4())
        except Exception:  # noqa: BLE001 — envelope vs raise is not under test
            pass
    return resolver


_CTX = str(uuid4())
_MID = str(uuid4())

CASES = [
    (
        memory_tools,
        "handle_recall",
        {"query": "q", "context_id": _CTX},
        "_resolve_context_for_read",
        "recall",
    ),
    (
        memory_tools,
        "handle_load_pinned",
        {"context_id": _CTX},
        "_resolve_context_for_read",
        "load_pinned",
    ),
    (
        memory_tools,
        "handle_reference",
        {"memory_id": _MID, "context_id": _CTX},
        "_resolve_context_for_read",
        "reference",
    ),
    (
        feedback_tools,
        "handle_feedback",
        {"context_id": _CTX, "memory_id": _MID, "helpful": True},
        "_resolve_context_for_read",
        "feedback",
    ),
    (
        memory_tools,
        "handle_remember",
        {
            "summary": "a searchable summary for the pin",
            "content": "c",
            "type": "note",
            "context_id": _CTX,
        },
        "_resolve_context",
        "remember",
    ),
    (
        memory_tools,
        "handle_update_memory",
        {"memory_id": _MID, "context_id": _CTX, "tags": ["x"]},
        "_resolve_context",
        "update",
    ),
    (
        memory_tools,
        "handle_forget",
        {"memory_id": _MID, "context_id": _CTX},
        "_resolve_context",
        "forget",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module, handler_name, args, helper_name, expected_op",
    CASES,
    ids=[c[4] if c[1] != "handle_update_memory" else "update" for c in CASES],
)
async def test_handler_threads_operation_into_pre_gate(
    module, handler_name, args, helper_name, expected_op
):
    resolver = await _invoke_with_denying_pre_gate(module, handler_name, dict(args), helper_name)
    assert resolver.await_count >= 1, f"{handler_name} never reached {helper_name}"
    assert resolver.await_args.kwargs.get("operation") == expected_op
