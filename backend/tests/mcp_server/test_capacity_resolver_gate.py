"""The capacity-over lock after authorization on MCP (#1954).

``_capacity_gate`` splits the BLOCKED tools three ways: the service-gated
ones (their service checks), the resolver-gated ones (the context resolver
checks, reading the running tool's policy from a contextvar the dispatcher
sets) and the dispatcher-only ones (the caller's session workspace, before
the handler). This file pins the split and proves, tool by tool, that a
resolver-gated tool naming a context of a locked workspace is refused with the
same ``capacity_locked`` envelope the dispatcher built — and that a caller the
resolver turns away gets the not-found instead. The DB-backed twin is
``tests/integration/test_capacity_resolver_gate_db.py``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import mcp_server.tools as tools_mod
from mcp_server.tools import _build_registry, execute_tool_call
from mcp_server.tools import _capacity_gate as gate
from mcp_server.tools._capacity_gate import (
    CAPACITY_LOCK_BLOCKED_TOOLS,
    CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS,
    CAPACITY_LOCK_RESOLVER_GATED_TOOLS,
    CAPACITY_LOCK_SERVICE_GATED_TOOLS,
    capacity_policy_for,
)
from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._errors import describe_tool_exception
from mcp_server.tools._helpers import (
    ToolErrorContent,
    _mcp_capacity_policy,
    mcp_capacity_policy,
    reset_mcp_capacity_policy,
    set_mcp_capacity_policy,
)
from services.capacity_lock import CapacityPolicy
from utils.exceptions import CapacityLockedError, NotFoundException

TOOLS_DIR = Path(tools_mod.__file__).parent


def _locked() -> CapacityLockedError:
    return CapacityLockedError(
        memory_count=1012,
        memory_limit=1000,
        over_memories=12,
        used_bytes=0,
        storage_limit_bytes=100 * 1024 * 1024,
        over_bytes=0,
        cleanup_url="https://app.example.test/workspace/settings/plan",
    )


@pytest.fixture(autouse=True)
def _fresh_candidate_cache():
    gate.invalidate_capacity_candidate_cache()
    yield
    gate.invalidate_capacity_candidate_cache()


# ---------------------------------------------------------------------------
# (a) the partition
# ---------------------------------------------------------------------------


class TestThePartition:
    def test_blocked_is_service_resolver_and_dispatcher_only_without_overlap(self) -> None:
        service, resolver, dispatcher = (
            CAPACITY_LOCK_SERVICE_GATED_TOOLS,
            CAPACITY_LOCK_RESOLVER_GATED_TOOLS,
            CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS,
        )
        assert service | resolver | dispatcher == CAPACITY_LOCK_BLOCKED_TOOLS
        assert service.isdisjoint(resolver)
        assert service.isdisjoint(dispatcher)
        assert resolver.isdisjoint(dispatcher)

    def test_the_service_gated_tools_are_exactly_these(self) -> None:
        assert CAPACITY_LOCK_SERVICE_GATED_TOOLS == {
            "complete_file_upload",
            "create_context",
            "explore",
            "get_agent_bootstrap",
            "init_file_upload",
            "list_revisions",
            "load_pinned",
            "recall",
            "reference",
            "remember",
            "remember_batch",
            "update_memory",
        }

    def test_the_resolver_gated_tools_are_exactly_these(self) -> None:
        assert CAPACITY_LOCK_RESOLVER_GATED_TOOLS == {
            "bootstrap",
            "changes_since",
            "create_edge",
            "feedback",
            "get_sleep_history",
            "get_sleep_report",
            "get_state",
            "list_edges",
            "merge_contexts",
            "recall_nearby",
            "recall_series",
            "recall_upcoming",
            "record_measurement",
            "rollback_session",
            "rollback_sleep_run",
            "set_state",
            "update_edge",
            "update_search_config",
        }

    def test_the_dispatcher_only_tools_are_exactly_these(self) -> None:
        """Checked on the session workspace before the handler, as before #1954."""
        assert CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS == {
            "analyze_context",
            "get_active_analysis",
            "get_analysis",
            "get_cluster",
            "ingest_events",
            "list_analyses",
            "setup_connector",
            "setup_resource",
        }

    @pytest.mark.parametrize("tool", sorted(_build_registry()))
    def test_only_resolver_gated_tools_resolve_with_blocked(self, tool: str) -> None:
        expected = (
            CapacityPolicy.BLOCKED
            if tool in CAPACITY_LOCK_RESOLVER_GATED_TOOLS
            else CapacityPolicy.ALLOWED
        )
        assert capacity_policy_for(tool) is expected


