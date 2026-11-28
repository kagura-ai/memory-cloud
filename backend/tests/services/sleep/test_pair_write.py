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
        # #3020: the content's whole length, for the judged-heads re-check.
        assert "char_length(memories.content)" in sql


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


# ------------------------------------------------------------ text recency


def _minutes(n: float):
    from datetime import datetime, timedelta

    return datetime(2026, 3, 1, 12, 0) + timedelta(minutes=n)


class TestTextRecency:
    """#3020: dedup's recency (#3019), shared so both phases order a pair alike."""

    def test_the_key_is_the_later_of_creation_and_the_last_text_edit(self) -> None:
        from services.sleep.pair_write import recency_key

        assert recency_key(_minutes(0), None) == _minutes(0)
        assert recency_key(_minutes(0), _minutes(30)) == _minutes(30)
        assert recency_key(_minutes(30), _minutes(0)) == _minutes(30)
        assert recency_key(None, None) is None
        assert recency_key("not a time", None) is None

    @staticmethod
    async def _unknowable(winner, *, began=None, **loser):
        from services.sleep.pair_write import recency_unknowable

        values = {
            "loser_text_edited_at": None,
            "loser_unsure_until": None,
            "loser_updated_at": None,
            "loser_unread": False,
        }
        values.update(loser)
        log_began = AsyncMock(return_value=began)
        result = await recency_unknowable(winner, log_began=log_began, **values)
        return result, log_began

    @pytest.mark.asyncio
    async def test_a_logged_edit_of_the_loser_is_known(self) -> None:
        result, log_began = await self._unknowable(
            _minutes(0), loser_text_edited_at=_minutes(-5), loser_updated_at=_minutes(60)
        )
        assert result is False
        log_began.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_stamp_at_or_before_the_winner_minute_is_known(self) -> None:
        result, log_began = await self._unknowable(_minutes(0), loser_updated_at=_minutes(0.5))
        assert result is False
        log_began.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_later_stamp_is_known_once_the_log_began_before_the_winner(self) -> None:
        result, _ = await self._unknowable(
            _minutes(0), loser_updated_at=_minutes(60), began=_minutes(-600)
        )
        assert result is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("began", [None, 120], ids=["no-log", "log-began-after"])
    async def test_a_later_stamp_without_a_log_that_covers_it_is_unknowable(self, began) -> None:
        result, log_began = await self._unknowable(
            _minutes(0),
            loser_updated_at=_minutes(60),
            began=None if began is None else _minutes(began),
        )
        assert result is True
        log_began.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unread_loser_with_a_later_stamp_is_unknowable(self) -> None:
        result, log_began = await self._unknowable(
            _minutes(0), loser_updated_at=_minutes(60), loser_unread=True, began=_minutes(-600)
        )
        assert result is True
        log_began.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_rollback_that_may_have_restored_a_later_edit_is_unknowable(self) -> None:
        result, _ = await self._unknowable(_minutes(0), loser_unsure_until=_minutes(30))
        assert result is True

    @pytest.mark.asyncio
    async def test_no_winner_recency_tells_nothing(self) -> None:
        result, _ = await self._unknowable(None, loser_updated_at=_minutes(60))
        assert result is False

    @pytest.mark.asyncio
    async def test_dedup_decides_through_the_shared_rule(self) -> None:
        """Dedup's keep-both check is the shared function, called with its
        per-run state (no copy of the rule left in dedup_merge)."""
        winner = SimpleNamespace(id=uuid4(), created_at=_minutes(0), version=1)
        loser = SimpleNamespace(id=uuid4(), created_at=_minutes(-60), updated_at=_minutes(30))
        phase = DedupMergePhase.__new__(DedupMergePhase)
        phase._log_began_read, phase._log_began_at = True, None
        shared = AsyncMock(return_value=True)
        with patch("services.sleep.dedup_merge.recency_unknowable", shared):
            assert await phase._recency_unknowable(winner, loser) is True
        args, kwargs = shared.await_args
        assert args == (_minutes(0),)
        assert kwargs["loser_updated_at"] == _minutes(30)
        assert kwargs["loser_text_edited_at"] is None
        assert kwargs["loser_unread"] is False
        assert kwargs["log_began"] == phase._revision_log_began


class TestDedupMergeDropsAppliedLinks:
    """#3020: a delete-mode merge deletes the loser's Sleep-applied links
    before it moves the loser's other edges onto the winner."""

    @pytest.mark.asyncio
    async def test_the_applied_links_go_before_the_transfer(self) -> None:
        phase = DedupMergePhase.__new__(DedupMergePhase)
        phase.db = AsyncMock()
        phase.collection_name = "kagura_memories"
        phase.pending_point_deletes = []
        calls = MagicMock()
        phase.edge_repo = calls
        calls.delete_sleep_applied_edges = AsyncMock(return_value=1)
        calls.transfer_edges = AsyncMock(return_value=0)
        winner, loser = SimpleNamespace(id=uuid4(), tags=[]), SimpleNamespace(id=uuid4(), tags=[])
        with (
            patch.object(phase, "_pinned_or_gone_since_fetch", AsyncMock(return_value=False)),
            patch("services.sleep.dedup_merge.unshared_point_id", AsyncMock(return_value=None)),
        ):
            assert await phase._execute_merge(winner, loser, "u", "w", "c") is True
        assert [name for name, *_ in calls.mock_calls] == [
            "delete_sleep_applied_edges",
            "transfer_edges",
        ]
        assert calls.delete_sleep_applied_edges.await_args.args == (loser.id, "u")

    def test_the_marker_predicate(self) -> None:
        from repositories.neural_edge import sleep_applied_supersedes

        sql = sleep_applied_supersedes().compile(dialect=postgresql.dialect())
        assert "neural_memory_edges.edge_type = " in str(sql)
        assert "neural_memory_edges.origin = " in str(sql)
        assert "CAST(neural_memory_edges.metadata AS JSONB) ->> " in str(sql)
        assert set(sql.params.values()) >= {"supersedes", "semantic", "sleep_supersede_apply"}
