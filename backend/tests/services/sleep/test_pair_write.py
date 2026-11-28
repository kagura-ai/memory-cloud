"""#3020: the locked recheck and the supersedes edge writer Sleep's phases share.

``locked_refusals`` is dedup's #1519/#1524/#2996/#3019 recheck, extracted
unchanged: these cases pin every reason per row state, so the extraction kept
dedup's rule. ``write_sleep_supersedes_edge`` is dedup's shadow-merge write;
dedup calls it with no metadata and no declared-link protection, as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from models.memory import MEMORY_TYPE_TIME
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.pair_write import locked_refusals, write_sleep_supersedes_edge
from services.supersede_verify import CONTENT_HEAD_CHARS


def _obj(**fields) -> SimpleNamespace:
    values = {"id": uuid4(), "user_id": "u", "context_id": uuid4(), "version": 1}
    values.update(fields)
    return SimpleNamespace(**values)


def _row(obj: SimpleNamespace, **fields) -> SimpleNamespace:
    values = {
        "id": obj.id,
        "pinned": False,
        "tool_triggered": False,
        "resource_owned": False,
        "deleted_at": None,
        "type": "note",
        "user_id": obj.user_id,
        "context_id": obj.context_id,
        "version": obj.version,
        "tags": ["t"],
        "supersede_candidate": {"memory_id": "x"},
        "summary": "s",
        "content_head": "c",
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _db(rows) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


class TestLockedRefusals:
    @pytest.mark.asyncio
    async def test_no_reason_returns_the_locked_rows(self) -> None:
        a, b = _obj(), _obj()
        rows = [_row(a, summary="new text"), _row(b, content_head="old head", tags=["x"])]
        reasons, locked = await locked_refusals(_db(rows), (("newer", a), ("older", b)))
        assert reasons == []
        assert set(locked) == {a.id, b.id}
        assert locked[a.id].summary == "new text"
        assert locked[b.id].content_head == "old head"
        assert locked[b.id].tags == ["x"]
        assert locked[a.id].supersede_candidate == {"memory_id": "x"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            ({"deleted_at": "2026-11-01"}, "deleted"),
            ({"pinned": True}, "pinned"),
            ({"tool_triggered": True}, "tool_triggered"),
            ({"resource_owned": True}, "resource_owned"),
            ({"type": MEMORY_TYPE_TIME}, "time"),
            ({"user_id": "someone-else"}, "moved"),
            ({"context_id": uuid4()}, "moved"),
            ({"version": 2}, "edited"),
        ],
    )
    async def test_each_reason_per_row(self, change, reason) -> None:
        a, b = _obj(), _obj()
        reasons, _ = await locked_refusals(
            _db([_row(a), _row(b, **change)]), (("winner", a), ("loser", b))
        )
        assert reasons == [f"loser_{reason}"]

    @pytest.mark.asyncio
    async def test_a_missing_row_and_the_first_reason_of_each_row(self) -> None:
        a, b = _obj(), _obj()
        # Deleted beats pinned on one row; the other row is gone.
        reasons, locked = await locked_refusals(
            _db([_row(a, deleted_at="x", pinned=True)]), (("newer", a), ("older", b))
        )
        assert reasons == ["newer_deleted", "older_missing"]
        assert set(locked) == {a.id}

    @pytest.mark.asyncio
    async def test_a_version_that_is_not_an_int_reads_unchanged(self) -> None:
        a, b = _obj(version=MagicMock()), _obj()
        reasons, _ = await locked_refusals(
            _db([_row(a, version=7), _row(b)]), (("newer", a), ("older", b))
        )
        assert reasons == []

    @pytest.mark.asyncio
    async def test_the_statement_locks_and_binds_the_content_head(self) -> None:
        a, b = _obj(), _obj()
        db = _db([_row(a), _row(b)])
        await locked_refusals(db, (("newer", a), ("older", b)))
        stmt = db.execute.await_args.args[0]
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "FOR UPDATE" in sql
        assert "substr(memories.content" in sql
        assert str(CONTENT_HEAD_CHARS) not in sql
        assert CONTENT_HEAD_CHARS in compiled.params.values()
        for column in ("tags", "supersede_candidate", "summary", "version"):
            assert f"memories.{column}" in sql


class TestDedupWrapperKeepsItsRule:
    @pytest.mark.asyncio
    async def test_refusal_logs_and_counts_a_resource_owned_side(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        winner, loser = _obj(), _obj()
        phase.db = _db([_row(winner), _row(loser, resource_owned=True)])
        with patch("services.sleep.dedup_merge.logger") as log:
            refused = await phase._pinned_or_gone_since_fetch(winner, loser)
        assert refused is True
        assert phase._resource_owned_refusals == 1
        log.info.assert_called_once()
        assert log.info.call_args.args[0] == "dedup_merge_skipped_pinned"
        assert log.info.call_args.kwargs["reasons"] == ["loser_resource_owned"]

    @pytest.mark.asyncio
    async def test_a_pinned_side_refuses_without_the_resource_counter(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        winner, loser = _obj(), _obj()
        phase.db = _db([_row(winner, pinned=True), _row(loser)])
        assert await phase._pinned_or_gone_since_fetch(winner, loser) is True
        assert phase._resource_owned_refusals == 0

    @pytest.mark.asyncio
    async def test_no_reason_lets_the_merge_run(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        winner, loser = _obj(), _obj()
        phase.db = _db([_row(winner), _row(loser)])
        assert await phase._pinned_or_gone_since_fetch(winner, loser) is False


class TestWriteSleepSupersedesEdge:
    def _repo(self, prior=None) -> MagicMock:
        repo = MagicMock()
        repo.get_edge = AsyncMock(return_value=prior)
        repo.create_or_update_edge = AsyncMock(return_value="edge")
        return repo

    @pytest.mark.asyncio
    async def test_dedup_shape_no_metadata_no_protection_no_refresh(self) -> None:
        repo = self._repo()
        newer, older = uuid4(), uuid4()
        prior, edge = await write_sleep_supersedes_edge(
            repo, user_id="u", newer_id=newer, older_id=older, workspace_id="w", context_id="c"
        )
        assert (prior, edge) == (None, "edge")
        repo.get_edge.assert_awaited_once_with("u", newer, older, "w", "c")
        assert repo.create_or_update_edge.await_args.kwargs == {
            "user_id": "u",
            "src_id": newer,
            "dst_id": older,
            "edge_type": "supersedes",
            "weight": 1.0,
            "confidence": 1.0,
            "workspace_id": "w",
            "context_id": "c",
            "origin": "semantic",
            "edge_metadata": None,
            "protect_declared_link": False,
            "return_fresh_edge": False,
        }

    @pytest.mark.asyncio
    async def test_metadata_and_protection_pass_through_with_a_refresh(self) -> None:
        repo = self._repo()
        await write_sleep_supersedes_edge(
            repo,
            user_id="u",
            newer_id=uuid4(),
            older_id=uuid4(),
            workspace_id="w",
            context_id="c",
            edge_metadata={"source": "s", "report_id": "r"},
            protect_declared_link=True,
        )
        kwargs = repo.create_or_update_edge.await_args.kwargs
        assert kwargs["edge_metadata"] == {"source": "s", "report_id": "r"}
        assert kwargs["protect_declared_link"] is True
        assert kwargs["return_fresh_edge"] is True

    @pytest.mark.asyncio
    async def test_a_retyped_row_is_snapshotted(self) -> None:
        existing = SimpleNamespace(
            edge_type="neural_association",
            origin="hebbian",
            weight=0.4,
            confidence=0.9,
            edge_metadata={"source": "tag_cooccurrence"},
        )
        prior, _ = await write_sleep_supersedes_edge(
            self._repo(existing),
            user_id="u",
            newer_id=uuid4(),
            older_id=uuid4(),
            workspace_id="w",
            context_id="c",
        )
        assert prior == {
            "edge_type": "neural_association",
            "origin": "hebbian",
            "weight": 0.4,
            "confidence": 0.9,
            "edge_metadata": {"source": "tag_cooccurrence"},
        }

    @pytest.mark.asyncio
    async def test_an_existing_supersedes_row_is_not_snapshotted(self) -> None:
        existing = SimpleNamespace(
            edge_type="supersedes",
            origin="semantic",
            weight=1.0,
            confidence=1.0,
            edge_metadata=None,
        )
        prior, _ = await write_sleep_supersedes_edge(
            self._repo(existing),
            user_id="u",
            newer_id=uuid4(),
            older_id=uuid4(),
            workspace_id="w",
            context_id="c",
        )
        assert prior is None
