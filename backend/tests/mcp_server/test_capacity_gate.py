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
    async def test_a_member_of_the_argument_workspace_is_refused(self) -> None:
        other = uuid4()
        with _patch_db(_fake_db([_ws(other)])), _locked_state(), _member(True) as member:
            result = await gate.capacity_lock_refusal(
                "recall", {"context_id": str(uuid4())}, "u1", uuid4()
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
                    "recall", {"context_id": str(uuid4())}, "u1", uuid4()
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
                "remember", {"context_id": str(uuid4())}, "u1", ws
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
                    "recall", {"context_ids": [str(uuid4()), str(uuid4())]}, "u1", uuid4()
                )
                is None
            )
        db.scalars.assert_awaited_once()
        sql = str(db.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "workspaces.plan_name =" in sql
        assert "workspaces.entitlement_source =" in sql
        state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_and_workspace_arguments_are_targets(self) -> None:
        from sqlalchemy.dialects import postgresql

        db = _fake_db([])
        with _patch_db(db):
            await gate.capacity_lock_refusal(
                "complete_file_upload",
                {"file_id": str(uuid4()), "workspace_id": str(uuid4())},
                "u1",
                None,
            )
        sql = str(db.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "file_objects" in sql
        assert "workspaces.id =" in sql

    @pytest.mark.asyncio
    async def test_an_infrastructure_error_fails_open(self) -> None:
        db = MagicMock()
        db.scalars = AsyncMock(side_effect=RuntimeError("db down"))
        with _patch_db(db):
            assert await gate.capacity_lock_refusal("recall", {}, "u1", uuid4()) is None
