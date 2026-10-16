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
    "recall_upcoming",
    "recall_nearby",
]

# _resolve_context_for_read call sites that deliberately thread no operation:
# they do not read memory rows, so they sit outside the memory_access_events
# vocabulary and their binding denies stay log-only. Keyed (module, function).
UNAUDITED_READ_SITES = {
    # Context metadata (stats / settings), not memory content.
    ("context.py", "handle_get_context_info"),
    # Owner-level context administration; both contexts are re-checked by the
    # merge service, which is not a memory read.
    ("context.py", "handle_merge_contexts"),
    # Graph edges between memories, not memory rows.
    ("edge.py", "handle_list_edges"),
    # Agent key/value state lives in its own table, not in memories.
    ("state.py", "handle_set_state"),
    ("state.py", "handle_get_state"),
    # Measurement series live in their own table, not in memories.
    ("measurement.py", "handle_record_measurement"),
    ("measurement.py", "handle_recall_series"),
}


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


def _resolver_calls():
    """Yield (path, enclosing function, call) for every ``_resolve_context*``
    call under mcp_server/tools."""
    for path in sorted(_TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = _callee_name(node.func)
                if name is not None and name.startswith("_resolve_context"):
                    yield path, fn.name, node, name


def test_every_threaded_operation_is_in_the_mae_vocabulary():
    """Every ``operation=`` passed to a ``_resolve_context*`` pre-gate must be a
    string literal in MAE_OPERATIONS — a computed value cannot be checked
    statically, so it fails here too."""
    found: set[tuple[str, str]] = set()
    problems: list[str] = []
    for path, _fn, call, _name in _resolver_calls():
        for kw in call.keywords:
            if kw.arg != "operation":
                continue
            where = f"{path.name}:{kw.value.lineno}"
            value = kw.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.add((value.value, where))
                if value.value not in MAE_OPERATIONS:
                    problems.append(f"{where}: {value.value!r} is not in MAE_OPERATIONS")
            else:
                problems.append(f"{where}: operation= is not a string literal")
    # The scan must actually see every read lane, or it passes vacuously.
    assert set(READ_OPERATIONS) <= {op for op, _ in found}, sorted(found)
    assert not problems, "\n".join(problems)


def test_every_memory_read_pre_gate_threads_an_operation():
    """A read pre-gate with no ``operation=`` returns the uniform 404 on an
    agent deny but writes no audit row. Only the allowlisted sites may do so."""
    missing: list[str] = []
    seen_sites: set[tuple[str, str]] = set()
    for path, fn, call, name in _resolver_calls():
        if name != "_resolve_context_for_read":
            continue
        if any(kw.arg == "operation" for kw in call.keywords):
            continue
        site = (path.name, fn)
        seen_sites.add(site)
        if site not in UNAUDITED_READ_SITES:
            missing.append(f"{path.name}:{call.lineno} in {fn}")
    assert not missing, "read pre-gate without operation=: " + ", ".join(missing)
    # A stale allowlist entry hides nothing today but would excuse a regression.
    assert seen_sites == UNAUDITED_READ_SITES, UNAUDITED_READ_SITES - seen_sites


@pytest.mark.asyncio
async def test_vocabulary_gap_keeps_the_enforce_deny_and_logs():
    """An operation missing from the vocabulary loses its row, never the deny:
    the request still gets the uniform 404 and the gap is logged at ERROR."""
    set_agent_scope(_scope("enforce"))
    db = _unbound_db()
    context = SimpleNamespace(id=uuid4(), is_private=False)
    with (
        _get_db(db),
        _patched_resolver(context),
        patch("services.memory_access_event_writer.logger.error") as error,
        pytest.raises(_ContextNotFoundError),
    ):
        await _resolve_context_for_read(db, "u", context.id, operation="not_an_operation")
    assert _audit_rows(db) == []
    error.assert_called_once()
    assert error.call_args.args[0] == "mae_vocabulary_gap"
    assert error.call_args.kwargs["operation"] == "not_an_operation"


@pytest.mark.asyncio
async def test_vocabulary_gap_lets_a_shadow_request_proceed():
    set_agent_scope(_scope("shadow"))
    db = _unbound_db()
    context = SimpleNamespace(id=uuid4(), is_private=False)
    with (
        _get_db(db),
        _patched_resolver(context),
        patch("services.memory_access_event_writer.logger.error") as error,
    ):
        resolved = await _resolve_context_for_read(
            db, "u", context.id, operation="not_an_operation"
        )
    assert resolved is context
    assert _audit_rows(db) == []
    assert error.call_args.args[0] == "mae_vocabulary_gap"