# ---------------------------------------------------------------------------
# (b) every resolver-gated tool refuses a locked workspace, after authorization
# ---------------------------------------------------------------------------

CTX = uuid4()
OTHER_CTX = uuid4()
WS = uuid4()
REPORT = uuid4()

# Resolver-gated tools that name a Sleep report, not a context: the handler
# looks the report up, then resolves its context (section (b2) below).
REPORT_TOOLS = frozenset({"get_sleep_report", "rollback_sleep_run"})
CONTEXT_NAMED_TOOLS = CAPACITY_LOCK_RESOLVER_GATED_TOOLS - REPORT_TOOLS

# Minimal valid arguments: each call gets as far as its context resolver.
RESOLVER_ARGS: dict[str, dict[str, Any]] = {
    "bootstrap": {"context_id": str(CTX)},
    "changes_since": {"context_id": str(CTX), "since": "2026-01-01T00:00:00Z"},
    "create_edge": {"context_id": str(CTX), "source_id": str(uuid4()), "target_id": str(uuid4())},
    "feedback": {"context_id": str(CTX), "memory_id": str(uuid4()), "helpful": True},
    "get_sleep_history": {"context_id": str(CTX)},
    "get_state": {"context_id": str(CTX)},
    "list_edges": {"context_id": str(CTX), "memory_id": str(uuid4())},
    "merge_contexts": {"source_context_id": str(CTX), "target_context_id": str(OTHER_CTX)},
    "recall_nearby": {"context_id": str(CTX), "lat": 35.0, "lon": 139.0},
    "recall_series": {"context_id": str(CTX), "metric": "weight"},
    "recall_upcoming": {"context_id": str(CTX)},
    "record_measurement": {"context_id": str(CTX), "metric": "weight", "value": 1.5},
    "rollback_session": {"context_id": str(CTX), "session_id": "s-1", "dry_run": True},
    "set_state": {"context_id": str(CTX), "key": "k", "value": "v"},
    "update_edge": {
        "context_id": str(CTX),
        "source_id": str(uuid4()),
        "target_id": str(uuid4()),
        "weight": 0.5,
    },
    "update_search_config": {"context_id": str(CTX), "semantic_weight": 0.5},
}


def test_every_resolver_gated_tool_has_arguments_here() -> None:
    assert set(RESOLVER_ARGS) == CONTEXT_NAMED_TOOLS
    assert REPORT_TOOLS <= CAPACITY_LOCK_RESOLVER_GATED_TOOLS


def _context(context_id=CTX) -> SimpleNamespace:
    return SimpleNamespace(
        id=context_id,
        workspace_id=WS,
        name="ctx",
        display_name="Ctx",
        summary=None,
        usage_guide=None,
        is_private=False,
        is_locked=False,
        is_public=False,
        created_by="u1",
    )


def _fake_db() -> MagicMock:
    db = MagicMock()
    for name in ("execute", "scalar", "scalars", "get", "commit", "rollback", "flush", "close"):
        setattr(db, name, AsyncMock())
    return db


class _Resolvers:
    """Fakes for every context resolver an MCP handler reaches.

    Each answers like the real one once authorization passed: the context, or
    the lock when the running policy is BLOCKED. ``deny`` makes them refuse
    the caller instead (a context the caller may not reach).
    """

    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.lock = AsyncMock(side_effect=self._lock)
        self.policies: list[CapacityPolicy] = []

    async def _lock(self, _db, _workspace_id, **_kwargs) -> None:
        # ``_helpers.ensure_not_capacity_locked``: only reached under BLOCKED.
        assert mcp_capacity_policy() is CapacityPolicy.BLOCKED
        raise _locked()

    async def resolve_read(self, _self, *, context_id, capacity, **_kwargs):
        self.policies.append(capacity)
        if self.deny:
            raise NotFoundException("Context", str(context_id))
        if capacity is not CapacityPolicy.ALLOWED:
            raise _locked()
        return _context(context_id)

    async def check_write(self, _self, _user_id, context_id, *, capacity, key_workspace_id=None):
        self.policies.append(capacity)
        if self.deny:
            raise NotFoundException("Context", str(context_id))
        if capacity is not CapacityPolicy.ALLOWED:
            raise _locked()
        return _context(context_id)

    async def get_context(self, _self, _user_id, context_id):
        if self.deny:
            raise NotFoundException("Context", str(context_id))
        return _context(context_id)

    def patches(self, db: MagicMock):
        async def _gen():
            yield db

        return (
            patch("db.base.get_db", lambda: _gen()),
            patch(
                "services.permission_service.PermissionService.resolve_context_for_workspace_read",
                new=lambda s, **kw: self.resolve_read(s, **kw),
            ),
            patch(
                "services.permission_service.PermissionService.check_context_write",
                new=lambda s, u, c, **kw: self.check_write(s, u, c, **kw),
            ),
            patch(
                "services.context_service.ContextService.get_context",
                new=lambda s, u, c: self.get_context(s, u, c),
            ),
            patch("mcp_server.tools._helpers.ensure_not_capacity_locked", self.lock),
            # The write role a resolver checks before the lock: an owner.
            patch(
                "mcp_server.tools._helpers._get_workspace_member_role",
                AsyncMock(return_value="owner"),
            ),
        )


