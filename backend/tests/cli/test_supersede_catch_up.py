"""``python -m src.cli.supersede_catch_up`` (#2256).

Plan by default, apply on ``--apply --yes``; ``--context`` is required and the
report prints the tally the #2133 measurement reads. The task is stubbed;
it is tested in ``tests/services/test_supersede_catch_up.py``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import supersede_catch_up as cli  # noqa: E402
from tasks.supersede_catch_up import SupersedeCatchUp  # noqa: E402

_CTX = str(uuid4())


async def _no_db():
    yield object()


def _run(argv: list[str], task: AsyncMock) -> int:
    with (
        patch.object(cli, "run_supersede_catch_up", task),
        patch("cli._oneshot.get_db", _no_db),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


class TestCommand:
    def test_default_is_a_read_only_plan(self, capsys):
        task = AsyncMock(return_value=SupersedeCatchUp(dry_run=True, eligible=9, planned=3))
        assert _run(["--context", _CTX], task) == 0
        assert [c.kwargs["dry_run"] for c in task.await_args_list] == [True]
        call = task.await_args_list[0]
        assert str(call.args[1]) == _CTX
        assert call.kwargs["limit"] == 200
        assert call.kwargs["max_judge_calls"] == 50
        assert call.kwargs["include_unverified"] is False
        assert call.kwargs["before"] is None
        assert call.kwargs["after"] is None
        assert call.kwargs["after_id"] is None
        out = capsys.readouterr().out
        assert "rows without a supersede check: 9" in out
        assert "rows this run checks: 3" in out
        assert "dry run" in out

    def test_apply_runs_and_prints_the_tally(self, capsys):
        task = AsyncMock(
            side_effect=[
                SupersedeCatchUp(dry_run=True, eligible=3, planned=3),
                SupersedeCatchUp(
                    dry_run=False,
                    eligible=3,
                    planned=3,
                    processed=2,
                    detected=1,
                    unchanged=1,
                    no_vector=1,
                    judge_calls=4,
                    stopped="judge_budget",
                    last_created_at=datetime(2026, 10, 1, 8, 30, 0, 123456),
                    last_id=UUID(int=7),
                ),
            ]
        )
        argv = ["--context", _CTX, "--apply", "--yes", "--limit", "3", "--max-judge-calls", "4"]
        assert _run(argv, task) == 0
        assert [c.kwargs["dry_run"] for c in task.await_args_list] == [True, False]
        assert task.await_args_list[1].kwargs["limit"] == 3
        assert task.await_args_list[1].kwargs["max_judge_calls"] == 4
        out = capsys.readouterr().out
        assert "examined 2 memory(s)" in out
        for line in (
            "processed    2",
            "detected     1",
            "cleared      0",
            "no_vector    1",
            "judge_calls  4",
            "stopped      judge_budget",
            "failed       0",
            "resume       --after 2026-10-01T08:30:00.123456Z,00000000-0000-0000-0000-000000000007",
        ):
            assert line in out

    def test_an_unreadable_counter_prints_unknown(self, capsys):
        task = AsyncMock(
            side_effect=[
                SupersedeCatchUp(dry_run=True, planned=1),
                SupersedeCatchUp(dry_run=False, planned=1, processed=1, unchanged=1),
            ]
        )
        assert _run(["--context", _CTX, "--apply", "--yes"], task) == 0
        assert "judge_calls  unknown" in capsys.readouterr().out

    def test_nothing_planned_does_not_apply(self):
        task = AsyncMock(return_value=SupersedeCatchUp(dry_run=True))
        assert _run(["--context", _CTX, "--apply", "--yes"], task) == 0
        assert task.await_count == 1

    def test_include_unverified_before_and_after_are_passed(self):
        task = AsyncMock(return_value=SupersedeCatchUp(dry_run=True))
        argv = [
            "--context",
            _CTX,
            "--include-unverified",
            "--before",
            "2026-10-01T09:00:00+09:00",
            "--after",
            "2026-09-01T00:00:00.5Z,00000000-0000-0000-0000-000000000009",
        ]
        assert _run(argv, task) == 0
        call = task.await_args_list[0]
        assert call.kwargs["include_unverified"] is True
        assert call.kwargs["before"] == datetime(2026, 10, 1, 0, 0)
        assert call.kwargs["after"] == datetime(2026, 9, 1, 0, 0, 0, 500000)
        assert call.kwargs["after_id"] == UUID(int=9)

    def test_a_bare_after_instant_is_accepted(self):
        args = cli._parse(["--context", _CTX, "--after", "2026-09-01"])
        assert args.after == (datetime(2026, 9, 1), None)

    def test_yes_without_apply_is_a_usage_error(self):
        task = AsyncMock()
        assert _run(["--context", _CTX, "--yes"], task) == 2
        task.assert_not_called()

    def test_a_task_error_exits_1(self, capsys):
        task = AsyncMock(side_effect=ValueError("context x not found"))
        assert _run(["--context", _CTX], task) == 1
        assert "context x not found" in capsys.readouterr().err


class TestParse:
    def test_context_is_required(self):
        with pytest.raises(SystemExit) as exc:
            cli._parse([])
        assert exc.value.code == 2

    def test_context_must_be_a_uuid(self):
        with pytest.raises(SystemExit):
            cli._parse(["--context", "not-a-uuid"])

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-10-01", datetime(2026, 10, 1)),
            ("2026-10-01T12:30:00Z", datetime(2026, 10, 1, 12, 30)),
            ("2026-10-01T12:30:00+02:00", datetime(2026, 10, 1, 10, 30)),
        ],
    )
    def test_before_is_naive_utc(self, value, expected):
        assert cli._parse(["--context", _CTX, "--before", value]).before == expected

    @pytest.mark.parametrize(
        "argv",
        [
            ["--before", "yesterday"],
            ["--limit", "0"],
            ["--max-judge-calls", "-1"],
            ["--after", "2026-09-01,not-a-uuid"],
        ],
    )
    def test_bad_values_are_usage_errors(self, argv):
        with pytest.raises(SystemExit) as exc:
            cli._parse(["--context", _CTX, *argv])
        assert exc.value.code == 2

    def test_plan_and_apply_are_exclusive(self):
        with pytest.raises(SystemExit):
            cli._parse(["--context", _CTX, "--plan", "--apply"])
