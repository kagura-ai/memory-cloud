"""#1943: the MCP write tools ask their resolver for a writable slot.

An AST scan of each handler, so a refactor that drops ``access="write"`` fails
here instead of silently leaving an MCP write ungated while its REST twin is.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from mcp_server.tools import edge, feedback, measurement, state

WRITE_TOOLS = {
    state.handle_set_state: "_resolve_context_for_read",
    measurement.handle_record_measurement: "_resolve_context_for_read",
    feedback.handle_feedback: "_resolve_context_for_read",
    edge.handle_create_edge: "_resolve_context",
    edge.handle_update_edge: "_resolve_context",
}
READ_TOOLS = {
    state.handle_get_state: "_resolve_context_for_read",
    measurement.handle_recall_series: "_resolve_context_for_read",
    edge.handle_list_edges: "_resolve_context_for_read",
    edge.handle_delete_edge: "_resolve_context",
}


def _resolver_access(handler, resolver: str) -> set[str | None]:
    tree = ast.parse(inspect.getsource(inspect.unwrap(handler)))
    found: set[str | None] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == resolver:
            access = next((kw.value.value for kw in node.keywords if kw.arg == "access"), None)
            found.add(access)
    return found


@pytest.mark.parametrize(
    ("handler", "resolver"), WRITE_TOOLS.items(), ids=lambda h: getattr(h, "__name__", str(h))
)
def test_write_tools_resolve_with_write_access(handler, resolver) -> None:
    assert _resolver_access(handler, resolver) == {"write"}


@pytest.mark.parametrize(
    ("handler", "resolver"), READ_TOOLS.items(), ids=lambda h: getattr(h, "__name__", str(h))
)
def test_read_and_delete_tools_do_not(handler, resolver) -> None:
    assert "write" not in _resolver_access(handler, resolver)
