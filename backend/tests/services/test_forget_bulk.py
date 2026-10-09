"""MemoryService.forget_bulk — delete by filter in one context (#1941)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from services.memory_service import MemoryService
from utils.exceptions import CapacityLockedError, NotFoundException

WS = uuid4()
CTX = uuid4()


def _row(author: str = "u1", type_: str = "note", guardrail: bool = False):
    return SimpleNamespace(
        id=uuid4(), user_id=author, type=type_, source_type="manual", is_guardrail=guardrail
    )


def _result(rows=None, memories=None) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows or []
    r.scalars.return_value.all.return_value = memories or []
    return r


def _service(rows, *, can_access=True) -> tuple[MemoryService, MagicMock]:
    db = MagicMock(spec=AsyncSession)
    db.commit = AsyncMock()
    memories_by_id = {}

    async def execute(stmt):
        if not hasattr(execute, "first_done"):
            execute.first_done = True  # type: ignore[attr-defined]
            return _result(rows=rows)
        # A chunk load: return one memory stand-in per id in the IN list.
        ids = stmt.whereclause.clauses[0].right.value
        return _result(memories=[memories_by_id.setdefault(i, SimpleNamespace(id=i)) for i in ids])

    db.execute = AsyncMock(side_effect=execute)
    service = MemoryService(db)
    service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), str(WS), str(CTX))
    )
    service._soft_delete_memory = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda user_id, memory, *a, **kw: memory.id
    )
    perm = MagicMock(
        can_access_memory=AsyncMock(
            side_effect=can_access if callable(can_access) else lambda **_: can_access
        )
    )
    return service, perm


AUDIT = AsyncMock()
POINTS = AsyncMock()


async def _run(service, perm, **kwargs):
    """Returns the count; ``_run_full`` returns ``(count, remaining)``."""
    count, _ = await _run_full(service, perm, **kwargs)
    return count


async def _run_full(service, perm, **kwargs):
    AUDIT.reset_mock()
    POINTS.reset_mock()
    with (
        patch("services.permission_service.PermissionService", return_value=perm),
        patch("services.memory_access_event_writer.emit_memory_access_event", AUDIT),
        patch("db.qdrant.delete_points_from_qdrant", POINTS),
        patch(
            "services.memory_service.resolve_collection_name",
            AsyncMock(return_value="kagura_memories"),
        ) as resolve,
    ):
        result = await service.forget_bulk("u1", context_id=CTX, **kwargs)
    service.resolve_calls = resolve.await_count
    return result


class TestPermissions:
    @pytest.mark.asyncio
    async def test_a_context_the_caller_cannot_write_is_not_found(self) -> None:
        service, perm = _service([_row()])
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            side_effect=NotFoundException("Context", str(CTX))
        )
        with pytest.raises(NotFoundException):
            await _run(service, perm, dry_run=False)
        service._soft_delete_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_context_gate_is_forgets_write_gate(self) -> None:
        service, perm = _service([])
        await _run(service, perm)
        kwargs = service._get_context_isolation_params.await_args.kwargs
        assert kwargs["access"] == "write"
        assert kwargs["operation"] == "forget"

    @pytest.mark.asyncio
    async def test_rows_the_caller_may_not_delete_are_skipped(self) -> None:
        mine, theirs = _row("u1"), _row("u2")
        service, perm = _service(
            [mine, theirs, _row("u1")],
            can_access=lambda **kw: kw["memory_user_id"] == "u1",
        )
        assert await _run(service, perm) == 2
        # Evaluated once per (author, type, source) group, not per row.
        assert perm.can_access_memory.await_count == 2
        assert all(c.kwargs["access"] == "write" for c in perm.can_access_memory.await_args_list)

    @pytest.mark.asyncio
    async def test_guardrails_need_the_author_gate(self) -> None:
        from utils.exceptions import AuthorizationError

        service, perm = _service([_row(), _row(guardrail=True), _row(guardrail=True)])
        service._require_guardrail_author = AsyncMock(  # type: ignore[method-assign]
            side_effect=AuthorizationError("no")
        )
        assert await _run(service, perm) == 1
        service._require_guardrail_author.assert_awaited_once()


class TestDryRunAndDelete:
    @pytest.mark.asyncio
    async def test_dry_run_counts_and_deletes_nothing(self) -> None:
        service, perm = _service([_row(), _row(), _row()])
        assert await _run(service, perm, dry_run=True) == 3
        service._soft_delete_memory.assert_not_awaited()
        service.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_runs_forgets_side_effects_in_chunks(self) -> None:
        rows = [_row() for _ in range(5)]
        service, perm = _service(rows)
        assert await _run(service, perm, dry_run=False, chunk_size=2) == 5
        assert service._soft_delete_memory.await_count == 5
        # 3 chunks (2 + 2 + 1), each committed.
        assert service.db.commit.await_count == 3
        call = service._soft_delete_memory.await_args
        assert call.args[0] == "u1"
        assert call.args[2:] == (str(WS), str(CTX))
        assert call.kwargs == {"delete_point": False}

    @pytest.mark.asyncio
    async def test_each_chunk_is_audited_and_its_points_removed_in_one_request(self) -> None:
        rows = [_row() for _ in range(5)]
        service, perm = _service(rows)
        await _run(service, perm, dry_run=False, chunk_size=2)
        assert AUDIT.await_count == 3
        assert [c.kwargs["result_count"] for c in AUDIT.await_args_list] == [2, 2, 1]
        assert POINTS.await_count == 3
        assert [len(c.args[0]) for c in POINTS.await_args_list] == [2, 2, 1]
        assert all(c.args[1] == "kagura_memories" for c in POINTS.await_args_list)
        # The collection is resolved once per request, not per memory.
        assert service.resolve_calls == 1

    @pytest.mark.asyncio
    async def test_one_request_is_capped_and_reports_what_remains(self) -> None:
        rows = [_row() for _ in range(5)]
        service, perm = _service(rows)
        count, remaining = await _run_full(service, perm, dry_run=False, max_rows=3, chunk_size=2)
        assert (count, remaining) == (3, True)
        assert service._soft_delete_memory.await_count == 3

    @pytest.mark.asyncio
    async def test_the_last_request_reports_nothing_remaining(self) -> None:
        service, perm = _service([_row() for _ in range(3)])
        assert await _run_full(service, perm, dry_run=False, max_rows=3) == (3, False)

    @pytest.mark.asyncio
    async def test_dry_run_counts_past_the_cap(self) -> None:
        service, perm = _service([_row() for _ in range(5)])
        assert await _run_full(service, perm, dry_run=True, max_rows=3) == (5, False)

    @pytest.mark.asyncio
    async def test_filters_reach_the_query(self) -> None:
        service, perm = _service([])
        await _run(
            service,
            perm,
            created_before=datetime(2026, 1, 1),
            created_after=datetime(2025, 1, 1),
            memory_type="note",
            tags=["old"],
        )
        stmt = service.db.execute.await_args_list[0].args[0]
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        for fragment in (
            "memories.context_id =",
            "memories.deleted_at IS NULL",
            "memories.created_at <",
            "memories.created_at >=",
            "memories.type =",
            "memories.tags &&",
        ):
            assert fragment in sql, fragment


class TestCapacityLock:
    @pytest.mark.asyncio
    async def test_bulk_delete_is_allowed_while_locked(self) -> None:
        """Deleting is the way out of the lock — it must never hit the check."""
        service, perm = _service([_row(), _row()])
        locked = AsyncMock(side_effect=CapacityLockedError.for_outsider())
        with (
            patch("services.capacity_lock.ensure_not_capacity_locked", locked),
            patch("services.capacity_lock.ensure_context_not_capacity_locked", locked),
        ):
            assert await _run(service, perm, dry_run=False) == 2
        locked.assert_not_awaited()


class TestForgetByQueryPassesTheWorkspace:
    @pytest.mark.asyncio
    async def test_recall_gets_the_context_workspace(self) -> None:
        """forget(query=...) is the cleanup path while locked; recall() refuses
        a call without the workspace, so forget must pass it (pre-#1941 bug)."""
        from models.schemas import ForgetRequest

        service = MemoryService(MagicMock())
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(WS), str(CTX))
        )
        service.recall = AsyncMock(side_effect=RuntimeError("stop"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="stop"):
            await service.forget(ForgetRequest(query="old notes", k=5), "u1", CTX)
        kwargs = service.recall.await_args.kwargs
        assert kwargs["current_workspace_id"] == WS
        assert kwargs["capacity_gate"] is False
