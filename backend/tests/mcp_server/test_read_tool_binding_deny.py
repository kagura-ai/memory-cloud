"""#2007: an agent-binding deny on every MCP read tool is the uniform 404.

``list`` / ``changes_since`` threaded ``operation="list"`` / ``"changes_since"``
into the read pre-gate, but neither value was in ``MAE_OPERATIONS``: the
deny-capture writer's enum check raised ``ValueError`` and the handler mapped
it to ``validation_error`` instead of ``context_not_found`` (shadow mode broke
the same way instead of proceeding). These tests run the REAL binding
evaluation and the REAL audit writer (only the DB session is a mock) so a
vocabulary gap surfaces as the failure it causes, plus a static scan so the
next read tool cannot thread an unknown operation.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from auth.agent_scope import AgentScope, set_agent_scope
from mcp_server.tools._helpers import _ContextNotFoundError, _resolve_context_for_read
from models.memory_access_event import MAE_OPERATIONS, MemoryAccessEvent
from services.permission_service import PermissionService

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "src" / "mcp_server" / "tools"
# Every read operation the MCP read tools thread into _resolve_context_for_read.
READ_OPERATIONS = [
    "recall",
    "reference",
    "load_pinned",
    "load_guardrails",
    "bootstrap",
    "feedback",
    "explore",
    "list",
    "changes_since",
]


@pytest.fixture(autouse=True)
def _clean_scope():
    set_agent_scope(None)
    yield
    set_agent_scope(None)


def _unbound_db() -> MagicMock:
    """A session whose binding lookup finds no binding (unbound context) and
    that accepts the audit writer's add + commit."""
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _patched_resolver(context: SimpleNamespace):
    """resolve_context_for_workspace_read reduced to its binding gate: the real
    _apply_agent_binding_filter (real AgentBindingService + real MAE writer),
    then the context. The RBAC half is not under test here."""

    async def resolve(self, *, user_id, context_id, operation=None, **_kw):
        await self._apply_agent_binding_filter(
            context_id, access="read", user_id=user_id, operation=operation
        )
        return context

    return patch.object(PermissionService, "resolve_context_for_workspace_read", resolve)


def _audit_rows(db: MagicMock) -> list[MemoryAccessEvent]:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], MemoryAccessEvent)]


def _scope(mode: str) -> AgentScope:
    return AgentScope(agent_id=uuid4(), enforcement_mode=mode, workspace_id=uuid4())


def _get_db(db: MagicMock):
    async def gen():
        yield db

    return patch("db.base.get_db", new=gen)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READ_OPERATIONS)
async def test_enforce_deny_is_uniform_not_found_with_a_denied_row(operation):
    set_agent_scope(_scope("enforce"))
    db = _unbound_db()
    context = SimpleNamespace(id=uuid4(), is_private=False)
    with _get_db(db), _patched_resolver(context), pytest.raises(_ContextNotFoundError):
        await _resolve_context_for_read(db, "u", context.id, operation=operation)
    (row,) = _audit_rows(db)
    assert (row.operation, row.outcome, row.policy_decision) == (
        operation,
        "denied",
        "binding_denied",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", READ_OPERATIONS)
async def test_shadow_proceeds_and_records_would_deny(operation):
    set_agent_scope(_scope("shadow"))
    db = _unbound_db()
    context = SimpleNamespace(id=uuid4(), is_private=False)
    with _get_db(db), _patched_resolver(context):
        resolved = await _resolve_context_for_read(db, "u", context.id, operation=operation)
    assert resolved is context
    (row,) = _audit_rows(db)
    assert (row.operation, row.outcome, row.policy_decision) == (
        operation,
        "success",
        "would_deny",
    )


_HANDLER_ARGS = {
    "list": {},
    "changes_since": {"since": "2026-10-01T00:00:00Z"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_HANDLER_ARGS))
async def test_list_and_changes_since_enforce_deny_envelope(name):
    from mcp_server.tools import listing

    set_agent_scope(_scope("enforce"))
    db = _unbound_db()
    context_id = uuid4()
    handler = getattr(listing, f"handle_{name}")
    with (
        _get_db(db),
        _patched_resolver(SimpleNamespace(id=context_id, is_private=False)),
        patch("mcp_server.tools.listing._log_tool_usage", new=AsyncMock()),
    ):
        (block,) = await handler(
            {"context_id": str(context_id), **_HANDLER_ARGS[name]}, "u", uuid4()
        )
    payload = json.loads(block.text)
    assert payload["status"] == "error"
    assert payload["error"] == "context_not_found"
    assert UUID(payload["context_id"]) == context_id
    (row,) = _audit_rows(db)
    assert row.operation == name


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_every_threaded_operation_is_in_the_mae_vocabulary():
    """Every ``operation=`` passed to a ``_resolve_context*`` pre-gate must be a
    string literal in MAE_OPERATIONS — a computed value cannot be checked
    statically, so it fails here too."""
    found: list[tuple[str, str]] = []
    problems: list[str] = []
    for path in sorted(_TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee_name(node.func)
            if name is None or not name.startswith("_resolve_context"):
                continue
            for kw in node.keywords:
                if kw.arg != "operation":
                    continue
                where = f"{path.name}:{kw.value.lineno}"
                value = kw.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.append((value.value, where))
                    if value.value not in MAE_OPERATIONS:
                        problems.append(f"{where}: {value.value!r} is not in MAE_OPERATIONS")
                else:
                    problems.append(f"{where}: operation= is not a string literal")
    # The scan must actually see the call sites, or it passes vacuously.
    assert len(found) >= 8, found
    assert {"list", "changes_since"} <= {op for op, _ in found}
    assert not problems, "\n".join(problems)
