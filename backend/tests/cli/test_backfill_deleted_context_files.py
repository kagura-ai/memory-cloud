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


def _found(*, dry_run: bool) -> DeletedContextFilesBackfill:
    result = DeletedContextFilesBackfill(dry_run=dry_run)
    result.contexts = [
        DeletedContextFiles(
            context_id=CTX,
            workspace_id=WS,
            deleted_at=datetime(2026, 9, 30, 12, 0, 0),
            files=3,
            uploaded_bytes=220,
        )
    ]
    if not dry_run:
        result.files_soft_deleted = 3
        result.released_bytes = {WS: 260}
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
        assert "1 context(s) in 1 workspace(s), 3 file(s), 220 uploaded bytes" in out
        assert "dry run — pass --apply to write" in out

    def test_apply_soft_deletes_and_reports_the_release(self, capsys):
        backfill = AsyncMock(side_effect=[_found(dry_run=True), _found(dry_run=False)])
        assert _run(["--apply", "--yes"], backfill) == 0
        assert [call.kwargs["dry_run"] for call in backfill.await_args_list] == [True, False]
        out = capsys.readouterr().out
        assert "soft-deleted 3 file(s) in 1 deleted context(s), 220 uploaded bytes" in out
        assert "files soft-deleted: 3" in out
        assert f"released   {WS}  260 bytes" in out

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
