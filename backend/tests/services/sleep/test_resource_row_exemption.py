"""#2996: rows the resource indexer owns are out of reach of every automated pass.

The source owns a resource-indexed row's lifecycle, whatever the context's
settings: Sleep's archive, its dedup merge and its importance re-evaluation
leave such a row alone, at the candidate fetch and again at the write.
``resource_owned_predicate`` is the SQL counterpart of ``owns_resource_point``;
the first class pins that the two agree on every row shape the writers
produce. Rows are real (``db_session``); the vector store is not touched.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory, not_resource_owned_predicate, resource_owned_predicate
from services.resource_indexer import owns_resource_point, resource_point_id
from utils.datetime import utcnow


def _user() -> str:
    return f"resource-row-exemption-{uuid4()}"


def _row(user_id: str, **overrides) -> Memory:
    old = utcnow() - timedelta(days=31)
    fields = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "user_id": user_id,
        "summary": "a memory summary",
        "content": "c",
        "type": "note",
        "importance": 0.5,
        "scope": "working",
        "client": "pytest",
        "access_count": 0,
        "reference_count": 0,
        "created_at": old,
        "updated_at": old,
    }
    fields.update(overrides)
    return Memory(**fields)


def _key(doc_id: str = "doc-1", version: int | str | None = 2) -> dict:
    return {"resource_id": "reports", "doc_id": doc_id, "version": version}


def _indexer_row(user_id: str, doc_id: str = "doc-1", **overrides) -> Memory:
    """A row the resource indexer wrote: it names its document's uuid5 point."""
    fields = {
        "summary": f"[reports] {doc_id} v2",
        "type": "resource_data",
        "source": "resource_ingest",
        "details": _key(doc_id),
        "summary_embedding_id": resource_point_id("reports", doc_id, 2),
    }
    fields.update(overrides)
    return _row(user_id, **fields)


class TestPredicateAgreesWithOwnsResourcePoint:
    async def test_every_row_shape_answers_the_same(self, db_session: AsyncSession):
        user = _user()
        own_id, copy_id, bare_id = uuid4(), uuid4(), uuid4()
        rows = {
            "indexer row": _indexer_row(user),
            "indexer row, working": _indexer_row(user, doc_id="doc-w", scope="working"),
            # remember(external_id, details={doc_id, version}): its point is its id.
            "remember(external_id) row": _row(
                user,
                id=own_id,
                details=_key("doc-r"),
                summary_embedding_id=own_id,
                source="mcp_remember",
            ),
            # The same row after the indexer matched it by natural key (#2164).
            "re-pointed remember row": _row(
                user,
                details=_key("doc-p"),
                summary_embedding_id=resource_point_id("reports", "doc-p", 2),
                source="mcp_remember",
            ),
            # A context merge copies an indexer row under a new id and point.
            "merge copy": _row(
                user,
                id=copy_id,
                details=_key("doc-c"),
                summary_embedding_id=copy_id,
                source="resource_ingest",
            ),
            "no version": _row(user, details=_key("doc-n", None)),
            "non-numeric version": _row(user, details=_key("doc-x", "v2")),
            "full key, no point yet": _row(user, details=_key("doc-e")),
            "plain row": _row(user, id=bare_id, summary_embedding_id=bare_id),
            "plain row, no point": _row(user),
        }
        db_session.add_all(rows.values())
        await db_session.flush()
        for row in rows.values():
            await db_session.refresh(row)

        sql = dict(
            (
                await db_session.execute(
                    select(Memory.id, resource_owned_predicate()).where(
                        Memory.id.in_([r.id for r in rows.values()])
                    )
                )
            )
            .tuples()
            .all()
        )
        negated = dict(
            (
                await db_session.execute(
                    select(Memory.id, not_resource_owned_predicate()).where(
                        Memory.id.in_([r.id for r in rows.values()])
                    )
                )
            )
            .tuples()
            .all()
        )

        python = {name: owns_resource_point(row) for name, row in rows.items()}
        assert {name: sql[row.id] for name, row in rows.items()} == python
        # The exclusion side is the exact complement: never NULL.
        assert {name: negated[row.id] for name, row in rows.items()} == {
            name: not owned for name, owned in python.items()
        }
        assert python == {
            "indexer row": True,
            "indexer row, working": True,
            "remember(external_id) row": False,
            "re-pointed remember row": True,
            "merge copy": False,
            "no version": False,
            "non-numeric version": False,
            "full key, no point yet": False,
            "plain row": False,
            "plain row, no point": False,
        }


class TestConsolidationLeavesResourceRowsAlone:
    @staticmethod
    def _phase(db_session: AsyncSession):
        from services.sleep.consolidation import ConsolidationPhase

        with patch("services.sleep.consolidation.GraphService"):
            return ConsolidationPhase(db_session, AsyncMock())

    async def test_the_fetch_skips_a_working_resource_row(self, db_session: AsyncSession):
        user = _user()
        plain, resource = _row(user), _indexer_row(user, scope="working")
        db_session.add_all([plain, resource])
        await db_session.flush()

        fetched = await self._phase(db_session)._fetch_working_memories(user, None, None)

        assert [m.id for m in fetched] == [plain.id]

    async def test_the_archive_stamp_refuses_a_resource_row(self, db_session: AsyncSession):
        """The row may become the indexer's between the fetch and the stamp (a
        re-pointed ``remember(external_id)`` row): the UPDATE re-checks."""
        user = _user()
        resource = _indexer_row(user, scope="working")
        db_session.add(resource)
        await db_session.flush()

        phase = self._phase(db_session)
        assert await phase._archive_tombstone(user, resource) is False

        await db_session.refresh(resource)
        assert resource.deleted_at is None
        assert phase.pending_point_deletes == []


class TestDedupLeavesResourceRowsAlone:
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

    async def test_the_fetch_skips_a_resource_row(self, db_session: AsyncSession):
        user = _user()
        plain, resource = _row(user), _indexer_row(user, scope="persistent")
        db_session.add_all([plain, resource])
        await db_session.flush()

        fetched = await self._phase(db_session)._fetch_active_memories(user, None, None)

        assert [m.id for m in fetched] == [plain.id]

    @pytest.mark.parametrize("resource_side", ["winner", "loser"])
    async def test_the_locked_recheck_refuses_a_pair_with_a_resource_row(
        self, db_session: AsyncSession, resource_side: str
    ):
        user = _user()
        plain, resource = _row(user, summary="near duplicate"), _indexer_row(user)
        db_session.add_all([plain, resource])
        await db_session.flush()
        winner, loser = (resource, plain) if resource_side == "winner" else (plain, resource)

        phase = self._phase(db_session)
        assert await phase._execute_merge(winner, loser, user, None, None) is False

        for row in (winner, loser):
            await db_session.refresh(row)
            assert row.deleted_at is None
        assert phase.pending_point_deletes == []


class TestImportanceLeavesResourceRowsAlone:
    async def test_a_resource_row_is_no_candidate(self, db_session: AsyncSession):
        from services.sleep.importance_reeval import ImportanceReevalPhase

        user = _user()
        stale = utcnow() - timedelta(days=60)
        plain = _row(user, updated_at=stale, created_at=stale)
        resource = _indexer_row(user, updated_at=stale, created_at=stale)
        db_session.add_all([plain, resource])
        await db_session.flush()

        fetched = await ImportanceReevalPhase(db_session, MagicMock())._fetch_candidates(
            user, None, None
        )

        assert [m.id for m in fetched] == [plain.id]
