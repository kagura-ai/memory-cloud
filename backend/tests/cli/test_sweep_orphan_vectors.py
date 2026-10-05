"""Tests for ``cli/sweep_orphan_vectors`` (#1798).

The sweep itself is covered in ``tests/services/test_orphan_vector_sweep.py``;
this pins the command around it: read-only by default, ``--apply`` deletes,
and the report says what an operator needs to compare.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# The CLI bootstraps sys.path itself; import it the same way create_admin's test does.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import sweep_orphan_vectors as cli  # noqa: E402
from services.orphan_vector_sweep import CollectionSweep, SweepResult  # noqa: E402


def _result(*, dry_run: bool, deleted: int = 0) -> SweepResult:
    return SweepResult(
        dry_run=dry_run,
        grace=timedelta(hours=1),
        collections=[
            CollectionSweep(
                collection="kagura_memories",
                scanned=10,
                no_row=2,
                tombstoned=1,
                context_deleted=0,
                deleted=deleted,
            )
        ],
        live_embedded_memories=7,
    )


async def _no_db():
    yield object()


def _run(argv: list[str], sweep: AsyncMock) -> int:
    import asyncio

    with (
        patch.object(cli, "sweep_orphan_points", sweep),
        patch("cli._oneshot.get_db", _no_db),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


class TestSweepOrphanVectorsCommand:
    def test_default_is_a_read_only_plan(self, capsys):
        sweep = AsyncMock(return_value=_result(dry_run=True))

        assert _run([], sweep) == 0

        assert [call.kwargs["dry_run"] for call in sweep.await_args_list] == [True]
        out = capsys.readouterr().out
        assert "would delete 3" in out
        assert "dry run — pass --apply to write" in out

    def test_apply_plans_then_deletes_and_reports_what_is_left(self, capsys):
        sweep = AsyncMock(side_effect=[_result(dry_run=True), _result(dry_run=False, deleted=3)])

        assert _run(["--apply", "--yes"], sweep) == 0

        assert [call.kwargs["dry_run"] for call in sweep.await_args_list] == [True, False]
        out = capsys.readouterr().out
        assert "deleted 3 orphaned point(s)" in out
        assert "7 point(s) left; 7 live embedded memories" in out

    def test_apply_without_confirmation_deletes_nothing(self, capsys, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        sweep = AsyncMock(return_value=_result(dry_run=True))

        assert _run(["--apply"], sweep) == 0

        assert [call.kwargs["dry_run"] for call in sweep.await_args_list] == [True]
        assert "skipped" in capsys.readouterr().out

    def test_grace_hours_reaches_the_sweep(self):
        sweep = AsyncMock(return_value=_result(dry_run=True))

        _run(["--grace-hours", "0"], sweep)

        assert sweep.await_args.kwargs["grace"] == timedelta(0)

    def test_negative_grace_is_rejected(self):
        with pytest.raises(SystemExit):
            cli._parse(["--grace-hours", "-1"])

    def test_vector_store_error_exits_non_zero(self, capsys):
        sweep = AsyncMock(side_effect=RuntimeError("qdrant down"))

        assert _run([], sweep) == 1
        assert "error: qdrant down" in capsys.readouterr().err


class TestApplyThatDeletedLessThanAsked:
    def test_a_refused_apply_exits_non_zero_and_says_why(self, capsys):
        refused = _result(dry_run=False)
        refused.refused = "a merge or a Sleep rollback was still writing points"
        sweep = AsyncMock(side_effect=[_result(dry_run=True), refused])

        assert _run(["--apply", "--yes"], sweep) == 1

        captured = capsys.readouterr()
        assert "still writing points" in captured.err
        # Nothing was deleted: the error alone, no report.
        assert "deleted 0" not in captured.out
        assert "point(s) left" not in captured.out

    def test_a_refusal_after_some_deletes_reports_what_was_deleted(self, capsys):
        """#1869: the delete pass can be refused after earlier batches went
        through; the operator has to see that points were deleted."""
        stopped = _result(dry_run=False, deleted=2)
        stopped.collections[0].error = "collection dropped"
        stopped.refused = (
            "a merge or a Sleep rollback was still writing points after 30s; "
            "stopped after deleting 2"
        )
        sweep = AsyncMock(side_effect=[_result(dry_run=True), stopped])

        assert _run(["--apply", "--yes"], sweep) == 1

        captured = capsys.readouterr()
        assert "deleted 2 orphaned point(s)" in captured.out
        assert "kagura_memories: skipped: collection dropped" in captured.out
        assert "8 point(s) left; 7 live embedded memories" in captured.out
        assert "error: a merge or a Sleep rollback was still writing points" in captured.err

    def test_a_refusal_with_nothing_deleted_prints_only_the_error(self, capsys):
        """The ratio guard refuses in the apply pass before any delete: no report."""
        refused = _result(dry_run=False, deleted=0)
        refused.refused = "3 of 10 points look orphaned, over 20%; nothing deleted"
        sweep = AsyncMock(side_effect=[_result(dry_run=True), refused])

        assert _run(["--apply", "--yes"], sweep) == 1

        captured = capsys.readouterr()
        # The plan is shown, then the error alone: no apply report.
        assert "deleted" not in captured.out
        assert "point(s) left" not in captured.out
        assert "error: 3 of 10 points look orphaned" in captured.err

    def test_a_collection_skipped_during_apply_is_named(self, capsys):
        applied = _result(dry_run=False, deleted=3)
        applied.collections[0].error = "collection dropped"
        sweep = AsyncMock(side_effect=[_result(dry_run=True), applied])

        assert _run(["--apply", "--yes"], sweep) == 0

        assert "kagura_memories: skipped: collection dropped" in capsys.readouterr().out
