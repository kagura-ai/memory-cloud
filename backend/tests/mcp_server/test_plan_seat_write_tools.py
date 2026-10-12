"""#1943: the MCP content-write tools ask for a writable slot — after the role gate.

An AST scan of each handler: the slot check (``ensure_context_writable``) is
present and comes after ``_check_viewer_permission`` (feedback has no role
gate, so after its resolver), and the read / delete tools never call it. A
refactor that drops the call or moves it ahead of authorization fails here,
instead of silently leaving an MCP write ungated — or letting a viewer learn a
context's slot state before being told it cannot write.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from mcp_server.tools import edge, feedback, measurement, state

WRITE_TOOLS = {
    state.handle_set_state: "_check_viewer_permission",
    measurement.handle_record_measurement: "_check_viewer_permission",
    edge.handle_create_edge: "_check_viewer_permission",
    edge.handle_update_edge: "_check_viewer_permission",
    feedback.handle_feedback: "_resolve_context_for_read",
}
READ_TOOLS = (
    state.handle_get_state,
    measurement.handle_recall_series,
    edge.handle_list_edges,
    edge.handle_delete_edge,
)


def _call_lines(handler, name: str) -> list[int]:
    tree = ast.parse(inspect.getsource(inspect.unwrap(handler)))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == name
    ]


@pytest.mark.parametrize(
    ("handler", "gate"), WRITE_TOOLS.items(), ids=lambda h: getattr(h, "__name__", str(h))
)
def test_write_tools_check_the_slot_after_the_gate(handler, gate: str) -> None:
    slot = _call_lines(handler, "ensure_context_writable")
    gates = _call_lines(handler, gate)
    assert len(slot) == 1, "one slot check"
    assert gates, f"{gate} not found"
    assert slot[0] > max(gates), "the slot check must come after authorization"


@pytest.mark.parametrize("handler", READ_TOOLS, ids=lambda h: h.__name__)
def test_read_and_delete_tools_never_ask_for_a_slot(handler) -> None:
    assert _call_lines(handler, "ensure_context_writable") == []
