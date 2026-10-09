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


_CLOCK = iter(range(10**9))


def _row(author: str = "u1", type_: str = "note", guardrail: bool = False):
    return SimpleNamespace(
        id=uuid4(),
        created_at=next(_CLOCK),
        user_id=author,
        type=type_,
        source_type="manual",
        is_guardrail=guardrail,
    )


def _result(rows=None, memories=None) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows or []
    r.scalars.return_value.all.return_value = memories or []
    return r


def _service(rows, *, can_access=True) -> tuple[MemoryService, MagicMock]:
    """A session that answers forget_bulk's three statement shapes.

    * the dry-run aggregate (GROUP BY) — grouped from ``rows``;
    * a candidate page (LIMIT) — the next ``limit`` rows (keyset emulated by
      a cursor; the real predicate is pinned by the SQL test);
    * a chunk load — one memory stand-in per id in the IN list.
    """
    db = MagicMock(spec=AsyncSession)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    memories_by_id: dict = {}
    cursor = {"pos": 0}
    db.statements = []

    async def execute(stmt):
        db.statements.append(stmt)
        if stmt._group_by_clauses:
            groups: dict = {}
            for r in rows:
                key = (r.user_id, r.type, r.source_type, r.is_guardrail)
                groups[key] = groups.get(key, 0) + 1
            return _result(
                rows=[
                    SimpleNamespace(
                        user_id=k[0], type=k[1], source_type=k[2], is_guardrail=k[3], n=n
                    )
                    for k, n in groups.items()
                ]
            )
        if stmt._limit_clause is not None:
            limit = stmt._limit_clause.value
            page = rows[cursor["pos"] : cursor["pos"] + limit]
            cursor["pos"] += len(page)
            return _result(rows=page)
        ids = stmt.whereclause.clauses[0].right.value
        return _result(memories=[memories_by_id.setdefault(i, SimpleNamespace(id=i)) for i in ids])

    db.execute = AsyncMock(side_effect=execute)
    service = MemoryService(db)
    service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), str(WS), str(CTX))
    )
    service._soft_delete_chunk = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda user_id, ids, *a, **kw: (list(ids), [str(i) for i in ids])
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


def _deleted_ids(service) -> list:
    return [i for c in service._soft_delete_chunk.await_args_list for i in c.args[1]]


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
    service.next_after = result[2]
    return result[0], result[1]


class TestPermissions:
    @pytest.mark.asyncio
    async def test_a_context_the_caller_cannot_write_is_not_found(self) -> None:
        service, perm = _service([_row()])
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            side_effect=NotFoundException("Context", str(CTX))
        )
        with pytest.raises(NotFoundException):
            await _run(service, perm, dry_run=False)
        service._soft_delete_chunk.assert_not_awaited()

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
        service._soft_delete_chunk.assert_not_awaited()
        service.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_runs_forgets_side_effects_in_chunks(self) -> None:
        rows = [_row() for _ in range(5)]
        service, perm = _service(rows)
        assert await _run(service, perm, dry_run=False, chunk_size=2) == 5
        assert len(_deleted_ids(service)) == 5
        assert [len(c.args[1]) for c in service._soft_delete_chunk.await_args_list] == [2, 2, 1]
        # 3 chunks (2 + 2 + 1), each committed.
        assert service.db.commit.await_count == 3
        call = service._soft_delete_chunk.await_args
        assert call.args[0] == "u1"
        assert call.args[2:] == (str(WS), str(CTX))

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
        assert len(_deleted_ids(service)) == 3

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


