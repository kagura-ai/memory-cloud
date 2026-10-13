"""#1951: a merged copy belongs to the target context's workspace.

``merge_contexts`` used to copy ``mem.workspace_id`` from the source row. A
legacy row with a NULL workspace would have been duplicated as another NULL,
and a cross-workspace merge would have filed the copy under the wrong tenant.
The copy is created in the target context, so its workspace is the target's.

Pinned at the AST level, like ``test_context_merge_slot.py``: ``merge_contexts``
also copies Qdrant points and search configs, so a behavioural run needs the
vector store, and the one fact under test is which workspace the ``Memory(...)``
copy is built with.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from services.context_service import ContextService


def _memory_constructor_kwargs() -> dict[str, ast.expr]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(ContextService.merge_contexts)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Memory"
    ]
    # Exactly one construction: a second one would make "the copy" ambiguous
    # and this pin would silently inspect the wrong call.
    assert len(calls) == 1, f"expected one Memory(...) in merge_contexts, found {len(calls)}"
    return {kw.arg: kw.value for kw in calls[0].keywords if kw.arg}


def test_merged_copy_takes_the_target_workspace() -> None:
    kwargs = _memory_constructor_kwargs()
    ws = kwargs["workspace_id"]
    assert isinstance(ws, ast.Attribute)
    assert ws.attr == "workspace_id"
    assert isinstance(ws.value, ast.Name) and ws.value.id == "target"


def test_merged_copy_is_filed_under_the_target_context() -> None:
    kwargs = _memory_constructor_kwargs()
    ctx = kwargs["context_id"]
    assert isinstance(ctx, ast.Name) and ctx.id == "target_context_id"
