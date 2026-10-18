"""Tests for ``cli/migrate_context_embedding``: the UNREBUILDABLE report (#1896)
and the content chunk handling before a switch (#2015).

The migration is covered in ``tests/services/test_embedding_migration_service.py``
and ``tests/services/test_embedding_migration_resource_rows.py``; this pins
what the command prints about resource-ingested memories it could not rebuild.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

# The CLI bootstraps sys.path itself; import it the same way create_admin's test does.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import migrate_context_embedding as cli  # noqa: E402
from services.embedding_migration_service import (  # noqa: E402
    ChunkRebuildResult,
    MigrationPlan,
    ReembedResult,
    SwitchResult,
    VerifyResult,
)

_MODEL = "qwen3-embedding:4b"


def _plan(context_id: UUID, **extra) -> MigrationPlan:
    return MigrationPlan(
        context_id=context_id,
        workspace_id=uuid4(),
        source_model="text-embedding-3-small",
        source_dimensions=512,
        source_collection="kagura_memories",
        target_model=_MODEL,
        target_dimensions=2560,
        target_collection="kagura_memories_qwen3_embedding_4b_2560",
        memory_count=3,
        **extra,
    )


async def _run(
    argv: list[str],
    *,
    reembed: ReembedResult,
    verify: VerifyResult | list[VerifyResult] | None = None,
    rebuild: AsyncMock | None = None,
    switch: AsyncMock | None = None,
    **plan_extra,
) -> int:
    """``verify`` as a list: one result per verify call, in order."""
    context_id = uuid4()
    args = cli._parse(["--to", _MODEL, "--context", str(context_id), *argv])
    plan = _plan(context_id, **plan_extra)
    verify_mock = (
        AsyncMock(side_effect=verify)
        if isinstance(verify, list)
        else AsyncMock(return_value=verify)
    )
    with (
        patch.object(cli, "plan_context_migration", AsyncMock(return_value=plan)),
        patch.object(cli, "reembed_context", AsyncMock(return_value=reembed)),
        patch.object(cli, "verify_context_migration", verify_mock),
        patch.object(
            cli, "rebuild_target_chunks", rebuild or AsyncMock(return_value=ChunkRebuildResult())
        ),
        patch.object(cli, "switch_context_embedding", switch or AsyncMock(return_value=_SWITCHED)),
    ):
        return await cli._one_context(MagicMock(), context_id, args)


_SWITCHED = SwitchResult(
    previous_model="text-embedding-3-small",
    previous_dimensions=512,
    model=_MODEL,
    dimensions=2560,
    requeued=0,
)


_STARTED = datetime(2026, 10, 1)


def _reembed(unrebuildable: list[UUID]) -> ReembedResult:
    return ReembedResult(embedded=2, batches=1, started_at=_STARTED, unrebuildable=unrebuildable)


@pytest.mark.asyncio
async def test_reembed_alone_lists_the_rows_it_could_not_rebuild(capsys):
    skipped = uuid4()
    assert await _run(["--reembed"], reembed=_reembed([skipped])) == 0
    out = capsys.readouterr().out
    assert out.count("UNREBUILDABLE 1 ") == 1
    assert str(skipped) in out


@pytest.mark.asyncio
async def test_reembed_and_verify_report_every_skipped_row_once(capsys):
    # ``kept`` was skipped by the re-embed, but another live row keeps its
    # point in the target, so verify finds it present and does not list it.
    both, kept = uuid4(), uuid4()
    verified = VerifyResult(expected=3, present=2, unrebuildable=[both])
    code = await _run(["--reembed", "--verify"], reembed=_reembed([both, kept]), verify=verified)
    out = capsys.readouterr().out
    # Unrebuildable rows are not missing rows: the step does not fail.
    assert code == 0
    assert out.count("UNREBUILDABLE") == 1
    assert "UNREBUILDABLE 2 " in out
    assert out.count(str(both)) == 1 and out.count(str(kept)) == 1


@pytest.mark.asyncio
async def test_nothing_is_printed_when_every_row_was_rebuilt(capsys):
    verified = VerifyResult(expected=3, present=3)
    assert await _run(["--reembed", "--verify"], reembed=_reembed([]), verify=verified) == 0
    assert "UNREBUILDABLE" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_content_chunks_are_estimated_reported_and_never_fail_verify(capsys):
    """#2015 PR 2a: the plan prints the chunk estimate; verify reports
    incomplete chunks on a CHUNKS_MISSING line without failing the step."""
    incomplete = uuid4()
    verified = VerifyResult(
        expected=3,
        present=3,
        chunks_expected=10,
        chunks_present=8,
        chunks_missing=[incomplete],
        chunks_stale_removed=1,
    )
    reembed = ReembedResult(
        embedded=3, batches=1, started_at=datetime(2026, 10, 1), chunks_written=8
    )
    code = await _run(
        ["--reembed", "--verify"],
        reembed=reembed,
        verify=verified,
        content_chunks_on=True,
        chunk_estimate=10,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "~10 chunk point(s)" in out
    assert "8 content chunk points" in out
    assert "content chunks: 8/10 present" in out
    assert "CHUNKS_MISSING 1 " in out and str(incomplete) in out
    assert "removed the content chunks of 1 memory(ies)" in out


@pytest.mark.asyncio
async def test_chunks_written_are_reported_when_the_flag_was_turned_on_after_the_plan(capsys):
    """F21: the count follows what the re-embed wrote, not the plan's flag."""
    reembed = ReembedResult(
        embedded=3, batches=1, started_at=datetime(2026, 10, 1), chunks_written=4
    )
    verified = VerifyResult(expected=3, present=3)
    assert await _run(["--reembed", "--verify"], reembed=reembed, verify=verified) == 0
    out = capsys.readouterr().out
    assert "4 content chunk points" in out
    assert "~" not in out  # no estimate line: the plan had the flag off


