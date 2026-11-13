"""``python -m src.cli.file_scan`` (#565): status and rescan.

``status`` prints the backlog (counts and one age only); ``rescan`` is a plan
by default and writes on ``--apply`` (confirmed, or ``--yes``). ``--errors``
needs ``--since`` or ``--file`` so every failure is never one flag away. The
service is patched here; the selection itself is pinned against Postgres in
tests/integration/test_file_scan_engine_db.py.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import file_scan as cli  # noqa: E402
from services.file_scan_service import FileScanBacklog, RescanSelection  # noqa: E402

FILE = uuid4()


async def _no_db():
    yield AsyncMock()


def _run(argv: list[str], **patches) -> int:
    with (
        patch("cli._oneshot.get_db", _no_db),
        patch.object(cli, "get_db", _no_db),
        patch.object(cli, "_scan_enabled", return_value=patches.pop("enabled", True)),
    ):
        with patch.multiple(cli, **patches) if patches else _null():
            return asyncio.run(cli._main(cli._parse(argv)))


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _selection(n: int = 1) -> RescanSelection:
    return RescanSelection(rows=tuple((uuid4(), "error", "scan_failed") for _ in range(n)))


class TestStatus:
    def test_prints_counts_and_age_only(self, capsys):
        backlog = FileScanBacklog(
            enabled=True,
            due_pending=2,
            oldest_due_pending_at=datetime(2026, 11, 13, 9, 0, 0),
            waiting_pending=5,
            errored=1,
            quarantined=3,
            unscanned=40,
        )
        assert _run(["status"], file_scan_backlog=AsyncMock(return_value=backlog)) == 0
        out = capsys.readouterr().out
        assert "scanning        on" in out
        assert "due pending     2 (oldest due 2026-11-13T09:00:00Z)" in out
        assert "waiting         5" in out
        assert "scan failed     1" in out
        assert "quarantined     3" in out
        assert "unscanned       40" in out


class TestRescan:
    def test_file_plan_is_read_only(self, capsys):
        requeue = AsyncMock(return_value=_selection())
        assert _run(["rescan", "--file", str(FILE)], requeue_for_rescan=requeue) == 0
        assert [c.kwargs["dry_run"] for c in requeue.await_args_list] == [True]
        assert requeue.await_args.kwargs["file_id"] == FILE
        assert requeue.await_args.kwargs["errors_only"] is False
        out = capsys.readouterr().out
        assert "1 file(s) to rescan" in out
        assert "dry run — pass --apply to write" in out

    def test_apply_writes(self, capsys):
        requeue = AsyncMock(side_effect=[_selection(2), _selection(2)])
        assert (
            _run(
                ["rescan", "--errors", "--since", "2026-11-01T00:00:00", "--apply", "--yes"],
                requeue_for_rescan=requeue,
            )
            == 0
        )
        assert [c.kwargs["dry_run"] for c in requeue.await_args_list] == [True, False]
        assert requeue.await_args.kwargs["errors_only"] is True
        assert requeue.await_args.kwargs["errors_since"] == datetime(2026, 11, 1)
        assert "requeued 2 file(s)" in capsys.readouterr().out

    def test_since_with_an_offset_is_converted_to_utc(self):
        requeue = AsyncMock(return_value=_selection(0))
        assert (
            _run(
                ["rescan", "--errors", "--since", "2026-11-01T09:00:00+09:00"],
                requeue_for_rescan=requeue,
            )
            == 0
        )
        assert requeue.await_args.kwargs["errors_since"] == datetime(2026, 11, 1, 0, 0, 0)

    def test_errors_needs_since_or_file(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli._parse(["rescan", "--errors"])
        assert exc.value.code == 2

    def test_needs_a_selector(self):
        with pytest.raises(SystemExit) as exc:
            cli._parse(["rescan"])
        assert exc.value.code == 2

    def test_refused_while_scanning_is_off(self, capsys):
        requeue = AsyncMock(return_value=_selection())
        assert _run(["rescan", "--file", str(FILE)], requeue_for_rescan=requeue, enabled=False) == 1
        requeue.assert_not_awaited()
        assert "FILE_SCAN_ENABLED" in capsys.readouterr().err


def test_parse_since_helper():
    assert cli._parse_since("2026-11-01") == datetime(2026, 11, 1)
    aware = datetime(2026, 11, 1, 1, 0, tzinfo=timezone(timedelta(hours=1)))
    assert cli._parse_since(aware.isoformat()) == datetime(2026, 11, 1, 0, 0)
    assert cli._parse_since("2026-11-01T00:00:00Z") == datetime(2026, 11, 1)
