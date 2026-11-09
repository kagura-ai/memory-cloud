"""The supersede catch-up pass (#2256): which rows, what it calls, how it counts.

The detection itself (``_create_knn_seed_edges``) is stubbed; its behaviour is
pinned in ``tests/services/test_knn_seeding.py``. The DB-backed run is in
``tests/integration/test_supersede_catch_up_db.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from tasks import supersede_catch_up as task
from tasks.supersede_catch_up import (
    STOPPED_COUNTER_UNREADABLE,
    STOPPED_JUDGE_BUDGET,
    STOPPED_SUGGESTIONS_OFF,
    catch_up_rows_query,
    run_supersede_catch_up,
)


def _sql(include_unverified: bool, before: datetime | None = None) -> str:
    query = catch_up_rows_query(
        uuid4(), before=before, include_unverified=include_unverified, limit=7
    )
    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class TestRowsQuery:
    def test_rows_without_a_candidate(self) -> None:
        sql = _sql(False)
        assert "memories.supersede_candidate IS NULL" in sql
        assert "CAST(memories.supersede_candidate AS TEXT) = 'null'" in sql
        assert "->>" not in sql
        for clause in (
            "memories.deleted_at IS NULL",
            "memories.embedding_suppressed_at IS NULL",
            "memories.resource_id IS NULL",
            "memories.embedding_status = 'success'",
        ):
            assert clause in sql
        assert "ORDER BY memories.created_at, memories.id" in sql
        assert "LIMIT 7" in sql
        assert "created_at <" not in sql

    def test_include_unverified_adds_live_candidates_without_a_verdict(self) -> None:
        sql = _sql(True)
        assert "(memories.supersede_candidate ->> 'memory_id') AS VARCHAR) IS NOT NULL" in sql
        assert "(memories.supersede_candidate ->> 'verification') AS VARCHAR) IS NULL" in sql
        assert " ? " not in sql  # json, not jsonb

    def test_before_bounds_created_at(self) -> None:
        assert "memories.created_at < '2026-01-01 00:00:00'" in _sql(False, datetime(2026, 1, 1))

    def test_after_resumes_past_created_at(self) -> None:
        query = catch_up_rows_query(
            uuid4(), before=None, include_unverified=False, limit=7, after=datetime(2026, 2, 1)
        )
        sql = str(
            query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert (
            "(memories.created_at, memories.id) > "
            "('2026-02-01 00:00:00', '00000000-0000-0000-0000-000000000000')"
        ) in sql


class _Db:
    """An AsyncSession double that answers the pass's statements by shape."""

    def __init__(self, memories: list[Any], workspace_id: UUID | None) -> None:
        self.memories = {m.id: m for m in memories}
        self.order = [m.id for m in memories]
        self.workspace_id = workspace_id
        self.gone: set[UUID] = set()
        self.statements: list[str] = []

    async def execute(self, statement: Any) -> Any:
        sql = str(statement)
        self.statements.append(sql)
        result = MagicMock()
        if "contexts.workspace_id" in sql:
            result.scalar_one_or_none.return_value = self.workspace_id
        elif "count(" in sql:
            result.scalar_one.return_value = len(self.order) + 3
        elif "ORDER BY" in sql:
            result.all.return_value = [
                SimpleNamespace(
                    id=i, summary_embedding_id=None, created_at=datetime(2026, 1, n + 1)
                )
                for n, i in enumerate(self.order)
            ]
        elif " IN (" in sql:
            # The revisions read before the vectors.
            result.all.return_value = [
                SimpleNamespace(
                    id=m.id,
                    summary=m.summary,
                    content=m.content,
                    embedded_text_digest=m.embedded_text_digest,
                )
                for m in self.memories.values()
            ]
        else:
            memory_id = statement.whereclause.clauses[0].right.value
            result.scalar_one_or_none.return_value = (
                None if memory_id in self.gone else self.memories[memory_id]
            )
        return result


def _memory(candidate: Any = None) -> Any:
    from services.supersede_verify import embedded_text_digest

    return SimpleNamespace(
        id=uuid4(),
        supersede_candidate=candidate,
        summary="s",
        content="c",
        embedded_text_digest=embedded_text_digest("s", "c"),
    )


def _config(suggest: bool = True, verify_mode: str = "off") -> Any:
    return SimpleNamespace(supersede_suggest_enabled=suggest, supersede_verify_mode=verify_mode)