async def _call(tool: str, resolvers: _Resolvers, db: MagicMock, usage: AsyncMock | None = None):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in resolvers.patches(db):
            stack.enter_context(p)
        # The usage row (some handlers log the refusal) is not handler work.
        stack.enter_context(patch("utils.usage_logger.log_usage", usage or AsyncMock()))
        # No session workspace: the dispatcher's own check has nothing to look at.
        # That it also skips a session workspace when a target is named is pinned
        # in test_capacity_gate.py (test_a_gated_tool_naming_its_target_opens_no_session).
        return await execute_tool_call(tool, dict(RESOLVER_ARGS[tool]), "u1", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(CONTEXT_NAMED_TOOLS))
async def test_a_resolver_gated_tool_refuses_a_locked_workspace(tool: str) -> None:
    """The envelope is byte-identical to the one the dispatcher built, and the
    handler stops at its resolver: nothing is read or written after it."""
    db = _fake_db()
    resolvers = _Resolvers()
    usage = AsyncMock(return_value=True)
    result = await _call(tool, resolvers, db, usage)

    expected = describe_tool_exception(tool, _locked()).response()
    assert isinstance(result, ToolErrorContent)
    assert [c.text for c in result] == [c.text for c in expected], result[0].text
    assert json.loads(result[0].text)["error"] == "capacity_locked"
    assert resolvers.policies or resolvers.lock.await_count, "no resolver was reached"
    assert all(p is CapacityPolicy.BLOCKED for p in resolvers.policies)
    for name in ("execute", "scalar", "scalars", "get", "flush"):
        assert getattr(db, name).await_count == 0, f"{tool}: db.{name} after the refusal"
    # A logged refusal is never a success row.
    assert all(c.kwargs["status_code"] != 200 for c in usage.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(CONTEXT_NAMED_TOOLS))
async def test_a_caller_the_resolver_turns_away_never_sees_the_lock(tool: str) -> None:
    """D1: a context the caller cannot reach answers with the handler's own
    refusal (context_not_found), never ``capacity_locked``, and the lock is
    never consulted."""
    resolvers = _Resolvers(deny=True)
    result = await _call(tool, resolvers, _fake_db())

    body = json.loads(result[0].text)
    assert body["error"] == "context_not_found", body
    resolvers.lock.assert_not_awaited()


# ---------------------------------------------------------------------------
# (b2) the Sleep report tools: the lock of the report's workspace
# ---------------------------------------------------------------------------


def _report_db() -> MagicMock:
    """A session whose first query finds the caller's report in ``CTX``."""
    db = _fake_db()
    report = SimpleNamespace(
        id=REPORT, user_id="u1", context_id=CTX, workspace_id=WS, status="completed"
    )
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=report))
    )
    return db


async def _call_report_tool(tool: str, resolvers: _Resolvers, db: MagicMock):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in resolvers.patches(db):
            stack.enter_context(p)
        stack.enter_context(patch("utils.usage_logger.log_usage", AsyncMock()))
        return await execute_tool_call(tool, {"report_id": str(REPORT)}, "u1", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(REPORT_TOOLS))
