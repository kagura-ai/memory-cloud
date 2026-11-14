"""``python -m src.cli.public_usage_budget`` (#2805): show, reset plan, reset apply."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from cli import _oneshot
from cli import public_usage_budget as cli
from services.public_usage_budget import BudgetReset, budget_status

WS = "00000000-0000-0000-0000-0000000000a1"
START = datetime(2026, 11, 10, 8, 0, 0)


@pytest.fixture
def session(monkeypatch):
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=UUID(WS)))

    async def get_db():
        yield db

    monkeypatch.setattr(_oneshot, "get_db", get_db)
    monkeypatch.setattr(_oneshot, "require_settings", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    return db


def _reset(applied: bool) -> BudgetReset:
    return BudgetReset(
        workspace_id=UUID(WS),
        calls_used_before=9,
        period_started_at=START,
        period_seq=4,
        applied=applied,
    )


async def test_show_prints_the_owner_status_as_json(session, capsys) -> None:
    status = budget_status(
        SimpleNamespace(calls_used=3, period_started_at=START, period_seq=2),
        limit=10,
        now=START,
    )
    read = AsyncMock(return_value=status)
    with patch.object(cli, "read_public_usage_budget", read):
        code = await cli._main(cli._parse(["show", WS]))
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["state"] == "active"
    assert (body["used"], body["remaining"], body["period_seq"]) == (3, 7, 2)
    assert body["resets_at"] == "2026-11-17T08:00:00Z"
    read.assert_awaited_once_with(session, UUID(WS))


async def test_show_unknown_workspace_exits_1(session, capsys) -> None:
    session.get.return_value = None
    with patch.object(cli, "read_public_usage_budget", AsyncMock()) as read:
        code = await cli._main(cli._parse(["show", WS]))
    assert code == 1
    assert "no workspace" in capsys.readouterr().err
    read.assert_not_awaited()


async def test_reset_plans_by_default(session, capsys) -> None:
    reset = AsyncMock(return_value=_reset(False))
    with patch.object(cli, "reset_public_usage_budget", reset):
        code = await cli._main(cli._parse(["reset", WS]))
    assert code == 0
    out = capsys.readouterr().out
    assert "9" in out and "dry run" in out
    reset.assert_awaited_once_with(session, UUID(WS), dry_run=True)


async def test_reset_apply_yes_writes(session, capsys) -> None:
    calls: list[bool] = []

    async def reset(db, workspace_id, *, dry_run):
        calls.append(dry_run)
        return _reset(not dry_run)

    with patch.object(cli, "reset_public_usage_budget", reset):
        code = await cli._main(cli._parse(["reset", WS, "--apply", "--yes"]))
    assert code == 0
    assert calls == [True, False]
    assert "refilled 1 budget(s)" in capsys.readouterr().out


async def test_reset_unknown_workspace_exits_1(session, capsys) -> None:
    reset = AsyncMock(side_effect=LookupError("no public usage budget is stored"))
    with patch.object(cli, "reset_public_usage_budget", reset):
        code = await cli._main(cli._parse(["reset", WS, "--apply", "--yes"]))
    assert code == 1
    assert "no public usage budget" in capsys.readouterr().err
    assert reset.await_count == 1  # the plan failed; nothing was applied


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        cli._parse([])
    with pytest.raises(SystemExit):
        cli._parse(["show", "not-a-uuid"])