async def _run(
    db: _Db,
    *,
    detect: Any = None,
    vectors: dict[str, list[float]] | None = None,
    counter: Any = None,
    config: Any = None,
    **kwargs: Any,
) -> tuple[Any, AsyncMock, AsyncMock, AsyncMock]:
    detect = detect or AsyncMock()
    if vectors is None:
        vectors = {str(i): [0.1] for i in db.order}
    retrieve = AsyncMock(return_value=vectors)
    read = counter or AsyncMock(return_value=0)
    with (
        patch(
            "neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=config or _config())
        ),
        patch(
            "services.context_routing.resolve_context_embedding",
            AsyncMock(return_value=("text-embedding-3-small", 512)),
        ),
        patch("db.qdrant.retrieve_dense_vectors", retrieve),
        patch("services.memory_service._create_knn_seed_edges", detect),
        patch.object(task, "_read_counter", read),
    ):
        result = await run_supersede_catch_up(db, uuid4(), dry_run=False, **kwargs)
    return result, detect, retrieve, read


def _setting(values: dict[UUID, Any]) -> AsyncMock:
    """A detection stub that leaves ``values[memory.id]`` in the column."""

    async def _detect(_db: Any, memory: Any, *_args: Any, **_kwargs: Any) -> bool:
        if memory.id in values:
            memory.supersede_candidate = values[memory.id]
        return True

    return AsyncMock(side_effect=_detect)


