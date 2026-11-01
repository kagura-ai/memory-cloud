"""``python -m src.cli.resource_point_metadata_backfill`` (#2254).

Plan by default (counts, no write), apply on ``--apply --yes``; the task
itself is covered in ``tests/tasks/test_resource_point_metadata_backfill.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import resource_point_metadata_backfill as cli  # noqa: E402
from tasks.resource_point_metadata_backfill import (  # noqa: E402
    ResourcePointMetadataBackfill,
)


async def _no_db():
    yield object()


def _run(argv: list[str], backfill: AsyncMock) -> int:
    with (
        patch.object(cli, "backfill_resource_point_metadata", backfill),
        patch("cli._oneshot.get_db", _no_db),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


def test_default_is_a_read_only_plan(capsys):
    backfill = AsyncMock(
        return_value=ResourcePointMetadataBackfill(dry_run=True, rows=3, contexts=2)
    )
    assert _run([], backfill) == 0
    assert [c.kwargs["dry_run"] for c in backfill.await_args_list] == [True]
    out = capsys.readouterr().out
    assert "resource-indexed memories with their own point: 3" in out
    assert "contexts: 2" in out
    assert "dry run" in out


def test_explicit_plan_is_the_same_read_only_run(capsys):
    backfill = AsyncMock(return_value=ResourcePointMetadataBackfill(dry_run=True, rows=1))
    assert _run(["--plan"], backfill) == 0
    assert [c.kwargs["dry_run"] for c in backfill.await_args_list] == [True]


def test_yes_without_apply_is_a_usage_error():
    backfill = AsyncMock()
    assert _run(["--yes"], backfill) == 2
    backfill.assert_not_awaited()


def test_apply_writes_and_reports(capsys):
    failed = uuid4()
    workspace = uuid4()
    backfill = AsyncMock(
        side_effect=[
            ResourcePointMetadataBackfill(dry_run=True, rows=3, contexts=1),
            ResourcePointMetadataBackfill(
                dry_run=False, rows=3, contexts=1, written=1, shared_skipped=1, failed=[failed]
            ),
        ]
    )
    assert _run(["--apply", "--yes", "--workspace", str(workspace)], backfill) == 0
    assert [c.kwargs["dry_run"] for c in backfill.await_args_list] == [True, False]
    assert {c.kwargs["workspace_id"] for c in backfill.await_args_list} == {workspace}
    out = capsys.readouterr().out
    assert "points written: 1" in out
    assert "skipped    1 point(s) shared with another live memory" in out
    assert str(failed) in out


def test_apply_with_nothing_to_do_does_not_write(capsys):
    backfill = AsyncMock(return_value=ResourcePointMetadataBackfill(dry_run=True, rows=0))
    assert _run(["--apply", "--yes"], backfill) == 0
    assert [c.kwargs["dry_run"] for c in backfill.await_args_list] == [True]