class TestBoundedReads:
    @pytest.mark.asyncio
    async def test_dry_run_is_one_grouped_count(self) -> None:
        rows = [_row("u1") for _ in range(40)] + [_row("u2") for _ in range(10)]
        service, perm = _service(rows, can_access=lambda **kw: kw["memory_user_id"] == "u1")
        assert await _run(service, perm, dry_run=True) == 40
        (stmt,) = service.db.statements
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "GROUP BY" in sql
        assert "count(" in sql.lower()
        assert stmt._limit_clause is None
        assert perm.can_access_memory.await_count == 2

    @pytest.mark.asyncio
    async def test_a_delete_reads_at_most_max_rows_plus_one(self) -> None:
        service, perm = _service([_row() for _ in range(10)])
        count, remaining = await _run_full(service, perm, dry_run=False, max_rows=3)
        assert (count, remaining) == (3, True)
        pages = [st for st in service.db.statements if st._limit_clause is not None]
        assert len(pages) == 1
        assert pages[0]._limit_clause.value == 4

    @pytest.mark.asyncio
    async def test_pages_on_when_permission_drops_rows(self) -> None:
        rows = [_row("u1" if i % 2 == 0 else "u2") for i in range(12)]
        service, perm = _service(rows, can_access=lambda **kw: kw["memory_user_id"] == "u1")
        count, remaining = await _run_full(service, perm, dry_run=False, max_rows=3)
        assert (count, remaining) == (3, True)
        pages = [st for st in service.db.statements if st._limit_clause is not None]
        assert len(pages) == 2
        # The second page continues after the last row seen (keyset).
        sql = str(pages[1].compile(dialect=postgresql.dialect()))
        assert "(memories.created_at, memories.id) >" in sql

    @pytest.mark.asyncio
    async def test_a_failure_in_chunk_two_keeps_chunk_one(self) -> None:
        """Points-then-commit per chunk, as single forget: chunk 1 stays
        committed and audited once; chunk 2 is rolled back and the error raised."""
        service, perm = _service([_row() for _ in range(4)])
        AUDIT.reset_mock()
        POINTS.reset_mock()
        POINTS.side_effect = [None, RuntimeError("qdrant down")]
        try:
            with (
                patch("services.permission_service.PermissionService", return_value=perm),
                patch("services.memory_access_event_writer.emit_memory_access_event", AUDIT),
                patch("db.qdrant.delete_points_from_qdrant", POINTS),
                patch(
                    "services.memory_service.resolve_collection_name",
                    AsyncMock(return_value="kagura_memories"),
                ),
                pytest.raises(RuntimeError, match="qdrant down"),
            ):
                await service.forget_bulk("u1", context_id=CTX, dry_run=False, chunk_size=2)
        finally:
            POINTS.side_effect = None
        assert service.db.commit.await_count == 1
        assert AUDIT.await_count == 1
        assert AUDIT.await_args.kwargs["result_count"] == 2
        service.db.rollback.assert_awaited_once()


class TestForgetByQueryUsesTheSessionWorkspace:
    @pytest.mark.asyncio
    async def test_the_session_workspace_wins(self) -> None:
        from models.schemas import ForgetRequest

        session_ws = uuid4()
        service = MemoryService(MagicMock())
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(WS), str(CTX))
        )
        service.recall = AsyncMock(side_effect=RuntimeError("stop"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="stop"):
            await service.forget(
                ForgetRequest(query="old notes", k=5), "u1", CTX, current_workspace_id=session_ws
            )
        assert service.recall.await_args.kwargs["current_workspace_id"] == session_ws

    @pytest.mark.asyncio
    async def test_query_matches_go_through_the_by_id_helper(self) -> None:
        """Same #1924 stamp, point rule and edge isolation as forget(memory_id)."""
        from models.schemas import ForgetRequest

        memory = SimpleNamespace(
            id=uuid4(), workspace_id=WS, context_id=CTX, is_tool_triggered=False
        )
        service = MemoryService(MagicMock(commit=AsyncMock()))
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(WS), str(CTX))
        )
        service.recall = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                degraded=False, results=[SimpleNamespace(memory_id=memory.id)]
            )
        )
        service.memory_repo = MagicMock(get=AsyncMock(return_value=memory))
        service._soft_delete_memory = AsyncMock()  # type: ignore[method-assign]
        with patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()):
            resp = await service.forget(ForgetRequest(query="old", k=5), "u1", CTX)
        assert resp.deleted_count == 1
        service._soft_delete_memory.assert_awaited_once_with("u1", memory, str(WS), str(CTX))