class TestApply:
    @pytest.mark.asyncio
    async def test_runs_the_detection_without_seeding_on_each_row(self) -> None:
        rows = [_memory(), _memory()]
        db = _Db(rows, uuid4())

        result, detect, retrieve, _ = await _run(db)

        assert detect.await_count == 2
        for call, memory in zip(detect.await_args_list, rows, strict=True):
            assert call.args[1] is memory
            assert call.args[2] == [0.1]
            assert call.args[3] == "kagura_memories"
            # #2510: the pass judges on the bulk lane, not live ingest's cap.
            assert call.kwargs == {"mechanical_reembed": False, "seed": False, "lane": "bulk"}
        retrieve.assert_awaited_once_with("kagura_memories", [str(m.id) for m in rows])
        assert (result.eligible, result.planned, result.processed) == (5, 2, 2)

    @pytest.mark.asyncio
    async def test_reads_the_stored_vectors_from_the_lance_store(self, tmp_path) -> None:
        """#2520: on the LanceDB preview the pass reads each row's stored vector
        from the store; a row without a point there is ``no_vector``."""
        pytest.importorskip("lancedb")
        from db.lance_store import LanceVectorStore

        store = LanceVectorStore(str(tmp_path / "lance"))
        indexed, unindexed = _memory(), _memory()
        await store.add_memory(
            user_id="google-oauth2|123",
            memory_id=indexed.id,
            vector=[1.0, 0.0, 0.0, 0.0],
            payload={"summary": "s", "type": "note", "scope": "working"},
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )
        db = _Db([indexed, unindexed], uuid4())
        detect = AsyncMock(return_value=True)
        with (
            patch("db.qdrant._active_store", lambda: store),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=_config())),
            patch(
                "services.context_routing.resolve_context_embedding",
                AsyncMock(return_value=("text-embedding-3-small", 512)),
            ),
            patch("services.memory_service._create_knn_seed_edges", detect),
            patch.object(task, "_read_counter", AsyncMock(return_value=0)),
        ):
            result = await run_supersede_catch_up(db, uuid4(), dry_run=False)

        assert [(c.args[1], c.args[2]) for c in detect.await_args_list] == [
            (indexed, [1.0, 0.0, 0.0, 0.0])
        ]
        assert (result.processed, result.no_vector) == (1, 1)

    @pytest.mark.asyncio
    async def test_tallies_from_the_column_before_and_after(self) -> None:
        target = str(uuid4())
        fresh, kept, cleared, updated = (
            _memory(),
            _memory(),
            _memory({"memory_id": target, "similarity": 0.9}),
            _memory({"memory_id": target, "similarity": 0.9}),
        )
        db = _Db([fresh, kept, cleared, updated], uuid4())
        detect = _setting(
            {
                fresh.id: {"memory_id": target, "similarity": 0.91},
                cleared.id: None,
                updated.id: {
                    "memory_id": target,
                    "similarity": 0.9,
                    "verification": {"verdict": "supersedes"},
                },
            }
        )

        result, *_ = await _run(db, detect=detect, include_unverified=True)

        assert (result.detected, result.cleared, result.updated, result.unchanged) == (1, 1, 1, 1)
        assert result.processed == 4
        assert result.judge_calls == 0
        assert result.stopped is None
        assert result.last_created_at == datetime(2026, 1, 4)

    @pytest.mark.asyncio
    async def test_a_restamped_candidate_with_the_same_target_and_verdict_is_unchanged(self):
        """Storing the same pair again moves ``detected_at`` only."""
        target = str(uuid4())
        row = _memory({"memory_id": target, "similarity": 0.9, "detected_at": "a"})
        db = _Db([row], uuid4())
        detect = _setting({row.id: {"memory_id": target, "similarity": 0.9, "detected_at": "b"}})

        result, *_ = await _run(db, detect=detect, include_unverified=True)

        assert (result.updated, result.unchanged) == (0, 1)

    @pytest.mark.asyncio
    async def test_a_swallowed_detection_failure_is_counted_as_failed(self) -> None:
        rows = [_memory(), _memory()]
        db = _Db(rows, uuid4())

        result, *_ = await _run(db, detect=AsyncMock(side_effect=[False, True]))

        assert (result.processed, result.failed, result.unchanged) == (2, 1, 1)

    @pytest.mark.asyncio
    async def test_after_is_passed_to_the_row_filter(self) -> None:
        db = _Db([_memory()], uuid4())

        await _run(db, after=datetime(2026, 3, 1), after_id=uuid4())

        assert all("(memories.created_at, memories.id) >" in sql for sql in db.statements[1:3])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["edited", "re_embedded"])
    async def test_a_row_that_moved_after_its_vector_was_read_is_skipped(self, change) -> None:
        """The vector was read for the text as it was; an edit (or an edit
        and its re-embed) landing before the detection makes the pair
        meaningless: skipped, no store, no clear."""
        moved, kept = _memory({"memory_id": str(uuid4()), "similarity": 0.9}), _memory()
        db = _Db([moved, kept], uuid4())
        before = dict(moved.supersede_candidate)

        async def retrieve(_collection, point_ids):
            if change == "edited":
                moved.summary = "edited"
            else:
                moved.embedded_text_digest = "another stamp"
            return {pid: [0.1] for pid in point_ids}

        detect = AsyncMock(return_value=True)
        with (
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=_config())),
            patch(
                "services.context_routing.resolve_context_embedding",
                AsyncMock(return_value=("text-embedding-3-small", 512)),
            ),
            patch("db.qdrant.retrieve_dense_vectors", AsyncMock(side_effect=retrieve)),
            patch("services.memory_service._create_knn_seed_edges", detect),
            patch.object(task, "_read_counter", AsyncMock(return_value=0)),
        ):
            result = await run_supersede_catch_up(
                db, uuid4(), dry_run=False, include_unverified=True
            )

        assert [c.args[1] for c in detect.await_args_list] == [kept]
        assert (result.processed, result.skipped) == (1, 1)
        assert moved.supersede_candidate == before

    @pytest.mark.asyncio
    async def test_the_resume_cursor_is_the_last_row_reached(self) -> None:
        rows = [_memory(), _memory()]
        db = _Db(rows, uuid4())

        result, *_ = await _run(db)

        assert (result.last_created_at, result.last_id) == (datetime(2026, 1, 2), rows[1].id)

    @pytest.mark.asyncio
    async def test_stops_once_the_judge_counter_advanced_by_the_budget(self) -> None:
        rows = [_memory() for _ in range(5)]
        db = _Db(rows, uuid4())
        # start 10; before rows 1-3: 10, 11, 12 → row 3 is skipped (2 used).
        counter = AsyncMock(side_effect=[10, 10, 11, 12, 12])

        result, detect, _, _ = await _run(db, counter=counter, max_judge_calls=2)

        assert detect.await_count == 2
        assert result.processed == 2
        assert result.stopped == STOPPED_JUDGE_BUDGET
        assert result.judge_calls == 2
        # Resume past the last row the detection ran on.
        assert result.last_created_at == datetime(2026, 1, 2)

    @pytest.mark.asyncio
    async def test_reads_the_workspace_bulk_counter_key(self) -> None:
        """#2510: the budget is read on the bulk lane's key, the one the
        pass's judge calls count on; live ingest's key is never read."""
        from services.supersede_verify import counter_key

        workspace_id = uuid4()
        db = _Db([_memory()], workspace_id)

        _, _, _, read = await _run(db)

        assert {c.args[0] for c in read.await_args_list} == {
            counter_key(str(workspace_id), lane="bulk")
        }
        assert counter_key(str(workspace_id), lane="bulk") != counter_key(str(workspace_id))

    @pytest.mark.asyncio
    async def test_a_run_across_midnight_carries_the_old_days_count(self) -> None:
        rows = [_memory(), _memory()]
        db = _Db(rows, uuid4())
        keys = iter(["k:day1", "k:day1", "k:day2", "k:day2"])
        values = {"k:day1": [5, 6, 7], "k:day2": [1, 2]}

        async def read(key: str) -> int:
            return values[key].pop(0)

        with patch(
            "services.supersede_verify.counter_key", side_effect=lambda _ws, **_kw: next(keys)
        ):
            result, *_ = await _run(db, counter=AsyncMock(side_effect=read))

        # day1 advanced 5 → 7 (2), then day2 0 → 2 (2).
        assert result.judge_calls == 4

    @pytest.mark.asyncio
    async def test_an_unreadable_counter_with_the_judge_off_runs_on(self) -> None:
        rows = [_memory(), _memory()]
        db = _Db(rows, uuid4())

        with patch.object(task, "logger") as log:
            result, detect, _, read = await _run(
                db, counter=AsyncMock(return_value=None), max_judge_calls=1
            )

        assert detect.await_count == 2
        assert result.judge_calls is None
        assert result.stopped is None
        read.assert_awaited_once()
        events = [c.args[0] for c in log.warning.call_args_list]
        assert events == ["supersede_catch_up_counter_unreadable"]

    @pytest.mark.asyncio
    async def test_an_unreadable_counter_with_the_judge_on_stops_before_any_row(self) -> None:
        db = _Db([_memory(), _memory()], uuid4())

        result, detect, _, _ = await _run(
            db, counter=AsyncMock(return_value=None), config=_config(verify_mode="llm")
        )

        detect.assert_not_called()
        assert result.stopped == STOPPED_COUNTER_UNREADABLE
        assert (result.processed, result.judge_calls) == (0, None)
        assert result.last_id is None

    @pytest.mark.asyncio
    async def test_a_counter_lost_mid_run_with_the_judge_on_stops_there(self) -> None:
        rows = [_memory(), _memory(), _memory()]
        db = _Db(rows, uuid4())
        # start, before row 1, then unreadable before row 2.
        counter = AsyncMock(side_effect=[3, 3, None])

        result, detect, _, _ = await _run(
            db, counter=counter, config=_config(verify_mode="llm"), max_judge_calls=10
        )

        assert [c.args[1] for c in detect.await_args_list] == [rows[0]]
        assert result.stopped == STOPPED_COUNTER_UNREADABLE
        assert result.processed == 1
        assert result.judge_calls is None
        assert result.last_id == rows[0].id  # resume at row 2

    @pytest.mark.asyncio
    async def test_a_row_without_a_stored_vector_is_counted_not_run(self) -> None:
        with_vector, without = _memory(), _memory()
        db = _Db([with_vector, without], uuid4())

        result, detect, _, _ = await _run(db, vectors={str(with_vector.id): [0.2]})

        assert detect.await_count == 1
        assert (result.processed, result.no_vector) == (1, 1)

    @pytest.mark.asyncio
    async def test_a_row_that_changed_after_the_plan_is_skipped(self) -> None:
        kept, gone = _memory(), _memory()
        db = _Db([kept, gone], uuid4())
        db.gone.add(gone.id)

        result, detect, _, _ = await _run(db)

        assert detect.await_count == 1
        assert (result.processed, result.skipped) == (1, 1)

    @pytest.mark.asyncio
    async def test_suggestions_off_runs_nothing(self) -> None:
        db = _Db([_memory()], uuid4())

        result, detect, retrieve, read = await _run(db, config=_config(suggest=False))

        detect.assert_not_called()
        retrieve.assert_not_called()
        read.assert_not_called()
        assert result.stopped == STOPPED_SUGGESTIONS_OFF
        assert result.processed == 0

    @pytest.mark.asyncio
    async def test_an_unknown_context_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            await _run(_Db([_memory()], None))


class TestPlan:
    @pytest.mark.asyncio
    async def test_dry_run_counts_and_runs_nothing(self) -> None:
        db = _Db([_memory(), _memory()], uuid4())
        detect = AsyncMock()
        read = AsyncMock()
        retrieve = AsyncMock()
        with (
            patch("services.memory_service._create_knn_seed_edges", detect),
            patch("db.qdrant.retrieve_dense_vectors", retrieve),
            patch.object(task, "_read_counter", read),
        ):
            result = await run_supersede_catch_up(db, uuid4(), dry_run=True, limit=2)

        assert (result.eligible, result.planned, result.processed) == (5, 2, 0)
        detect.assert_not_called()
        retrieve.assert_not_called()
        read.assert_not_called()
        assert len(db.statements) == 3  # context, count, rows


class TestReadCounter:
    @pytest.mark.asyncio
    async def test_missing_key_is_zero_and_an_error_is_none(self) -> None:
        client = MagicMock()
        client.get = AsyncMock(side_effect=[None, "7", RuntimeError("down")])
        with patch("db.redis.get_redis_client", return_value=client):
            assert await task._read_counter("k") == 0
            assert await task._read_counter("k") == 7
            assert await task._read_counter("k") is None
