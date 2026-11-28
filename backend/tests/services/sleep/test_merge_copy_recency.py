"""#3051: a context-merge copy keeps its source's last text-edit time.

A merge copies each memory as a new row at version 1, so the copy has no
revision log of its own. The source's last text edit travels on the copy
(``Memory.carried_text_edited_at``), and the recency rule dedup uses
(``read_text_edit_times`` → ``text_edit_times``) reads it: a copy is as recent
as the text it holds, not as its creation time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.memory_versions import (
    TextEditEvent,
    TextEditReads,
    TextEditTimes,
    carried_text_edit_at,
    read_text_edit_times,
    text_edit_times,
)
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.reporter import SleepBudget


def _now() -> datetime:
    return datetime(2026, 11, 27, 12, 0)


def _ago(days: float) -> datetime:
    return _now() - timedelta(days=days)


def _event(
    version: int,
    days_ago: float,
    *,
    writer: str = "api",
    text: bool = True,
    restored_from: int | None = None,
) -> TextEditEvent:
    return TextEditEvent(
        version=version, writer=writer, at=_ago(days_ago), text=text, restored_from=restored_from
    )


def _row(
    *, version: Any = 1, carried: Any = None, created: datetime | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        version=version,
        carried_text_edited_at=carried,
        created_at=created or _ago(300),
    )


def _rev(memory_id: UUID, version: int, days_ago: float, **fields: Any) -> SimpleNamespace:
    """One row of ``text_edit_events_query``."""
    return SimpleNamespace(
        memory_id=memory_id,
        version=version,
        writer=fields.get("writer", "api"),
        at=_ago(days_ago),
        text=fields.get("text", True),
        restored_from=fields.get("restored_from"),
    )


def _db(rows=(), error: Exception | None = None) -> MagicMock:
    db = MagicMock()
    savepoint = MagicMock()
    savepoint.__aenter__ = AsyncMock(return_value=None)
    savepoint.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=savepoint)
    result = MagicMock()
    result.all.return_value = list(rows)
    db.execute = AsyncMock(side_effect=error) if error else AsyncMock(return_value=result)
    return db


class TestTheWalkStartsFromTheCarriedTime:
    """``text_edit_times(events, carried=...)``: the carried time is the text
    the copy was created with (version 1), the first candidate of the walk."""

    def test_no_event_leaves_the_carried_time(self) -> None:
        assert text_edit_times((), carried=_ago(30)) == TextEditTimes(last=_ago(30))

    def test_an_edit_of_the_copy_is_later(self) -> None:
        times = text_edit_times([_event(2, 5)], carried=_ago(30))
        assert times == TextEditTimes(last=_ago(5))

    def test_an_edit_of_other_fields_leaves_the_carried_time(self) -> None:
        times = text_edit_times([_event(2, 5, text=False)], carried=_ago(30))
        assert times == TextEditTimes(last=_ago(30))

    def test_a_rollback_of_the_copys_edits_puts_the_carried_text_back(self) -> None:
        """The session rollback names the block it reverted (from version 2):
        the text of version 1, written at the carried time, is back."""
        times = text_edit_times(
            [_event(2, 5), _event(3, 1, writer="rollback", restored_from=2)], carried=_ago(30)
        )
        assert times == TextEditTimes(last=_ago(30))

    def test_a_rollback_naming_no_block_drops_the_carried_time_too(self) -> None:
        """Which version's text it put back is unknown: the walk keeps the
        doubt and falls back to the creation time, never a later one."""
        times = text_edit_times([_event(2, 5), _event(3, 1, writer="rollback")], carried=_ago(30))
        assert times == TextEditTimes(last=None, unsure_until=_ago(5))

    def test_no_carried_time_walks_as_before(self) -> None:
        assert text_edit_times([_event(2, 5)]) == TextEditTimes(last=_ago(5))
        assert text_edit_times(()) == TextEditTimes()


class TestCarriedTextEditAt:
    @pytest.mark.parametrize("value", [None, MagicMock(), "2026-11-01", 3])
    def test_anything_but_a_datetime_is_none(self, value: Any) -> None:
        assert carried_text_edit_at(SimpleNamespace(carried_text_edited_at=value)) is None

    def test_a_row_without_the_attribute_is_none(self) -> None:
        assert carried_text_edit_at(SimpleNamespace(id=uuid4())) is None

    def test_a_datetime_is_returned(self) -> None:
        assert carried_text_edit_at(SimpleNamespace(carried_text_edited_at=_ago(3))) == _ago(3)


class TestReadTextEditTimes:
    async def test_a_copy_at_version_1_reads_nothing_and_is_its_carried_time(self) -> None:
        db = _db()
        copy, plain = _row(carried=_ago(30)), _row()

        reads = await read_text_edit_times(db, [copy, plain])

        db.execute.assert_not_called()
        db.begin_nested.assert_not_called()
        assert reads == TextEditReads(times={copy.id: TextEditTimes(last=_ago(30))})

    async def test_an_edited_copy_is_walked_from_its_carried_time(self) -> None:
        copy = _row(version=3, carried=_ago(30))
        db = _db(
            [
                _rev(copy.id, 2, 5),
                _rev(copy.id, 3, 1, writer="rollback", restored_from="2"),
            ]
        )

        reads = await read_text_edit_times(db, [copy])

        assert db.execute.await_count == 1
        assert reads.times == {copy.id: TextEditTimes(last=_ago(30))}
        assert reads.unread == frozenset()

    async def test_skipped_and_repeated_ids_are_read_once(self) -> None:
        a, b = _row(version=2), _row(version=2)
        db = _db([_rev(a.id, 2, 4), _rev(b.id, 2, 3)])

        reads = await read_text_edit_times(db, [a, a, b], skip={b.id})

        assert db.execute.await_count == 1
        assert reads.times == {a.id: TextEditTimes(last=_ago(4))}

    async def test_a_failed_read_leaves_the_read_rows_unread(self) -> None:
        """An edited copy whose read failed is not given its carried time: an
        edit of the copy itself, later than it, is unknown."""
        edited_copy = _row(version=2, carried=_ago(30))
        copy = _row(carried=_ago(20))
        error = RuntimeError("statement timeout")
        db = _db(error=error)

        reads = await read_text_edit_times(db, [edited_copy, copy])

        assert reads.times == {
            edited_copy.id: TextEditTimes(),
            copy.id: TextEditTimes(last=_ago(20)),
        }
        assert reads.unread == frozenset({edited_copy.id})
        assert reads.error is error

    async def test_ids_are_read_in_chunks(self, monkeypatch) -> None:
        from services import memory_versions

        monkeypatch.setattr(memory_versions, "TEXT_EDIT_READ_CHUNK", 2)
        rows = [_row(version=2) for _ in range(5)]
        db = _db()

        reads = await read_text_edit_times(db, rows)

        assert db.execute.await_count == 3
        assert db.begin_nested.call_count == 1
        assert reads.times == {row.id: TextEditTimes() for row in rows}


def _mem(*, created: datetime, version: Any = 1, carried: Any = None, **fields: Any) -> MagicMock:
    m = MagicMock()
    m.id = uuid4()
    m.created_at = created
    m.updated_at = fields.get("updated")
    m.version = version
    m.carried_text_edited_at = carried
    m.source_type = "api"
    m.importance = 0.5
    m.summary = fields.get("summary", f"memory {m.id.hex[:6]}")
    m.type = "note"
    m.tags = []
    return m


class TestDedupReadsTheCarriedTime:
    """The issue's scenario in the dedup phase: T was written in January and
    corrected in March in its source context; C (February) still holds the
    stale value. After a context merge both are copies at version 1 with a
    fresh ``updated_at``; T's copy carries March."""

    def _pair(self) -> tuple[MagicMock, MagicMock]:
        t_copy = _mem(created=_ago(300), version=1, carried=_ago(240), updated=_ago(1))
        c_copy = _mem(created=_ago(270), version=1, carried=None, updated=_ago(1))
        return t_copy, c_copy

    async def test_the_copy_holding_the_correction_wins(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        phase.db = _db()
        phase._log_began_at = _ago(365)
        phase._log_began_read = True
        t_copy, c_copy = self._pair()
        key = tuple(sorted([t_copy.id, c_copy.id], key=str))

        await phase._load_text_edit_times([t_copy, c_copy])
        decisions = await phase._judge_cluster(
            [t_copy, c_copy], {key: 0.99}, False, "u", "ctx", "ws", SleepBudget(), MagicMock()
        )

        assert phase._recency_key(t_copy) == _ago(240)
        assert phase._recency_key(c_copy) == c_copy.created_at
        assert decisions == [(t_copy.id, c_copy.id)]
        phase.db.execute.assert_not_called()

    async def test_a_copy_that_is_a_mock_without_the_column_reads_its_creation(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        phase.db = _db()
        m = _mem(created=_ago(10))
        m.carried_text_edited_at = MagicMock()

        await phase._load_text_edit_times([m])

        assert phase._text_edited_at == {}
        assert phase._recency_key(m) == m.created_at
