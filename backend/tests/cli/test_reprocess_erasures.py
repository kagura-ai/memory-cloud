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

    @pytest.mark.parametrize("argv", [["--all", "--yes"], ["--all", "--plan", "--yes"]])
    def test_yes_without_apply_is_a_usage_error(self, argv, capsys):
        """#2268: the plan never writes, so ``--yes`` without ``--apply`` is
        refused (exit 2) instead of silently running the plan only."""
        worker = AsyncMock(return_value=_result(dry_run=True))

        assert _run(argv, worker) == 2

        worker.assert_not_awaited()
        assert "--yes needs --apply" in capsys.readouterr().err

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


def test_the_applied_report_names_already_queued_and_failed_requeues(capsys):
    """#2266: failed patches on rows already queued, and re-queues that failed,
    are printed (the latter points at the warnings that carry the ids)."""
    result = cli.ReprocessResult(dry_run=False, scanned=1)
    result.changed.append(
        cli.RequestChange(
            request_id=REQUEST_ID,
            completed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            details_restored=3,
            details_restored_unavailable=0,
            details_stripped=0,
            scrub_marked=0,
            embedding_requeued=1,
            payload={
                "details_payload_synced": 1,
                "details_payload_skipped_requeued": 1,
                "details_payload_requeue_failed": 1,
            },
        )
    )
    cli._print_applied(result)
    out = capsys.readouterr().out
    assert "re-queued for embedding 1, already queued 1, NOT re-queued 1" in out


def test_a_repaired_request_is_listed_without_a_completion_time(capsys):
    """#2268: a request that failed after its Postgres step has no completion
    time (``-``) and is named as repaired; the account id is never printed."""
    result = cli.ReprocessResult(dry_run=True, scanned=1)
    result.changed.append(
        cli.RequestChange(
            request_id=REQUEST_ID,
            completed_at=None,
            details_restored=0,
            details_restored_unavailable=0,
            details_stripped=0,
            scrub_marked=0,
            repaired=True,
        )
    )
    assert result.changes == 1  # the repair alone is a change
    cli._print_plan(result)
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith(str(REQUEST_ID)))
    assert row.split()[1:] == ["-", "0", "0", "0", "0"]
    assert f"would repair  {REQUEST_ID}  failed after its Postgres step" in out
    assert cli._summary(result).endswith(", 1 request row(s) repaired")


@pytest.mark.parametrize(
    ("summary", "completed_at", "expected"),
    [
        (
            {"postgres": {"memory_scrub_at": "2026-09-01T12:00:00Z"}},
            datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
            datetime(2026, 9, 1, 12, 0),
        ),
        (
            {"postgres": {"users": 1}},
            datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
            datetime(2026, 9, 1, 13, 0),
        ),
        ({"postgres": {"users": 1}}, None, None),
        (None, None, None),
    ],
)
def test_the_rerun_bound_prefers_the_scrub_time(summary, completed_at, expected):
    """#2268: ``memory_scrub_at`` when stored, else the completion time, else
    no bound (a legacy request that failed after its Postgres step)."""
    assert cli._existed_at(summary, completed_at) == expected


def test_a_legacy_failed_request_is_bounded_an_hour_after_its_start():
    """#2268 review: no scrub or completion time, but ``started_at``: the
    sweep's crash cutoff after it bounds the strip. Ahead of it, the stored
    times win."""
    started = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert cli._existed_at({"postgres": {"users": 1}}, None, started) == datetime(2026, 9, 1, 13, 0)
    completed = datetime(2026, 9, 1, 12, 5, tzinfo=UTC)
    assert cli._existed_at({"postgres": {}}, completed, started) == datetime(2026, 9, 1, 12, 5)


@pytest.mark.parametrize(
    ("summary", "wrote"),
    [
        ({"postgres": {"memories_scrubbed": 2}}, True),
        ({"postgres": {"memories_scrubbed": 0, "memory_revisions_pseudonymized": 3}}, True),
        ({"postgres": {"memories_scrubbed": 0, "memory_revisions_pseudonymized": 0}}, False),
        ({"postgres": {"users": 1}}, False),
        (None, False),
    ],
)
def test_only_an_erasure_that_wrote_the_pseudonym_can_fail_the_probe(summary, wrote):
    """#2268 review: a subject with nothing left is repaired, not skipped."""
    assert cli._wrote_pseudonym(summary) is wrote
