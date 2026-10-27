"""Operator cleanup output never exposes object names, including errors."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli import sweep_orphan_objects as command


@pytest.mark.asyncio
async def test_plan_is_default_and_output_contains_only_counts(capsys):
    result = {"scanned": 4, "candidates": 1, "deleted": 0, "failed": 0, "kept": 3}

    async def driver(**kwargs):
        db = MagicMock()
        plan = await kwargs["run"](db, True)
        kwargs["print_plan"](plan)
        assert kwargs["apply"] is False
        return 0

    with (
        patch.object(command, "get_blob_storage", MagicMock()),
        patch.object(command, "sweep_unregistered_objects", AsyncMock(return_value=result)),
        patch.object(command, "run_plan_apply", driver),
    ):
        assert await command._main(command._parse([])) == 0
    assert capsys.readouterr().out.strip() == "scanned=4 candidates=1 deleted=0 failed=0 kept=3"


@pytest.mark.asyncio
async def test_failure_sanitized_before_shared_driver(capsys):
    async def driver(**kwargs):
        try:
            await kwargs["run"](MagicMock(), False)
        except RuntimeError as exc:
            assert str(exc) == "ValueError"
            assert exc.__suppress_context__
            return 1
        raise AssertionError("expected failure")

    with (
        patch.object(command, "get_blob_storage", MagicMock()),
        patch.object(
            command,
            "sweep_unregistered_objects",
            AsyncMock(side_effect=ValueError("private-sub/private-name")),
        ),
        patch.object(command, "run_plan_apply", driver),
    ):
        assert await command._main(command._parse(["--apply", "--yes"])) == 1
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
