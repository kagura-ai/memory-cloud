"""The capacity-over lock at the MCP dispatcher (#1941)."""

from __future__ import annotations

import json
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
        "tool", ["list", "list_contexts", "forget", "delete_context", "delete_file", "get_usage"]
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


def _fake_db(*, context_workspaces: list | None = None, file_workspace=None) -> MagicMock:
    db = MagicMock()
    db.scalars = AsyncMock(return_value=list(context_workspaces or []))
    db.scalar = AsyncMock(return_value=file_workspace)
    return db


def _patch_db(db: MagicMock):
    async def _gen():
        yield db

    return patch("db.base.get_db", lambda: _gen())


def _payload(result) -> dict:
    return json.loads(result[0].text)


class TestTheDispatcher:
    @pytest.mark.asyncio
    async def test_a_blocked_tool_on_a_locked_workspace_is_refused(self) -> None:
        ws = uuid4()
        handler = AsyncMock()
        ensure = AsyncMock(side_effect=_locked())
        with (
            _patch_db(_fake_db()),
            patch("services.capacity_lock.ensure_not_capacity_locked", ensure),
            patch("mcp_server.tools._TOOL_REGISTRY", {**_build_registry(), "recall": handler}),
            patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 9))),
        ):
            result = await execute_tool_call(
                "recall", {"context_id": str(uuid4()), "query": "q"}, "u1", ws
            )

        handler.assert_not_awaited()
        body = _payload(result)
        assert body["error"] == "capacity_locked"
        assert body["gate"] == "capacity"
        assert body["over_memories"] == 12
        assert body["cleanup_url"] == "https://app.example.test/workspace/settings/plan"
        assert "12 memories" in body["help"]
        assert "forget" in body["help"]
        # The context did not resolve, so the session workspace is the target.
        ensure.assert_awaited_once()
        assert ensure.await_args.args[1] == ws

    @pytest.mark.asyncio
    async def test_an_allowed_tool_never_touches_the_lock(self) -> None:
        ensure = AsyncMock(side_effect=_locked())
        with patch("services.capacity_lock.ensure_not_capacity_locked", ensure):
            assert await gate.capacity_lock_refusal("forget", {}, "u1", uuid4()) is None
        ensure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_target_is_the_context_workspace_not_the_session(self) -> None:
        """A context of another workspace is checked as that workspace, with the
        caller's id so a non-member gets no numbers."""
        session_ws, other_ws = uuid4(), uuid4()
        ensure = AsyncMock()
        with (
            _patch_db(_fake_db(context_workspaces=[other_ws])),
            patch("services.capacity_lock.ensure_not_capacity_locked", ensure),
        ):
            await gate.capacity_lock_refusal(
                "recall", {"context_id": str(uuid4())}, "u1", session_ws
            )
        ensure.assert_awaited_once()
        assert ensure.await_args.args[1] == other_ws
        assert ensure.await_args.kwargs == {"user_id": "u1"}

    @pytest.mark.asyncio
    async def test_cross_context_recall_checks_every_workspace(self) -> None:
        session_ws, other_ws = uuid4(), uuid4()
        ensure = AsyncMock()
        with (
            _patch_db(_fake_db(context_workspaces=[session_ws, other_ws])),
            patch("services.capacity_lock.ensure_not_capacity_locked", ensure),
        ):
            await gate.capacity_lock_refusal(
                "recall", {"context_ids": [str(uuid4()), str(uuid4())]}, "u1", session_ws
            )
        checked = {c.args[1] for c in ensure.await_args_list}
        assert checked == {session_ws, other_ws}

    @pytest.mark.asyncio
    async def test_complete_upload_resolves_the_file_workspace(self) -> None:
        file_ws = uuid4()
        ensure = AsyncMock()
        with (
            _patch_db(_fake_db(file_workspace=file_ws)),
            patch("services.capacity_lock.ensure_not_capacity_locked", ensure),
        ):
            await gate.capacity_lock_refusal(
                "complete_file_upload", {"file_id": str(uuid4())}, "u1", None
            )
        assert ensure.await_args.args[1] == file_ws

    @pytest.mark.asyncio
    async def test_an_infrastructure_error_fails_open(self) -> None:
        ensure = AsyncMock(side_effect=RuntimeError("db down"))
        with (
            _patch_db(_fake_db()),
            patch("services.capacity_lock.ensure_not_capacity_locked", ensure),
        ):
            assert await gate.capacity_lock_refusal("recall", {}, "u1", uuid4()) is None
