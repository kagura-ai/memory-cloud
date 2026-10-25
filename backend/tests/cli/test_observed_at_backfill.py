"""``python -m src.cli.observed_at_backfill`` and its task (#2330).

Plan by default (counts, no write), apply on ``--apply --yes``; the task sets
the column value on the point each row names, skips a shared point and
isolates a failing row.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import observed_at_backfill as cli  # noqa: E402
from tasks.observed_at_backfill import (  # noqa: E402
    ObservedAtBackfill,
    backfill_observed_at_payloads,
)


async def _no_db():
    yield object()


def _run(argv: list[str], backfill: AsyncMock) -> int:
    with (
        patch.object(cli, "backfill_observed_at_payloads", backfill),
        patch("cli._oneshot.get_db", _no_db),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


class TestCommand:
    def test_default_is_a_read_only_plan(self, capsys):
        backfill = AsyncMock(return_value=ObservedAtBackfill(dry_run=True, rows=3))
        assert _run([], backfill) == 0
        assert [c.kwargs["dry_run"] for c in backfill.await_args_list] == [True]
        out = capsys.readouterr().out
        assert "embedded memories with observed_at: 3" in out
        assert "dry run" in out

    def test_apply_writes_and_reports(self, capsys):
        failed = uuid4()
        backfill = AsyncMock(
            side_effect=[
                ObservedAtBackfill(dry_run=True, rows=3),
                ObservedAtBackfill(
                    dry_run=False, rows=3, written=1, shared_skipped=1, failed=[failed]
                ),
            ]
        )
        assert _run(["--apply", "--yes"], backfill) == 0
        assert [c.kwargs["dry_run"] for c in backfill.await_args_list] == [True, False]
        out = capsys.readouterr().out
        assert "points written: 1" in out
        assert str(failed) in out


def _db(rows):
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


def _row(**overrides):
    return SimpleNamespace(
        id=overrides.get("id", uuid4()),
        summary_embedding_id=overrides.get("summary_embedding_id"),
        context_id=uuid4(),
        observed_at="2024-03-07T00:00:00Z",
    )


class TestTask:
    def test_dry_run_counts_only(self):
        update = AsyncMock()
        with patch("db.qdrant.update_memory_payload_in_qdrant", update):
            result = asyncio.run(backfill_observed_at_payloads(_db([_row(), _row()]), dry_run=True))
        assert result.rows == 2
        update.assert_not_awaited()

    def test_apply_sets_the_column_value_and_isolates_failures(self):
        good, shared, bad = _row(), _row(), _row()

        async def point(db, memory_id, point_id):
            return None if memory_id == shared.id else point_id

        update = AsyncMock(side_effect=[None, RuntimeError("down")])
        with (
            patch("db.qdrant.update_memory_payload_in_qdrant", update),
            patch("services.resource_indexer.unshared_point_id", side_effect=point),
            patch(
                "services.context_routing.resolve_collection_name",
                AsyncMock(return_value="kagura_memories"),
            ),
        ):
            result = asyncio.run(
                backfill_observed_at_payloads(_db([good, shared, bad]), dry_run=False)
            )
        assert (result.written, result.shared_skipped, result.failed) == (1, 1, [bad.id])
        first = update.await_args_list[0].kwargs
        assert first["memory_id"] == good.id
        assert first["payload_updates"] == {"observed_at": "2024-03-07T00:00:00Z"}
