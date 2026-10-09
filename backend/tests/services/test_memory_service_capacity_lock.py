"""The capacity-over lock at the MemoryService layer (#1941).

The second line behind the MCP dispatcher: REST and MCP both reach these
methods. The session is a ``MagicMock(spec=AsyncSession)`` so the root
conftest lets ``_ensure_capacity`` run; the lock itself is patched so each
test pins WHERE the check happens, not the predicate (``test_capacity_lock``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.schemas import (
    ExploreRequest,
    ForgetRequest,
    PatchMemoryRequest,
    RecallRequest,
    RememberRequest,
    UpdateMemoryRequest,
)
from services.memory_service import MemoryService
from utils.exceptions import CapacityLockedError, NotFoundException


def _locked() -> CapacityLockedError:
    return CapacityLockedError(
        memory_count=1001,
        memory_limit=1000,
        over_memories=1,
        used_bytes=0,
        storage_limit_bytes=1,
        over_bytes=0,
        cleanup_url="https://app.example.test/workspace/settings/plan",
    )


def _service() -> MemoryService:
    return MemoryService(MagicMock(spec=AsyncSession))


@pytest.fixture
def locked():
    """Every workspace and context reads as locked."""
    with (
        patch(
            "services.capacity_lock.ensure_not_capacity_locked",
            AsyncMock(side_effect=_locked()),
        ) as by_ws,
        patch(
            "services.capacity_lock.ensure_contexts_not_capacity_locked",
            AsyncMock(side_effect=_locked()),
        ) as by_ctx,
    ):
        yield SimpleNamespace(by_ws=by_ws, by_ctx=by_ctx)


class TestEnsureCapacity:
    @pytest.mark.asyncio
    async def test_all_contexts_go_in_one_batched_check(self) -> None:
        ctx = uuid4()
        other = uuid4()
        with patch(
            "services.capacity_lock.ensure_contexts_not_capacity_locked", AsyncMock()
        ) as by_ctx:
            await _service()._ensure_capacity("u", context_ids=(ctx, None, ctx, other))
        by_ctx.assert_awaited_once()
        assert list(by_ctx.await_args.args[1]) == [ctx, None, ctx, other]
        assert by_ctx.await_args.kwargs == {"user_id": "u"}

    @pytest.mark.asyncio
    async def test_a_mock_session_skips_the_check(self) -> None:
        """The conftest gate: a bare MagicMock session never runs the lock."""
        with patch("services.capacity_lock.ensure_not_capacity_locked", AsyncMock()) as by_ws:
            await MemoryService(MagicMock())._ensure_capacity("u", workspace_id=uuid4())
        by_ws.assert_not_awaited()


class TestBlocked:
    @pytest.mark.asyncio
    async def test_remember_checks_after_the_write_gate(self, locked) -> None:
        service = _service()
        ws = uuid4()
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(ws), str(uuid4()))
        )
        with pytest.raises(CapacityLockedError):
            await service.remember(
                RememberRequest(summary="a summary long enough", content="body", type="note"),
                user_id="u",
                current_context_id=uuid4(),
            )
        service._get_context_isolation_params.assert_awaited_once()
        assert locked.by_ws.await_args.args[1] == str(ws)

    @pytest.mark.asyncio
    async def test_remember_many_checks_the_batch_once_after_the_write_gate(self, locked) -> None:
        service = _service()
        ws = uuid4()
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(ws), str(uuid4()))
        )
        service._prepare_remember = AsyncMock()  # type: ignore[method-assign]
        requests = [
            RememberRequest(summary=f"a summary long enough {i}", content="body", type="note")
            for i in range(3)
        ]
        with pytest.raises(CapacityLockedError):
            await service.remember_many(requests, user_id="u", current_context_id=uuid4())
        service._get_context_isolation_params.assert_awaited_once()
        locked.by_ws.assert_awaited_once()
        assert locked.by_ws.await_args.args[1] == str(ws)
        service._prepare_remember.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_memory_checks_after_the_access_check(self, locked) -> None:
        service = _service()
        ws = uuid4()
        service._update_load_authorized = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(workspace_id=ws)
        )
        with pytest.raises(CapacityLockedError):
            await service.update_memory(
                UpdateMemoryRequest(memory_id=uuid4(), summary="new summary text"),
                user_id="u",
                current_context_id=uuid4(),
            )
        assert locked.by_ws.await_args.args[1] == ws

    @pytest.mark.asyncio
    async def test_recall_checks_every_context_after_scope_resolution(self, locked) -> None:
        service = _service()
        service._recall_check_agent_bindings = AsyncMock()  # type: ignore[method-assign]
        service._recall_prepare = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(request=RecallRequest(query="q"))
        )
        ctx, extra = uuid4(), uuid4()
        with pytest.raises(CapacityLockedError):
            await service.recall(
                RecallRequest(query="q"),
                "u",
                current_context_id=ctx,
                current_workspace_id=uuid4(),
                context_ids=[extra],
            )
        service._recall_prepare.assert_awaited_once()
        assert list(locked.by_ctx.await_args.args[1]) == [ctx, extra]


class TestNonMembersGetNotFoundNotTheLock:
    """The lock must not answer before authorization: a caller who may not
    reach the context or memory gets the uniform not-found, never a
    CAPACITY-001 that would reveal the target exists and is over capacity."""

    @pytest.mark.asyncio
    async def test_remember(self, locked) -> None:
        service = _service()
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            side_effect=NotFoundException("Context", "x")
        )
        with pytest.raises(NotFoundException):
            await service.remember(
                RememberRequest(summary="a summary long enough", content="body", type="note"),
                user_id="outsider",
                current_context_id=uuid4(),
            )
        locked.by_ws.assert_not_awaited()
        locked.by_ctx.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_memory(self, locked) -> None:
        service = _service()
        service._update_load_authorized = AsyncMock(  # type: ignore[method-assign]
            side_effect=NotFoundException("Memory", "x")
        )
        with pytest.raises(NotFoundException):
            await service.update_memory(
                UpdateMemoryRequest(memory_id=uuid4(), summary="new summary text"),
                user_id="outsider",
                current_context_id=uuid4(),
            )
        locked.by_ws.assert_not_awaited()
        locked.by_ctx.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recall(self, locked) -> None:
        service = _service()
        service._recall_check_agent_bindings = AsyncMock(  # type: ignore[method-assign]
            side_effect=NotFoundException("Context", "x")
        )
        with pytest.raises(NotFoundException):
            await service.recall(
                RecallRequest(query="q"),
                "outsider",
                current_context_id=uuid4(),
                current_workspace_id=uuid4(),
            )
        locked.by_ctx.assert_not_awaited()


class TestBlockedReadsAfterAccess:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["load_pinned"])
    async def test_the_deterministic_loads(self, locked, method: str) -> None:
        service = _service()
        ws = uuid4()
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(ws), str(uuid4()))
        )
        with pytest.raises(CapacityLockedError):
            await getattr(service, method)(user_id="u", current_context_id=uuid4())
        assert locked.by_ws.await_args.args[1] == str(ws)

    @pytest.mark.asyncio
    async def test_reference_checks_after_the_access_check(self, locked) -> None:
        service = _service()
        ws = uuid4()
        memory = SimpleNamespace(
            user_id="u",
            workspace_id=ws,
            context_id=uuid4(),
            type="note",
            source_type=None,
        )
        service.memory_repo = MagicMock(get=AsyncMock(return_value=memory))
        perm = MagicMock(can_access_memory=AsyncMock(return_value=True))
        with patch("services.permission_service.PermissionService", return_value=perm):
            with pytest.raises(CapacityLockedError):
                await service.reference(uuid4(), "u")
        perm.can_access_memory.assert_awaited_once()
        assert locked.by_ws.await_args.args[1] == ws

    @pytest.mark.asyncio
    async def test_a_denied_reference_is_not_found_not_locked(self, locked) -> None:
        """The lock must not become an existence oracle."""
        from utils.exceptions import NotFoundException

        service = _service()
        memory = SimpleNamespace(
            user_id="x", workspace_id=uuid4(), context_id=uuid4(), type="note", source_type=None
        )
        service.memory_repo = MagicMock(get=AsyncMock(return_value=memory))
        perm = MagicMock(can_access_memory=AsyncMock(return_value=False))
        with patch("services.permission_service.PermissionService", return_value=perm):
            with pytest.raises(NotFoundException):
                await service.reference(uuid4(), "u")
        locked.by_ws.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_patch_memory(self, locked) -> None:
        service = _service()
        ws = uuid4()
        service._patch_load_authorized = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(workspace_id=ws)
        )
        with pytest.raises(CapacityLockedError):
            await service.patch_memory(
                uuid4(), PatchMemoryRequest(summary="a changed summary"), "u"
            )
        assert locked.by_ws.await_args.args[1] == ws

    @pytest.mark.asyncio
    async def test_explore(self, locked) -> None:
        service = _service()
        ws = uuid4()
        seed = SimpleNamespace(
            id=uuid4(),
            user_id="u",
            workspace_id=ws,
            context_id=uuid4(),
            type="note",
            source_type=None,
        )
        service.memory_repo = MagicMock(get=AsyncMock(return_value=seed))
        perm = MagicMock(can_access_memory=AsyncMock(return_value=True))
        with patch("services.permission_service.PermissionService", return_value=perm):
            with pytest.raises(CapacityLockedError):
                await service.explore(ExploreRequest(memory_id=seed.id), "u")
        assert locked.by_ws.await_args.args[1] == ws


class TestStillAllowed:
    @pytest.mark.asyncio
    async def test_guardrails_stay_available_while_locked(self, locked) -> None:
        """Safety rails, small and bounded — and served by other doors anyway."""
        service = _service()
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(uuid4()), str(uuid4()))
        )
        service.memory_repo = MagicMock(
            list_pinned=AsyncMock(side_effect=RuntimeError("reached the read"))
        )
        with pytest.raises(RuntimeError, match="reached the read"):
            await service.load_guardrails(user_id="u", current_context_id=uuid4())
        locked.by_ws.assert_not_awaited()
        locked.by_ctx.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forget_by_query_finds_candidates_without_the_lock(self) -> None:
        """forget(query=...) goes through recall; it must keep working while locked."""
        service = _service()
        service.recall = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("stop after the recall call")
        )
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(uuid4()), str(uuid4()))
        )
        with pytest.raises(RuntimeError, match="stop after"):
            await service.forget(ForgetRequest(query="old notes", k=5), "u", uuid4())
        assert service.recall.await_args.kwargs.get("capacity_gate") is False
