"""``python -m src.cli.backfill_deleted_context_files`` (#1966).

Plan by default, apply on ``--apply`` (confirmed, or ``--yes``), one line per
deleted context, totals, exit codes — with the service patched; the service
itself is pinned against Postgres in tests/integration/test_storage_backfill_db.py.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import backfill_deleted_context_files as cli  # noqa: E402
from services.storage_backfill import (  # noqa: E402
    DeletedContextFiles,
    DeletedContextFilesBackfill,
)

WS = uuid4()
CTX = uuid4()
OLD = uuid4()


def _found(*, dry_run: bool) -> DeletedContextFilesBackfill:
    result = DeletedContextFilesBackfill(dry_run=dry_run)
    result.contexts = [
        DeletedContextFiles(
            context_id=CTX,
            workspace_id=WS,
            deleted_at=datetime(2026, 9, 30, 12, 0, 0),
            files=3,
            uploaded_bytes=220,
            past_restore_window=False,
        ),
        DeletedContextFiles(
            context_id=OLD,
            workspace_id=WS,
            deleted_at=datetime(2026, 8, 1, 12, 0, 0),
            files=1,
            uploaded_bytes=10,
            past_restore_window=True,
        ),
    ]
    if not dry_run:
        result.files_soft_deleted = 4
        result.released_bytes = {WS: 270}
    return result


async def _no_db():
    yield object()


def _run(argv: list[str], backfill: AsyncMock) -> int:
    with (
        patch.object(cli, "backfill_deleted_context_files", backfill),
        patch("cli._oneshot.get_db", _no_db),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


class TestBackfillCommand:
    def test_default_is_a_read_only_plan(self, capsys):
        backfill = AsyncMock(return_value=_found(dry_run=True))
        assert _run([], backfill) == 0
        assert [call.kwargs["dry_run"] for call in backfill.await_args_list] == [True]
        out = capsys.readouterr().out
        assert str(CTX) in out and "220" in out
        assert "2 context(s) in 1 workspace(s), 4 file(s), 230 uploaded bytes" in out
        assert "past window: objects removed at the next file GC" in out
        assert "warning    1 context(s) are past the file restore window" in out
        assert "dry run — pass --apply to write" in out

    def test_apply_soft_deletes_and_reports_the_release(self, capsys):
        backfill = AsyncMock(side_effect=[_found(dry_run=True), _found(dry_run=False)])
        assert _run(["--apply", "--yes"], backfill) == 0
        assert [call.kwargs["dry_run"] for call in backfill.await_args_list] == [True, False]
        out = capsys.readouterr().out
        assert "soft-deleted 4 file(s) in 2 deleted context(s), 230 uploaded bytes" in out
        assert "files soft-deleted: 4" in out
        assert f"released   {WS}  270 bytes" in out

    def test_workspace_flag_scopes_the_run(self, capsys):
        backfill = AsyncMock(return_value=_found(dry_run=True))
        assert _run(["--workspace", str(WS)], backfill) == 0
        assert backfill.await_args.kwargs["workspace_id"] == WS
        assert backfill.await_args.kwargs["include_past_window"] is False

    def test_past_window_is_opt_in(self, capsys):
        applied = _found(dry_run=False)
        applied.skipped_past_window = [OLD]
        backfill = AsyncMock(side_effect=[_found(dry_run=True), applied])
        assert _run(["--apply", "--yes"], backfill) == 0
        out = capsys.readouterr().out
        assert "skipped unless --include-past-window is given" in out
        assert f"skipped    {OLD}  past the restore window; pass --include-past-window" in out

        backfill = AsyncMock(side_effect=[_found(dry_run=True), _found(dry_run=False)])
        assert _run(["--apply", "--yes", "--include-past-window"], backfill) == 0
        assert backfill.await_args.kwargs["include_past_window"] is True

    def test_nothing_to_do_exits_zero_without_applying(self, capsys):
        backfill = AsyncMock(return_value=DeletedContextFilesBackfill(dry_run=True))
        assert _run(["--apply", "--yes"], backfill) == 0
        assert backfill.await_count == 1  # the plan only; nothing to write
        assert "no live files in deleted contexts" in capsys.readouterr().out

    def test_declined_confirmation_writes_nothing(self, capsys):
        backfill = AsyncMock(return_value=_found(dry_run=True))
        with patch("cli._oneshot.input", return_value="n", create=True):
            assert _run(["--apply"], backfill) == 0
        assert backfill.await_count == 1
        assert "skipped" in capsys.readouterr().out

    def test_error_is_a_non_zero_exit(self, capsys):
        backfill = AsyncMock(side_effect=RuntimeError("database unreachable"))
        assert _run([], backfill) == 1
        assert "database unreachable" in capsys.readouterr().err