@pytest.mark.asyncio
async def test_no_chunk_lines_without_content_chunks(capsys):
    verified = VerifyResult(expected=3, present=3)
    assert await _run(["--reembed", "--verify"], reembed=_reembed([]), verify=verified) == 0
    out = capsys.readouterr().out
    assert "chunk" not in out.lower()


def _chunks_missing(*memory_ids: UUID) -> VerifyResult:
    return VerifyResult(
        expected=3, present=3, chunks_expected=6, chunks_present=0, chunks_missing=list(memory_ids)
    )


def _chunks_complete() -> VerifyResult:
    return VerifyResult(expected=3, present=3, chunks_expected=6, chunks_present=6)


@pytest.mark.asyncio
async def test_verify_alone_names_a_remedy_that_works_before_the_switch(capsys):
    """F8: a re-embed to the target is refused once the context routes to it,
    so the remedy is worded for the state verify runs in (not switched)."""
    incomplete = uuid4()
    rebuild = AsyncMock()
    code = await _run(
        ["--verify"], reembed=_reembed([]), verify=_chunks_missing(incomplete), rebuild=rebuild
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "CHUNKS_MISSING 1 " in out and str(incomplete) in out
    assert "Not switched yet: re-run --reembed" in out
    assert "--run, which rebuilds them before it switches" in out
    rebuild.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_rebuilds_chunks_the_flag_flip_left_out_before_switching(capsys):
    """F8: content_chunks_enabled turned on while ``--run --yes`` re-embedded
    (the plan had it off, so no chunks were written): the run rebuilds the
    listed memories' chunks in the target, checks them, then switches."""
    a, b = uuid4(), uuid4()
    order: list[str] = []
    rebuilt = ChunkRebuildResult(rebuilt=2, chunks_written=6)
    rebuild = AsyncMock(side_effect=lambda *_a, **_k: order.append("rebuild") or rebuilt)
    switch = AsyncMock(side_effect=lambda *_a, **_k: order.append("switch") or _SWITCHED)
    code = await _run(
        ["--run", "--yes"],
        reembed=_reembed([]),
        verify=_chunks_missing(a, b),
        rebuild=rebuild,
        switch=switch,
        content_chunks_on=False,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert order == ["rebuild", "switch"]
    assert rebuild.await_args.args[2] == [a, b]
    # The switch re-queues what changed since the re-embed started: the
    # rebuild's check leaves those to the sweep.
    assert rebuild.await_args.kwargs["requeue_since"] == _STARTED
    assert switch.await_args.kwargs["requeue_since"] == _STARTED
    assert "content_chunks_enabled was turned on after the plan" in out
    assert "Rebuilding them before the switch" in out
    assert "rebuilt the content chunks of 2 memory(ies): 6 chunk points" in out
    assert "Not switched yet" not in out
    assert "switched: text-embedding-3-small -> " in out


@pytest.mark.asyncio
async def test_run_stops_before_the_switch_when_chunks_stay_missing(capsys):
    stuck = uuid4()
    switch = AsyncMock(return_value=_SWITCHED)
    code = await _run(
        ["--run", "--yes"],
        reembed=_reembed([]),
        verify=_chunks_missing(stuck),
        rebuild=AsyncMock(return_value=ChunkRebuildResult(rebuilt=1, still_missing=[stuck])),
        switch=switch,
        content_chunks_on=True,
    )
    out = capsys.readouterr().out
    assert code == 2
    switch.assert_not_awaited()
    assert "turned on after the plan" not in out
    assert "the context was NOT switched" in out
    # A retry that does not re-embed the whole context, keeping the re-queue.
    assert f"--verify --switch --requeue-since {_STARTED.isoformat()}" in out
    assert out.count(str(stuck)) == 2  # the first verify's line and the one after the rebuild


@pytest.mark.asyncio
async def test_a_failed_chunk_rebuild_skips_the_switch_and_the_run_goes_on(capsys):
    switch = AsyncMock()
    code = await _run(
        ["--run", "--yes"],
        reembed=_reembed([]),
        verify=_chunks_missing(uuid4()),
        rebuild=AsyncMock(side_effect=RuntimeError("upsert failed")),
        switch=switch,
    )
    assert code == 1
    switch.assert_not_awaited()
    assert "chunk rebuild failed, NOT switched (RuntimeError: upsert failed)" in (
        capsys.readouterr().out
    )


@pytest.mark.asyncio
async def test_declining_asks_before_the_rebuild_and_spends_nothing(capsys):
    rebuild = AsyncMock()
    switch = AsyncMock()
    with patch("builtins.input", return_value="n") as asked:
        code = await _run(
            ["--verify", "--switch"],
            reembed=_reembed([]),
            verify=_chunks_missing(uuid4()),
            rebuild=rebuild,
            switch=switch,
        )
    assert code == 0
    asked.assert_called_once()
    assert "Rebuild the content chunks of 1 memory(ies), then switch" in asked.call_args.args[0]
    rebuild.assert_not_awaited()
    switch.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_confirmation_covers_the_rebuild_and_the_switch(capsys):
    switch = AsyncMock(return_value=_SWITCHED)
    with patch("builtins.input", return_value="y") as asked:
        code = await _run(
            ["--verify", "--switch"],
            reembed=_reembed([]),
            verify=_chunks_missing(uuid4()),
            rebuild=AsyncMock(return_value=ChunkRebuildResult(rebuilt=1, chunks_written=3)),
            switch=switch,
        )
    assert code == 0
    asked.assert_called_once()
    switch.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_does_not_rebuild_when_verify_already_failed(capsys):
    """Missing summary points stop the run first (exit 2), as before."""
    rebuild = AsyncMock()
    switch = AsyncMock()
    verified = VerifyResult(expected=3, present=2, missing=[uuid4()], chunks_missing=[uuid4()])
    code = await _run(
        ["--run", "--yes"], reembed=_reembed([]), verify=verified, rebuild=rebuild, switch=switch
    )
    assert code == 2
    rebuild.assert_not_awaited()
    switch.assert_not_awaited()
    assert "Not switched yet" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_with_complete_chunks_switches_without_a_rebuild(capsys):
    rebuild = AsyncMock()
    switch = AsyncMock(return_value=_SWITCHED)
    code = await _run(
        ["--run", "--yes"],
        reembed=_reembed([]),
        verify=_chunks_complete(),
        rebuild=rebuild,
        switch=switch,
        content_chunks_on=True,
    )
    assert code == 0
    rebuild.assert_not_awaited()
    switch.assert_awaited_once()