class TestResumeCursor:
    @pytest.mark.asyncio
    async def test_next_after_is_the_last_deleted_row(self) -> None:
        rows = [_row() for _ in range(5)]
        service, perm = _service(rows)
        await _run_full(service, perm, dry_run=False, max_rows=3)
        assert service.next_after == (rows[2].created_at, rows[2].id)

    @pytest.mark.asyncio
    async def test_nothing_remaining_means_no_cursor(self) -> None:
        service, perm = _service([_row() for _ in range(2)])
        await _run_full(service, perm, dry_run=False, max_rows=3)
        assert service.next_after is None

    @pytest.mark.asyncio
    async def test_a_resume_point_starts_the_scan_after_it(self) -> None:
        from datetime import datetime as dt

        service, perm = _service([_row()])
        resume = (dt(2026, 1, 1), uuid4())
        await _run_full(service, perm, dry_run=False, after=resume)
        page = next(st for st in service.db.statements if st._limit_clause is not None)
        sql = str(page.compile(dialect=postgresql.dialect()))
        assert "(memories.created_at, memories.id) >" in sql


class TestTheChunkIsAFixedNumberOfStatements:
    @pytest.mark.asyncio
    async def test_update_shared_point_read_and_edge_delete(self) -> None:
        """One UPDATE, one shared-point SELECT, one edge DELETE per chunk —
        whatever the chunk size."""
        own = [uuid4() for _ in range(3)]
        shared_point = uuid4()
        resource_row = uuid4()
        returned = [SimpleNamespace(id=i, summary_embedding_id=i) for i in own] + [
            SimpleNamespace(id=resource_row, summary_embedding_id=shared_point)
        ]
        statements: list = []

        async def execute(stmt):
            statements.append(stmt)
            r = MagicMock()
            if len(statements) == 1:
                r.all.return_value = returned
            elif len(statements) == 2:
                r.scalars.return_value = [shared_point]  # still owned by a live row
            else:
                r.rowcount = 4
            return r

        db = MagicMock(execute=AsyncMock(side_effect=execute))
        service = MemoryService(db)
        deleted, points = await service._soft_delete_chunk(
            "u1", [*own, resource_row], str(WS), str(CTX)
        )
        assert len(statements) == 3
        sqls = [str(st.compile(dialect=postgresql.dialect())) for st in statements]
        assert sqls[0].startswith("UPDATE memories SET deleted_at=")
        assert "deleted_by" in sqls[0] and "updated_at" not in sqls[0]  # #1924
        assert "memories.context_id =" in sqls[0]
        assert sqls[2].startswith("DELETE FROM neural_memory_edges")
        for fragment in ("user_id", "workspace_id", "context_id"):
            assert f"neural_memory_edges.{fragment} =" in sqls[2]
        assert deleted == [*own, resource_row]
        # The shared point another live row owns is kept out of the index delete.
        assert points == [str(i) for i in own]

    @pytest.mark.asyncio
    async def test_no_shared_points_skips_the_select(self) -> None:
        own = [uuid4() for _ in range(2)]
        statements: list = []

        async def execute(stmt):
            statements.append(stmt)
            r = MagicMock()
            r.all.return_value = [SimpleNamespace(id=i, summary_embedding_id=None) for i in own]
            r.rowcount = 0
            return r

        service = MemoryService(MagicMock(execute=AsyncMock(side_effect=execute)))
        await service._soft_delete_chunk("u1", own, str(WS), str(CTX))
        assert len(statements) == 2


class TestForgetByQueryIsAllOrNothing:
    @pytest.mark.asyncio
    async def test_a_legacy_null_context_row_aborts_before_any_side_effect(self) -> None:
        """A NULL-isolation match after a normal one: nothing is deleted (no
        search point removed for a row that would then stay live)."""
        from models.schemas import ForgetRequest

        good = SimpleNamespace(id=uuid4(), workspace_id=WS, context_id=CTX, is_tool_triggered=False)
        legacy = SimpleNamespace(
            id=uuid4(), workspace_id=None, context_id=None, is_tool_triggered=False
        )
        service = MemoryService(MagicMock(commit=AsyncMock()))
        service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), str(WS), str(CTX))
        )
        service.recall = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                degraded=False,
                results=[SimpleNamespace(memory_id=good.id), SimpleNamespace(memory_id=legacy.id)],
            )
        )
        by_id = {good.id: good, legacy.id: legacy}
        service.memory_repo = MagicMock(get=AsyncMock(side_effect=lambda mid: by_id[mid]))
        service._soft_delete_memory = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="NULL workspace_id/context_id"):
            await service.forget(ForgetRequest(query="old", k=5), "u1", CTX)
        service._soft_delete_memory.assert_not_awaited()
        service.db.commit.assert_not_awaited()
