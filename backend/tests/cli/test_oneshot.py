"""``cli._oneshot.run_plan_apply`` argument checks (#2268)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli import _oneshot


@pytest.mark.asyncio
async def test_yes_without_apply_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """``--yes`` answers the apply prompt: without ``--apply`` the operator
    expected a write the plan never does. Exit 2 before the secret guard or a
    session runs."""
    run = AsyncMock()
    guard = MagicMock()
    get_db = MagicMock()
    with (
        patch.object(_oneshot, "require_settings", guard),
        patch.object(_oneshot, "get_db", get_db),
    ):
        code = await _oneshot.run_plan_apply(
            run=run,
            print_plan=MagicMock(),
            changes=lambda result: 1,
            noun="row",
            apply=False,
            assume_yes=True,
        )

    assert code == 2
    assert "--yes needs --apply" in capsys.readouterr().err
    run.assert_not_awaited()
    guard.assert_not_called()
    get_db.assert_not_called()


_WS = "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        ("apply_rerank_defaults", ["--all"]),
        ("backfill_deleted_context_files", []),
        ("restore_context", [_WS]),
        ("transfer_context_creator", ["--from", "a", "--to", "b", "--workspace", _WS]),
        ("sweep_orphan_vectors", []),
        ("reprocess_erasures", ["--all"]),
    ],
)
@pytest.mark.asyncio
async def test_every_plan_apply_command_refuses_yes_without_apply(
    module: str, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The six commands on the scaffold: ``--yes`` alone exits 2 and opens no
    session."""
    import importlib

    cli = importlib.import_module(f"cli.{module}")
    get_db = MagicMock()
    with (
        patch.object(_oneshot, "get_db", get_db),
        patch.object(cli, "configure_logging", lambda level: None),
    ):
        code = await cli._main(cli._parse([*argv, "--yes"]))

    assert code == 2
    assert "--yes needs --apply" in capsys.readouterr().err
    get_db.assert_not_called()
