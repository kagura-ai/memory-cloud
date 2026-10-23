"""Tests for ``cli/reprocess_erasures`` (#2243).

The re-processing itself runs against Postgres in
``tests/integration/test_reprocess_erasures_db.py``; this pins the command
around it: a read-only dry run by default (``--plan`` / ``--dry-run`` name
it), ``--apply`` writes after the confirmation, the scope is required, and
an error is an error exit.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import reprocess_erasures as cli  # noqa: E402

REQUEST_ID = uuid4()
SKIPPED_ID = uuid4()


def _result(*, dry_run: bool, changed: bool = True) -> cli.ReprocessResult:
    result = cli.ReprocessResult(dry_run=dry_run, scanned=2)
    result.skipped.append((SKIPPED_ID, cli.SKIPPED_NOT_PSEUDONYMIZED))
    if changed:
        result.changed.append(
            cli.RequestChange(
                request_id=REQUEST_ID,
                completed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
                details_restored=2,
                details_restored_unavailable=1,
                details_stripped=3,
                scrub_marked=4,
                payload={}
                if dry_run
                else {"details_payload_synced": 2, "details_payload_skipped_shared": 1},
            )
        )
    return result


async def _no_db():
    yield object()


def _run(argv: list[str], worker: AsyncMock) -> int:
    with (
        patch.object(cli, "reprocess_erasures", worker),
        patch("cli._oneshot.get_db", _no_db),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


class TestReprocessErasuresCommand:
    @pytest.mark.parametrize("argv", [["--all"], ["--all", "--plan"], ["--all", "--dry-run"]])
    def test_default_is_a_read_only_dry_run(self, argv, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True))

        assert _run(argv, worker) == 0

        assert [call.kwargs["dry_run"] for call in worker.await_args_list] == [True]
        assert worker.await_args.kwargs["request_id"] is None
        out = capsys.readouterr().out
        row = next(line for line in out.splitlines() if line.startswith(str(REQUEST_ID)))
        assert row.split()[1:] == ["2026-08-01T12:00:00Z", "2", "1", "3", "4"]
        assert f"skipped  {SKIPPED_ID}  stores the account id" in out
        assert "2 completed erasure(s): would change 1, skipped 1" in out
        assert "dry run — pass --apply to write" in out

    def test_apply_writes_after_the_plan(self, capsys):
        worker = AsyncMock(side_effect=[_result(dry_run=True), _result(dry_run=False)])

        assert _run(["--all", "--apply", "--yes"], worker) == 0

        assert [call.kwargs["dry_run"] for call in worker.await_args_list] == [True, False]
        out = capsys.readouterr().out
        assert (
            "changed 1 erasure request(s): 2 memory details restored, 1 cleared (unavailable), "
            "3 revision prior(s) stripped, 4 marked scrubbed"
        ) in out
        assert (
            f"payload  {REQUEST_ID}  synced 2, skipped 1 (tombstone 0, shared 1, edited since 0), "
            "re-queued for embedding 0"
        ) in out

    def test_the_confirmation_is_honoured(self, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True))

        with patch("builtins.input", return_value="n") as prompt:
            assert _run(["--all", "--apply"], worker) == 0

        prompt.assert_called_once()
        assert "Change 1 erasure request(s)" in prompt.call_args.args[0]
        assert [call.kwargs["dry_run"] for call in worker.await_args_list] == [True]
        assert "skipped" in capsys.readouterr().out

    def test_nothing_to_change_never_writes(self, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True, changed=False))

        assert _run(["--all", "--apply", "--yes"], worker) == 0

        assert [call.kwargs["dry_run"] for call in worker.await_args_list] == [True]
        out = capsys.readouterr().out
        assert "2 completed erasure(s): would change 0, skipped 1" in out
        assert "dry run" not in out

    def test_request_reaches_the_worker(self, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True))

        assert _run(["--request", str(REQUEST_ID)], worker) == 0

        assert worker.await_args.kwargs["request_id"] == REQUEST_ID

    def test_an_error_is_an_error_exit(self, capsys):
        worker = AsyncMock(side_effect=LookupError(f"{REQUEST_ID} is not a completed request"))

        assert _run(["--request", str(REQUEST_ID)], worker) == 1

        assert "is not a completed request" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--apply"],
            ["--all", "--request", str(REQUEST_ID)],
            ["--all", "--plan", "--apply"],
            ["--request", "not-a-uuid"],
        ],
    )
    def test_the_scope_is_required_and_exclusive(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            cli._parse(argv)
        assert exc.value.code == 2
