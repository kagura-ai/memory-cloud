"""#3038: the member's run read reports the run's index progress like the worker's.

The SQL side (a drain read at both endpoints) is in
``tests/integration/test_sync_indexer_drain_db.py``.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from models.sync_activity_schemas import IndexProgress, RunDetail, RunObservation
from services import sync_activity


def _row() -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        feed_id=uuid4(),
        operation_id=uuid4(),
        state="committed",
        expires_at=now + timedelta(hours=1),
        requires_approval=False,
        prepared_until=None,
        manifest={"records": 3, "chunks": 1},
        created_at=now,
        committed_at=now,
    )


@pytest.fixture
def detail(monkeypatch):
    """``run_detail`` over one committed run, its boundary and SQL replaced."""
    row, feed = _row(), SimpleNamespace(id=uuid4())

    @asynccontextmanager
    async def boundary(*_args, **_kwargs):
        yield None, None, None

    monkeypatch.setattr(sync_activity, "activity_boundary", boundary)
    monkeypatch.setattr(sync_activity, "_run", AsyncMock(return_value=(row, feed, 3)))
    monkeypatch.setattr(sync_activity, "index_status", AsyncMock(return_value="pending"))
    progress = AsyncMock(return_value={"indexed": 1, "total": 3})
    monkeypatch.setattr(sync_activity, "index_progress", progress)
    db = AsyncMock(spec=AsyncSession)
    counts = MagicMock()
    counts.one.return_value = (1, 3)
    db.execute.return_value = counts

    async def read() -> dict:
        return await sync_activity.run_detail(
            db, uuid4(), uuid4(), row.id, SimpleNamespace(user_id="u", client_id="c")
        )

    return SimpleNamespace(read=read, row=row, progress=progress, db=db)


async def test_run_detail_carries_the_workers_index_progress_for_its_run(detail):
    result = await detail.read()

    assert result["index_progress"] == {"indexed": 1, "total": 3}
    assert result["search"] == {"state": "pending", "reason": "index_pending"}
    detail.progress.assert_awaited_once_with(detail.db, detail.row)
    assert RunDetail.model_validate(result).index_progress == IndexProgress(indexed=1, total=3)


async def test_a_run_without_index_events_reads_zero_of_zero_and_search_stays_the_verdict(
    detail,
):
    detail.progress.return_value = {"indexed": 0, "total": 0}

    result = await detail.read()

    assert result["index_progress"] == {"indexed": 0, "total": 0}
    assert result["search"]["state"] == "pending"


def test_index_progress_is_an_optional_additive_field_of_the_run_read_only():
    assert "index_progress" not in RunDetail.model_json_schema()["required"]
    assert RunDetail.model_fields["index_progress"].default is None
    # The run list does not evaluate index evidence (its search state is
    # ``not_evaluated``): no progress there.
    assert "index_progress" not in RunObservation.model_fields


def test_index_progress_has_the_workers_shape():
    assert IndexProgress.model_validate({"indexed": 2, "total": 5}).model_dump() == {
        "indexed": 2,
        "total": 5,
    }
    with pytest.raises(ValidationError):
        IndexProgress.model_validate({"indexed": -1, "total": 0})
    with pytest.raises(ValidationError):
        IndexProgress.model_validate({"indexed": 0})