async def test_a_report_tool_refuses_a_locked_workspace_after_the_report_lookup(tool) -> None:
    db = _report_db()
    resolvers = _Resolvers()
    result = await _call_report_tool(tool, resolvers, db)

    expected = describe_tool_exception(tool, _locked()).response()
    assert [c.text for c in result] == [c.text for c in expected], result[0].text
    assert resolvers.policies and all(p is CapacityPolicy.BLOCKED for p in resolvers.policies)
    # Only the report lookup ran before the refusal.
    assert db.execute.await_count == 1
    for name in ("scalar", "scalars", "get", "flush", "commit"):
        assert getattr(db, name).await_count == 0, f"{tool}: db.{name} after the refusal"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(REPORT_TOOLS))
async def test_a_report_tool_turned_away_answers_report_not_found(tool) -> None:
    resolvers = _Resolvers(deny=True)
    result = await _call_report_tool(tool, resolvers, _report_db())
    body = json.loads(result[0].text)
    assert body["error"] == "report_not_found", body
    resolvers.lock.assert_not_awaited()


# ---------------------------------------------------------------------------
# (c) the real resolver denies first: the lock is never awaited
# ---------------------------------------------------------------------------


class TestTheRealResolverOrder:
    @pytest.mark.asyncio
    async def test_get_state_on_an_unreadable_context_is_not_found(self) -> None:
        """``resolve_context_for_workspace_read`` loads the context, finds
        nothing, and never reaches its capacity step."""
        db = _fake_db()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        lock = AsyncMock(side_effect=_locked())

        async def _gen():
            yield db

        with (
            patch("db.base.get_db", lambda: _gen()),
            patch("services.permission_service.ensure_not_capacity_locked", lock),
        ):
            result = await execute_tool_call("get_state", {"context_id": str(CTX)}, "u1", None)
        assert json.loads(result[0].text)["error"] == "context_not_found"
        lock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_edge_on_an_unreadable_context_is_not_found(self) -> None:
        """``_resolve_context`` → ``ContextService.get_context`` refuses before
        the lock step."""
        db = _fake_db()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        lock = AsyncMock(side_effect=_locked())

        async def _gen():
            yield db

        with (
            patch("db.base.get_db", lambda: _gen()),
            patch("mcp_server.tools._helpers.ensure_not_capacity_locked", lock),
        ):
            result = await execute_tool_call(
                "create_edge", dict(RESOLVER_ARGS["create_edge"]), "u1", None
            )
        assert json.loads(result[0].text)["error"] == "context_not_found"
        lock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_context_checks_the_lock_last_under_blocked_only(self) -> None:
        from mcp_server.tools._helpers import _resolve_context

        db = _fake_db()
        lock = AsyncMock()
        with (
            patch(
                "services.context_service.ContextService.get_context",
                new=AsyncMock(return_value=_context()),
            ),
            patch("mcp_server.tools._helpers.ensure_not_capacity_locked", lock),
        ):
            token = set_mcp_capacity_policy(CapacityPolicy.ALLOWED)
            try:
                await _resolve_context(db, "u1", CTX)
            finally:
                reset_mcp_capacity_policy(token)
            lock.assert_not_awaited()

            token = set_mcp_capacity_policy(CapacityPolicy.BLOCKED)
            try:
                await _resolve_context(db, "u1", CTX)
            finally:
                reset_mcp_capacity_policy(token)
            lock.assert_awaited_once_with(db, WS)

            lock.reset_mock()
            # Unset: a handler called outside the dispatcher fails closed.
            assert _mcp_capacity_policy.get() is None
            await _resolve_context(db, "u1", CTX)
            lock.assert_awaited_once_with(db, WS)


# ---------------------------------------------------------------------------
# (d) the contextvar: set per tool, reset after the call
# ---------------------------------------------------------------------------


def _schema_args(tool: str) -> dict[str, Any]:
    props = next(
        d["inputSchema"].get("properties", {}) for d in get_tool_definitions() if d["name"] == tool
    )
    return {"context_id": str(CTX)} if "context_id" in props else {}


@pytest.fixture
def stub_registry(monkeypatch):
    seen: dict[str, CapacityPolicy | None] = {}

    def stub(name):
        async def handler(_args, _user_id, _workspace_id):
            seen[name] = _mcp_capacity_policy.get()
            if name == "get_state":
                raise RuntimeError("handler failed")
            return [tools_mod.TextContent(type="text", text='{"status":"success"}')]

        return handler

    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", {n: stub(n) for n in _build_registry()})
    monkeypatch.setattr(gate, "capacity_lock_refusal", AsyncMock(return_value=None))
    return seen


