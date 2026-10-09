"""The capacity-over lock at the MCP dispatcher (#1941)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools import _build_registry, execute_tool_call
from mcp_server.tools import _capacity_gate as gate
from mcp_server.tools._capacity_gate import (
    CAPACITY_LOCK_ALLOWED_TOOLS,
    CAPACITY_LOCK_BLOCKED_TOOLS,
)
from utils.exceptions import CapacityLockedError


def _locked(**over: object) -> CapacityLockedError:
    fields: dict = {
        "memory_count": 1012,
        "memory_limit": 1000,
        "over_memories": 12,
        "used_bytes": 0,
        "storage_limit_bytes": 100 * 1024 * 1024,
        "over_bytes": 0,
        "cleanup_url": "https://app.example.test/workspace/settings/plan",
    }
    fields.update(over)
    return CapacityLockedError(**fields)


class TestTheClassification:
    def test_the_two_sets_cover_exactly_the_registry(self) -> None:
        """A new tool cannot ship without deciding whether a locked workspace keeps it."""
        registry = set(_build_registry())
        assert CAPACITY_LOCK_BLOCKED_TOOLS.isdisjoint(CAPACITY_LOCK_ALLOWED_TOOLS)
        classified = CAPACITY_LOCK_BLOCKED_TOOLS | CAPACITY_LOCK_ALLOWED_TOOLS
        assert classified == registry, (
            f"unclassified: {sorted(registry - classified)}; stale: {sorted(classified - registry)}"
        )

    @pytest.mark.parametrize(
        "tool",
        [
            "list",
            "list_contexts",
            "forget",
            "delete_context",
            "delete_file",
            "get_usage",
            "load_guardrails",
        ],
    )
    def test_the_cleanup_surface_stays_allowed(self, tool: str) -> None:
        assert tool in CAPACITY_LOCK_ALLOWED_TOOLS

    @pytest.mark.parametrize(
        "tool",
        [
            "recall",
            "reference",
            "explore",
            "load_pinned",
            "remember",
            "remember_batch",
            "update_memory",
            "create_context",
            "init_file_upload",
        ],
    )
    def test_search_and_saving_are_blocked(self, tool: str) -> None:
        assert tool in CAPACITY_LOCK_BLOCKED_TOOLS


def _ws(ws_id=None):
    return SimpleNamespace(id=ws_id or uuid4())


def _fake_db(workspaces=()) -> MagicMock:
    db = MagicMock()
    db.scalars = AsyncMock(return_value=MagicMock(all=lambda: list(workspaces)))
    return db


def _patch_db(db: MagicMock):
    async def _gen():
        yield db

    return patch("db.base.get_db", lambda: _gen())


def _payload(result) -> dict:
    return json.loads(result[0].text)


def _lock_obj():
    from services.capacity_lock import CapacityLock

    return CapacityLock(
        memory_count=1012,
        memory_limit=1000,
        over_memories=12,
        used_bytes=0,
        storage_limit_bytes=100 * 1024 * 1024,
        over_bytes=0,
        cleanup_url="https://app.example.test/workspace/settings/plan",
    )


def _locked_state():
    return patch("services.capacity_lock.capacity_lock_state", AsyncMock(return_value=_lock_obj()))


def _member(answer: bool):
    return patch("services.capacity_lock.is_workspace_member", AsyncMock(return_value=answer))


@pytest.fixture(autouse=True)
def _fresh_candidate_cache():
    gate.invalidate_capacity_candidate_cache()
    yield
    gate.invalidate_capacity_candidate_cache()


class TestTheSplit:
    def test_the_dispatcher_only_tools_are_exactly_these(self) -> None:
        """The BLOCKED tools with no service-level second line (CSO F3 list)."""
        assert gate.CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS == {
            "analyze_context",
            "bootstrap",
            "changes_since",
            "create_edge",
            "feedback",
            "get_active_analysis",
            "get_agent_bootstrap",
            "get_analysis",
            "get_cluster",
            "get_sleep_history",
            "get_sleep_report",
            "get_state",
            "ingest_events",
            "list_analyses",
            "list_edges",
            "merge_contexts",
            "recall_nearby",
            "recall_series",
            "recall_upcoming",
            "record_measurement",
            "rollback_sleep_run",
            "set_state",
            "setup_connector",
            "setup_resource",
            "update_edge",
            "update_search_config",
        }

    def test_service_gated_tools_are_blocked_tools(self) -> None:
        assert gate.CAPACITY_LOCK_SERVICE_GATED_TOOLS <= CAPACITY_LOCK_BLOCKED_TOOLS


class TestTheDispatcher:
    @pytest.mark.asyncio
    async def test_a_blocked_tool_on_the_locked_session_workspace_is_refused(self) -> None:
        ws = uuid4()
        handler = AsyncMock()
        with (
            _patch_db(_fake_db([_ws(ws)])),
            _locked_state(),
            patch("mcp_server.tools._TOOL_REGISTRY", {**_build_registry(), "recall": handler}),
            patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 9))),
        ):
            result = await execute_tool_call("recall", {"query": "q"}, "u1", ws)

        handler.assert_not_awaited()
        body = _payload(result)
        assert body["error"] == "capacity_locked"
        assert body["gate"] == "capacity"
        assert body["over_memories"] == 12
        assert body["cleanup_url"] == "https://app.example.test/workspace/settings/plan"
        assert "12 memories" in body["help"]
        assert "forget" in body["help"]

    @pytest.mark.asyncio
    async def test_an_allowed_tool_never_touches_the_lock(self) -> None:
        db = _fake_db([_ws()])
        with _patch_db(db), _locked_state():
            assert await gate.capacity_lock_refusal("forget", {}, "u1", uuid4()) is None
        db.scalars.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_service_gated_tool_with_an_argument_target_is_left_to_the_service(
        self,
    ) -> None:
        """CSO F3: the post-authorization service check answers instead."""
        db = _fake_db([_ws()])
        with _patch_db(db), _locked_state():
            for tool in ("recall", "remember", "update_memory", "complete_file_upload"):
                args = (
                    {"file_id": str(uuid4())}
                    if tool == "complete_file_upload"
                    else {"context_id": str(uuid4())}
                )
                assert await gate.capacity_lock_refusal(tool, args, "u1", uuid4()) is None
        db.scalars.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_member_of_the_argument_workspace_is_refused(self) -> None:
        other = uuid4()
        with _patch_db(_fake_db([_ws(other)])), _locked_state(), _member(True) as member:
            result = await gate.capacity_lock_refusal(
                "get_state", {"context_id": str(uuid4())}, "u1", uuid4()
            )
        assert result is not None
        assert _payload(result)["error"] == "capacity_locked"
        assert member.await_args.args[1:] == (other, "u1")

    @pytest.mark.asyncio
    async def test_a_non_member_is_left_to_the_handlers_not_found(self) -> None:
        """No existence / billing-state oracle: the handler answers first."""
        with _patch_db(_fake_db([_ws()])), _locked_state(), _member(False):
            assert (
                await gate.capacity_lock_refusal(
                    "get_state", {"context_id": str(uuid4())}, "u1", uuid4()
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_the_session_workspace_named_in_arguments_needs_no_membership_read(
        self,
    ) -> None:
        ws = uuid4()
        with _patch_db(_fake_db([_ws(ws)])), _locked_state(), _member(False) as member:
            result = await gate.capacity_lock_refusal(
                "set_state", {"context_id": str(uuid4())}, "u1", ws
            )
        assert result is not None
        member.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_query_filtered_to_candidates(self) -> None:
        """Hot path: a paid workspace costs one lookup and nothing else."""
        from sqlalchemy.dialects import postgresql

        db = _fake_db([])
        state = AsyncMock()
        with _patch_db(db), patch("services.capacity_lock.capacity_lock_state", state):
            assert (
                await gate.capacity_lock_refusal(
                    "recall_series", {"context_ids": [str(uuid4()), str(uuid4())]}, "u1", uuid4()
                )
                is None
            )
        db.scalars.assert_awaited_once()
        sql = str(db.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "workspaces.plan_name =" in sql
        assert "workspaces.entitlement_source =" in sql
        state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_non_candidate_session_workspace_is_cached(self) -> None:
        """A paid session workspace opens no database session on the next call."""
        ws = uuid4()
        db = _fake_db([])
        opened = 0

        async def _gen():
            nonlocal opened
            opened += 1
            yield db

        with patch("db.base.get_db", lambda: _gen()):
            assert await gate.capacity_lock_refusal("recall", {}, "u1", ws) is None
            assert await gate.capacity_lock_refusal("remember", {}, "u1", ws) is None
        assert opened == 1

    @pytest.mark.asyncio
    async def test_the_cache_expires(self, monkeypatch) -> None:
        ws = uuid4()
        gate._remember_not_candidate(ws)
        assert gate._known_not_candidate(ws)
        monkeypatch.setattr(gate.time, "monotonic", lambda: 10**12)
        assert not gate._known_not_candidate(ws)

    def test_file_and_workspace_arguments_are_targets(self) -> None:
        from sqlalchemy.dialects import postgresql

        clause = gate._argument_target_clause(set(), uuid4(), uuid4())
        sql = str(clause.compile(dialect=postgresql.dialect()))
        assert "file_objects" in sql
        assert "workspaces.id =" in sql

    @pytest.mark.asyncio
    async def test_an_infrastructure_error_fails_open(self) -> None:
        db = MagicMock()
        db.scalars = AsyncMock(side_effect=RuntimeError("db down"))
        with _patch_db(db):
            assert await gate.capacity_lock_refusal("recall", {}, "u1", uuid4()) is None


class TestTheServiceRefusalKeepsTheCode:
    def test_a_handler_legacy_code_does_not_hide_the_lock(self) -> None:
        from mcp_server.tools._errors import describe_tool_exception

        failure = describe_tool_exception("remember", _locked(), error="remember_error")
        body = json.loads(failure.response()[0].text)
        assert body["error"] == "capacity_locked"
        assert body["gate"] == "capacity"
