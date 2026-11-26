"""#2065: Sleep addresses a resource-indexer row's own point, not its row id.

A row the resource indexer wrote owns the indexer's point, whose id is
``Memory.point_id`` (the uuid5 of the document version), never the row id; the
same document indexed into two contexts that share a collection is one point
for two rows; ``unshared_point_id`` leaves a point another live row still
names alone. Since #2996 the archive, the merge and the importance
re-evaluation refuse such a row outright (the source owns its lifecycle), so
its point is never queued for a delete and its importance is never rescored.
A plain row's archive still queues the point under its id (#2565). Rows are
real; the vector store is faked.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from services.resource_indexer import resource_point_id, unshared_point_id, unshared_point_ids
from utils.datetime import utcnow

_USER = "resource-row-points"


def _row(**overrides) -> Memory:
    fields = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "user_id": _USER,
        "summary": "a memory summary",
        "content": "c",
        "type": "note",
        "importance": 0.5,
        "scope": "working",
        "client": "pytest",
    }
    fields.update(overrides)
    return Memory(**fields)


def _indexer_row(doc_id: str = "doc-1", version: int = 2, **overrides) -> Memory:
    """A row the resource indexer wrote: it names its document's uuid5 point and
    is persistent from creation (#2996)."""
    fields = {
        "summary": f"[resource] {doc_id} v{version}",
        "content": '{"title": "Annual report"}',
        "details": {"resource_id": "reports", "doc_id": doc_id, "version": str(version)},
        "summary_embedding_id": resource_point_id("reports", doc_id, version),
        "scope": "persistent",
        "promoted_at": utcnow(),
    }
    fields.update(overrides)
    return _row(**fields)


async def _queued_points(db: AsyncSession, memory_id: UUID) -> list[UUID]:
    """The points queued for ``memory_id`` in ``db``'s transaction (#2565)."""
    from models.deferred_point_delete import DeferredPointDelete

    return list(
        (
            await db.execute(
                select(DeferredPointDelete.point_id).where(
                    DeferredPointDelete.memory_id == memory_id
                )
            )
        ).scalars()
    )


class TestUnsharedPointId:
    async def test_a_point_under_the_rows_own_id_needs_no_lookup(self):
        db = AsyncMock()
        memory_id = uuid4()
        assert await unshared_point_id(db, memory_id, memory_id) == memory_id
        db.execute.assert_not_awaited()

    async def test_an_indexer_row_owns_its_documents_point(self, db_session: AsyncSession):
        row = _indexer_row()
        db_session.add(row)
        await db_session.flush()

        assert await unshared_point_id(db_session, row.id, row.point_id) == resource_point_id(
            "reports", "doc-1", 2
        )

    async def test_a_point_another_live_row_names_is_not_this_rows_alone(
        self, db_session: AsyncSession
    ):
        row, other = _indexer_row(), _indexer_row()
        db_session.add_all([row, other])
        await db_session.flush()

        assert await unshared_point_id(db_session, row.id, row.point_id) is None

    async def test_a_tombstone_naming_the_point_does_not_share_it(self, db_session: AsyncSession):
        row = _indexer_row()
        tombstone = _indexer_row(deleted_at=utcnow() - timedelta(days=1))
        db_session.add_all([row, tombstone])
        await db_session.flush()

        assert await unshared_point_id(db_session, row.id, row.point_id) == row.point_id


class TestUnsharedPointIds:
    """#2459: the batched sibling answers each row as ``unshared_point_id`` does, in one query."""

    async def test_points_under_the_rows_own_ids_need_no_lookup(self):
        db = AsyncMock()
        a, b = uuid4(), uuid4()
        assert await unshared_point_ids(db, [(a, a), (b, b)]) == {a: a, b: b}
        db.execute.assert_not_awaited()

    async def test_each_row_gets_the_single_row_answer(self, db_session: AsyncSession):
        from sqlalchemy import event

        owner = _indexer_row(doc_id="doc-own")
        shared_a, shared_b = _indexer_row(doc_id="doc-shared"), _indexer_row(doc_id="doc-shared")
        kept = _indexer_row(doc_id="doc-kept")
        tombstone = _indexer_row(doc_id="doc-kept", deleted_at=utcnow() - timedelta(days=1))
        api_id = uuid4()
        api = _row(id=api_id, summary_embedding_id=api_id)
        db_session.add_all([owner, shared_a, shared_b, kept, tombstone, api])
        await db_session.flush()
        rows = [owner, shared_a, shared_b, kept, api]

        statements: list[str] = []

        def _statement(conn, cursor, statement, parameters, context, executemany):
            if "summary_embedding_id" in statement:
                statements.append(statement)

        engine = db_session.bind.sync_engine
        event.listen(engine, "before_cursor_execute", _statement)
        try:
            batched = await unshared_point_ids(db_session, [(r.id, r.point_id) for r in rows])
        finally:
            event.remove(engine, "before_cursor_execute", _statement)

        assert len(statements) == 1
        assert batched == {
            r.id: await unshared_point_id(db_session, r.id, r.point_id) for r in rows
        }
        assert batched == {
            owner.id: owner.point_id,
            shared_a.id: None,
            shared_b.id: None,
            kept.id: kept.point_id,
            api.id: api.id,
        }


class TestMergeDeletesTheLosersOwnPoint:
    @staticmethod
    def _phase(db_session: AsyncSession):
        from services.sleep.dedup_merge import DedupMergePhase

        with (
            patch("services.sleep.dedup_merge.NeuralEdgeRepository"),
            patch("services.sleep.dedup_merge.EmbeddingService"),
        ):
            phase = DedupMergePhase(db_session, AsyncMock())
        phase.edge_repo = AsyncMock()
        phase.edge_repo.transfer_edges = AsyncMock(return_value=0)
        return phase

    @pytest.mark.parametrize("shared", [False, True])
    async def test_an_indexer_loser_is_never_merged_and_keeps_its_point(
        self, db_session: AsyncSession, shared: bool
    ):
        """#2996: the locked re-check refuses the pair, so the point stays
        whether or not another row names it."""
        winner, loser = _row(summary="winner"), _indexer_row()
        db_session.add_all([winner, loser])
        if shared:
            # The same document indexed into another context of the collection.
            db_session.add(_indexer_row())
        await db_session.flush()

        phase = self._phase(db_session)
        assert await phase._execute_merge(winner, loser, _USER, None, None) is False

        assert phase.pending_point_deletes == []
        assert await _queued_points(db_session, loser.id) == []

    async def test_a_plain_losers_point_goes(self, db_session: AsyncSession):
        winner, loser = _row(summary="winner"), _row(summary="loser")
        db_session.add_all([winner, loser])
        await db_session.flush()

        phase = self._phase(db_session)
        assert await phase._execute_merge(winner, loser, _USER, None, None) is True

        assert phase.pending_point_deletes == [(loser.id, loser.id, "kagura_memories")]
        assert await _queued_points(db_session, loser.id) == [loser.id]


class TestImportanceSkipsTheIndexerPoint:
    @staticmethod
    async def _run(db_session: AsyncSession, row: Memory) -> tuple[AsyncMock, set]:
        from services.llm_service import LLMResponse
        from services.sleep.importance_reeval import ImportanceReevalPhase
        from services.sleep.reporter import SleepBudget

        config = MagicMock()
        config.sleep_importance_reeval_enabled = True
        config.importance_ema_alpha = 0.3
        config.sleep_llm_provider = "openai"
        config.sleep_llm_model = "gpt-5-nano"
        llm = AsyncMock()
        llm.complete_json = AsyncMock(
            return_value=LLMResponse(
                parsed={"scores": [{"label": "A", "importance": 0.8}]},
                total_tokens=42,
                input_tokens=30,
                output_tokens=12,
                cached_input_tokens=0,
                provider="openai",
                model="gpt-5-nano",
                tokenizer_version="o200k_base",
            )
        )
        with patch(
            "services.sleep.importance_reeval.update_memory_payload_in_qdrant", new=AsyncMock()
        ) as payload:
            result = await ImportanceReevalPhase(db_session, llm).execute(
                config, row.user_id, None, None, SleepBudget()
            )
        return payload, result.changed_memory_ids

    @pytest.mark.parametrize("indexer_row", [False, True])
    async def test_only_a_summary_point_gets_the_payload(
        self, db_session: AsyncSession, indexer_row: bool
    ):
        """#2996: the source owns an indexer row's importance. Even handed to the
        write (the row became the indexer's after the candidate fetch), the
        UPDATE refuses it: its importance stays, and neither the payload write
        nor the reindex is spent on it."""
        from services.sleep.importance_reeval import ImportanceReevalPhase

        user = f"{_USER}-{uuid4()}"
        stale = utcnow() - timedelta(days=60)
        make = _indexer_row if indexer_row else _row
        row = make(user_id=user, updated_at=stale, created_at=stale)
        db_session.add(row)
        await db_session.flush()

        with patch.object(
            ImportanceReevalPhase, "_fetch_candidates", AsyncMock(return_value=[row])
        ):
            payload, changed = await self._run(db_session, row)

        await db_session.refresh(row)
        if indexer_row:
            payload.assert_not_awaited()
            assert changed == set()
            assert row.importance == 0.5
        else:
            assert payload.await_args.kwargs["memory_id"] == row.id
            assert changed == {row.id}
            assert row.importance != 0.5


class TestRollbackImportanceSkipsTheIndexerPoint:
    @pytest.mark.parametrize("indexer_row", [False, True])
    async def test_the_restored_importance_reaches_only_a_summary_point(self, indexer_row: bool):
        from mcp_server.tools import sleep as sleep_tools

        returned = MagicMock(
            summary_embedding_id=resource_point_id("reports", "doc-1", 2) if indexer_row else None,
            resource_id="reports" if indexer_row else None,
            resource_doc_id="doc-1" if indexer_row else None,
            resource_version=2 if indexer_row else None,
        )
        restored = MagicMock()
        restored.one_or_none.return_value = returned
        action = MagicMock(memory_id=uuid4(), details={"old_importance": 0.4})
        ctx = MagicMock(user_id="u", ctx_id_str="", collection_name="coll")
        db = MagicMock()
        db.execute = AsyncMock(return_value=restored)
        summary = {"importance_restored": 0, "importance_kept": 0}
        with patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as payload:
            await sleep_tools._undo_update_importance(db, action, ctx, summary)

        assert summary["importance_restored"] == 1
        if indexer_row:
            payload.assert_not_awaited()
        else:
            assert payload.await_args.kwargs["memory_id"] == action.memory_id
