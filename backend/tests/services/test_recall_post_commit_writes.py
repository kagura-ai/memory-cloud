"""recall() writes its statistics after the read commits (Issue #1985).

The access-stat bump, auto-promotion and the Hebbian pass used to run inside
the recall transaction, row by row, so overlapping recalls deadlocked and a
failed statistics write turned into a 503 for a page that was already built.
They now run after ``self.db.commit()``, each in an isolated session, and a
database failure there is logged instead of failing the recall.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from models.schemas import MemoryResponse, RecallRequest
from services.memory_service import MemoryService


def _memory(**overrides) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "summary": "s",
        "context_summary": None,
        "type": "note",
        "importance": 0.5,
        "scope": "working",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "last_used_at": None,
        "access_count": 0,
        "accessed_by_clients": None,
        "confidence": 1.0,
        "client": "test",
        "tags": [],
        "context": None,
        "source_uri": None,
        "source_type": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _response(memory: SimpleNamespace) -> MemoryResponse:
    return MemoryResponse(
        memory_id=memory.id,
        summary=memory.summary,
        context_summary=None,
        tags=[],
        context=None,
        type=memory.type,
        importance=memory.importance,
        scope=memory.scope,
        created_at=memory.created_at,
        client=memory.client,
    )


def _isolated(service: MemoryService) -> MagicMock:
    """Make ``service._isolated_session()`` hand out one recorded session."""
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    service._isolated_session = MagicMock(return_value=session)  # type: ignore[method-assign]
    return session


@pytest.fixture
def service() -> MemoryService:
    db = MagicMock(spec=AsyncSession)
    # The conftest gate runs the post-commit write only for a session bound to
    # an AsyncEngine; ``_isolated`` below replaces the session it would open.
    db.bind = MagicMock(spec=AsyncEngine)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return MemoryService(db)


class TestBuildResponsesIsReadOnly:
    @pytest.mark.asyncio
    async def test_no_write_on_the_request_session(self, service):
        memory = _memory()
        service.memory_repo = MagicMock()
        service._resolve_supersede_candidates = AsyncMock(return_value={})

        responses = await service._recall_build_responses(
            RecallRequest(query="q", k=5),
            [{"id": "p1", "score": 0.9}],
            {"p1": memory},
            shadow_map={},
            contradiction_map={},
            user_id="u",
        )

        assert [r.memory_id for r in responses] == [memory.id]
        assert service.memory_repo.mock_calls == []
        service.db.commit.assert_not_awaited()


class TestRecallOrder:
    @pytest.mark.asyncio
    async def test_access_write_runs_after_the_recall_commit(self):
        # A bare MagicMock session skips the #1941 capacity reads (tests/conftest.py).
        service = MemoryService(MagicMock())
        memory_id = str(uuid4())
        order: list[str] = []
        service.search_service.hybrid_search = AsyncMock(
            return_value=[{"id": memory_id, "score": 0.9, "hybrid_score": 0.9}]
        )
        row = MagicMock(
            id=memory_id,
            summary="s",
            context_summary=None,
            type="note",
            importance=0.5,
            scope="working",
            created_at=datetime.utcnow(),
            client="test",
            tags=[],
            context=None,
            source_uri=None,
            source_type=None,
        )
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        service.db.execute = AsyncMock(return_value=result)

        async def commit() -> None:
            order.append("commit")

        async def record_access(_self, responses, memories, *, client) -> None:
            order.append(f"access:{client}:{len(responses)}")

        service.db.commit = AsyncMock(side_effect=commit)
        with patch.object(MemoryService, "_recall_record_access", record_access):
            await service.recall(
                request=RecallRequest(query="q", k=5),
                user_id="u",
                current_context_id=uuid4(),
                current_workspace_id=uuid4(),
            )

        assert order[:2] == ["commit", "access:api:1"]


class TestRecordAccess:
    @pytest.mark.asyncio
    async def test_bumps_the_page_and_promotes_what_qualifies(self, service):
        old = _memory(created_at=datetime.utcnow() - timedelta(days=30))
        fresh = _memory()
        already = _memory(scope="persistent", importance=0.9)
        responses = [_response(m) for m in (old, fresh, already)]
        session = _isolated(service)
        repo = MagicMock()
        repo.bulk_update_access_stats = AsyncMock(
            return_value={old.id: 1, fresh.id: 1, already.id: 9}
        )
        repo.bulk_promote_to_persistent = AsyncMock(return_value=[old.id])

        with patch("services.memory_service.MemoryRepository", return_value=repo) as cls:
            await service._recall_record_access(
                responses,
                {"a": old, "b": fresh, "c": already},
                client="claude",
            )

        cls.assert_called_once_with(session)
        repo.bulk_update_access_stats.assert_awaited_once_with(
            [old.id, fresh.id, already.id], "claude"
        )
        # #1046: recall is surfacing, never adoption.
        assert repo.bulk_update_access_stats.await_args.kwargs == {}
        repo.bulk_promote_to_persistent.assert_awaited_once_with([old.id], skip_locked=True)
        session.commit.assert_awaited_once()
        assert [r.scope for r in responses] == ["persistent", "working", "persistent"]
        service.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_client_counts_toward_promotion(self, service):
        memory = _memory(accessed_by_clients=["api"])
        _isolated(service)
        repo = MagicMock()
        repo.bulk_update_access_stats = AsyncMock(return_value={memory.id: 1})
        repo.bulk_promote_to_persistent = AsyncMock(return_value=[])

        with patch("services.memory_service.MemoryRepository", return_value=repo):
            await service._recall_record_access([_response(memory)], {"a": memory}, client="claude")

        repo.bulk_promote_to_persistent.assert_awaited_once_with([memory.id], skip_locked=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            OperationalError("UPDATE memories", {}, Exception("deadlock detected")),
            ConnectionRefusedError(111, "refused"),
            RuntimeError("a bug"),
        ],
        ids=["database", "connection", "bug"],
    )
    async def test_any_failure_is_logged_not_raised(self, service, error):
        """The page is already committed: nothing in this step may fail it."""
        memory = _memory(created_at=datetime.utcnow() - timedelta(days=30))
        responses = [_response(memory)]
        session = _isolated(service)

        async def fail(*args, **kwargs):
            raise error

        repo = MagicMock()
        repo.bulk_update_access_stats = AsyncMock(side_effect=fail)
        repo.bulk_promote_to_persistent = AsyncMock()

        with (
            patch("services.memory_service.MemoryRepository", return_value=repo),
            patch("services.memory_service.logger") as logger,
        ):
            await service._recall_record_access(responses, {"a": memory}, client="api")

        repo.bulk_promote_to_persistent.assert_not_awaited()
        session.commit.assert_not_awaited()
        assert responses[0].scope == "working"
        logger.warning.assert_called_once()
        assert logger.warning.call_args.args == ("access_stats_update_failed",)
        assert logger.warning.call_args.kwargs["error_type"] == type(error).__name__
        assert logger.warning.call_args.kwargs["exc_info"] is True

    @pytest.mark.asyncio
    async def test_empty_page_opens_no_session(self, service):
        service._isolated_session = MagicMock()  # type: ignore[method-assign]

        await service._recall_record_access([], {}, client="api")

        service._isolated_session.assert_not_called()


class TestHebbianSession:
    @pytest.mark.asyncio
    async def test_learning_runs_on_its_own_session_and_fails_open(self, service):
        session = _isolated(service)

        async def failing_commit() -> None:
            raise OperationalError("INSERT neural_memory_edges", {}, Exception("deadlock"))

        session.commit = AsyncMock(side_effect=failing_commit)
        service._recall_learn_on_session = AsyncMock()  # type: ignore[method-assign]

        await service._recall_run_hebbian_learning(
            RecallRequest(query="q", k=5),
            [],
            {},
            user_id="u",
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
        )

        assert service._recall_learn_on_session.await_args.args[0] is session
        session.commit.assert_awaited_once()
        service.db.commit.assert_not_awaited()
        service.db.rollback.assert_not_awaited()


class TestIsolatedSession:
    @pytest.mark.asyncio
    async def test_binds_to_the_request_session_engine(self, service):
        engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/none")
        service.db.bind = engine
        try:
            isolated = service._isolated_session()
            assert isinstance(isolated, AsyncSession)
            assert isolated.bind is engine
            assert isolated is not service.db
            await isolated.close()
        finally:
            await engine.dispose()


class TestHebbianLearningLock:
    """#1985: one learning pass per graph owner; a busy owner is skipped."""

    async def _learn(self, service: MemoryService, graph_db: MagicMock) -> None:
        await service._recall_learn_on_session(
            graph_db,
            RecallRequest(query="q", k=5),
            [],
            {},
            user_id="u",
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
        )

    @pytest.mark.asyncio
    async def test_busy_owner_skips_without_touching_the_graph(self, service):
        graph_db = MagicMock(spec=AsyncSession)
        config = AsyncMock()
        with (
            patch(
                "services.memory_service.try_lock_graph_learning",
                AsyncMock(return_value=False),
            ) as lock,
            patch("neural.config.NeuralMemoryConfig.from_db", config),
            patch("services.memory_service.logger") as logger,
        ):
            await self._learn(service, graph_db)

        lock.assert_awaited_once_with(graph_db, "u")
        config.assert_not_awaited()
        graph_db.execute.assert_not_called()
        logger.info.assert_called_once_with("hebbian_update_skipped_busy", user_id="u")

    @pytest.mark.asyncio
    async def test_free_owner_runs_the_pass(self, service):
        graph_db = MagicMock(spec=AsyncSession)

        class _Reached(Exception):
            pass

        async def stop(*args, **kwargs):
            raise _Reached

        with (
            patch(
                "services.memory_service.try_lock_graph_learning",
                AsyncMock(return_value=True),
            ),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(side_effect=stop)),
            pytest.raises(_Reached),
        ):
            await self._learn(service, graph_db)
