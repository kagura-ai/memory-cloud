"""#1943: ``merge_contexts`` is cleanup, not a content write.

A merge retires its source, so it only ever brings the workspace closer to
its context cap — it never asks whether the target holds a writable slot
(the capacity lock treats it the same way: ``CapacityPolicy.ALLOWED``). An
over-cap owner can therefore consolidate in either direction.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from services.context_service import ContextService


def test_merge_never_asks_for_a_slot() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(ContextService.merge_contexts)))
    called = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "ensure_context_writable" not in called
    assert "check_context_owner" in called  # owner-only stays
