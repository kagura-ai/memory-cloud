"""Tests for ``cli/restore_context`` (#1804).

The restore itself is covered in ``tests/services/test_context_restore.py``;
this pins the command around it: read-only by default, ``--apply`` restores,
``--name`` reaches the service, and a refusal is an error exit.
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

from cli import restore_context as cli  # noqa: E402
from services.context_restore import ContextRestoreResult  # noqa: E402
from utils.exceptions import ConflictError  # noqa: E402

CONTEXT_ID = uuid4()


def _result(*, dry_run: bool, **overrides) -> ContextRestoreResult:
    fields = {
        "context_id": str(CONTEXT_ID),
        "workspace_id": str(uuid4()),
        "name": "notes",
        "deleted_at": datetime(2026, 9, 30, 12, 0, 0),
        "deleted_by": "owner",
        "dry_run": dry_run,
        "memories_restored": 5,
        "memories_left_deleted": 2,
    }
    fields.update(overrides)
    return ContextRestoreResult(**fields)


async def _no_db():
    yield object()


def _run(argv: list[str], restore: AsyncMock) -> int:
    with (
        patch.object(cli, "restore_deleted_context", restore),
        patch("cli._oneshot.get_db", _no_db),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


class TestRestoreContextCommand:
    def test_default_is_a_read_only_plan(self, capsys):
        restore = AsyncMock(return_value=_result(dry_run=True))

        assert _run([str(CONTEXT_ID)], restore) == 0

        assert [call.kwargs["dry_run"] for call in restore.await_args_list] == [True]
        out = capsys.readouterr().out
        assert "5 to restore and re-embed" in out
        assert "2 stay deleted" in out
        assert "dry run — pass --apply to write" in out

    def test_apply_restores(self, capsys):
        restore = AsyncMock(side_effect=[_result(dry_run=True), _result(dry_run=False)])

        assert _run([str(CONTEXT_ID), "--apply", "--yes"], restore) == 0

        assert [call.kwargs["dry_run"] for call in restore.await_args_list] == [True, False]
        assert restore.await_args_list[1].args[1] == CONTEXT_ID
        out = capsys.readouterr().out
        assert "restored 1 context(s)" in out
        assert "memories restored: 5" in out

    def test_a_context_with_no_memories_left_is_still_restored(self, capsys):
        empty = {"memories_restored": 0, "memories_left_deleted": 0}
        restore = AsyncMock(
            side_effect=[_result(dry_run=True, **empty), _result(dry_run=False, **empty)]
        )

        assert _run([str(CONTEXT_ID), "--apply", "--yes"], restore) == 0

        assert restore.await_count == 2

    def test_name_reaches_the_service_and_the_plan_shows_it(self, capsys):
        restore = AsyncMock(
            return_value=_result(dry_run=True, name="notes-2", renamed_from="notes")
        )

        assert _run([str(CONTEXT_ID), "--name", "notes-2"], restore) == 0

        assert restore.await_args.kwargs["new_name"] == "notes-2"
        assert "notes-2 (was notes)" in capsys.readouterr().out

    def test_refusal_is_an_error_exit(self, capsys):
        restore = AsyncMock(side_effect=ConflictError("Context is not deleted"))

        assert _run([str(CONTEXT_ID)], restore) == 1

        assert "not deleted" in capsys.readouterr().err