class TestThePolicyContextvar:
    def test_unset_and_stray_values_read_as_blocked(self) -> None:
        assert _mcp_capacity_policy.get() is None
        assert mcp_capacity_policy() is CapacityPolicy.BLOCKED
        token = _mcp_capacity_policy.set("allowed")  # type: ignore[arg-type]
        try:
            assert mcp_capacity_policy() is CapacityPolicy.BLOCKED
        finally:
            _mcp_capacity_policy.reset(token)

    @pytest.mark.asyncio
    async def test_every_tool_runs_under_its_own_policy(self, stub_registry) -> None:
        for tool in sorted(_build_registry()):
            await execute_tool_call(tool, _schema_args(tool), "u1", None)
            assert _mcp_capacity_policy.get() is None, f"{tool} leaked its policy"
        assert set(stub_registry) == set(_build_registry())
        for tool, policy in stub_registry.items():
            assert policy is capacity_policy_for(tool), tool

    @pytest.mark.asyncio
    async def test_the_policy_is_reset_after_a_raising_handler(self, stub_registry) -> None:
        outer = set_mcp_capacity_policy(CapacityPolicy.ALLOWED)
        try:
            result = await execute_tool_call("get_state", {"context_id": str(CTX)}, "u1", None)
            assert json.loads(result[0].text)["error"] == "internal_error"
            assert stub_registry["get_state"] is CapacityPolicy.BLOCKED
            assert _mcp_capacity_policy.get() is CapacityPolicy.ALLOWED
        finally:
            reset_mcp_capacity_policy(outer)


# ---------------------------------------------------------------------------
# (e) no tool module decides the policy itself
# ---------------------------------------------------------------------------


def _tool_modules():
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.name in ("_helpers.py", "_capacity_gate.py"):
            continue
        yield path, ast.parse(path.read_text())


def test_no_tool_module_names_a_capacity_policy() -> None:
    """The table in ``_capacity_gate`` is the only MCP decision: a literal
    ``CapacityPolicy.ALLOWED`` in a handler would skip the lock for a tool the
    table blocks."""
    offenders = []
    for path, tree in _tool_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "CapacityPolicy":
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.alias) and node.name == "CapacityPolicy":
                offenders.append(f"{path.name}: import")
    assert not offenders, offenders


def test_every_capacity_argument_in_a_tool_module_is_the_running_policy() -> None:
    offenders = []
    for path, tree in _tool_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "capacity":
                    continue
                v = kw.value
                if not (
                    isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id == "mcp_capacity_policy"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


def test_no_read_resolver_call_raises_the_role() -> None:
    """BLOCKED on ``_resolve_context_for_read`` adds only the lock while the
    role stays the viewer default; a writer role there would pull the #1943
    slot check ahead of the tools' own role gates."""
    offenders = []
    for path, tree in _tool_modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_resolve_context_for_read"
                and any(kw.arg == "required_role" for kw in node.keywords)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# (f) a dispatcher-only tool checks the session workspace, never the named one
# ---------------------------------------------------------------------------


class TestDispatcherOnly:
    @pytest.mark.asyncio
    async def test_a_named_context_does_not_move_the_check_off_the_session_workspace(
        self,
    ) -> None:
        from sqlalchemy.dialects import postgresql

        session_ws = uuid4()
        db = MagicMock()
        db.scalars = AsyncMock(return_value=MagicMock(all=lambda: [SimpleNamespace(id=session_ws)]))
        lock = SimpleNamespace(to_error=_locked)

        async def _gen():
            yield db

        with (
            patch("db.base.get_db", lambda: _gen()),
            patch("services.capacity_lock.capacity_lock_state", AsyncMock(return_value=lock)),
        ):
            result = await gate.capacity_lock_refusal(
                "analyze_context", {"context_id": str(CTX)}, session_ws
            )
        assert result is not None
        assert json.loads(result[0].text)["error"] == "capacity_locked"
        compiled = db.scalars.await_args.args[0].compile(dialect=postgresql.dialect())
        assert session_ws in compiled.params.values()
        assert CTX not in compiled.params.values()
        sql = str(compiled)
        assert sql.count("FROM") == 1 and "FROM workspaces" in sql, sql

    @pytest.mark.asyncio
    async def test_without_a_session_workspace_nothing_is_checked(self) -> None:
        def _get_db():
            raise AssertionError("no session without a session workspace")

        with patch("db.base.get_db", _get_db):
            assert (
                await gate.capacity_lock_refusal("analyze_context", {"context_id": str(CTX)}, None)
                is None
            )
