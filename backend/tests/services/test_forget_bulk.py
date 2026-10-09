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
    service._soft_delete_memory = AsyncMock()  # type: ignore[method-assign]
    perm = MagicMock(
        can_access_memory=AsyncMock(
            side_effect=can_access if callable(can_access) else lambda **_: can_access
        )
    )
    return service, perm


async def _run(service, perm, **kwargs):
    with (
        patch("services.permission_service.PermissionService", return_value=perm),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
    ):
        return await service.forget_bulk("u1", context_id=CTX, **kwargs)


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
