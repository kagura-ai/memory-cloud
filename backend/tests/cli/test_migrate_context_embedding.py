"""Tests for ``cli/migrate_context_embedding``: the UNREBUILDABLE report (#1896)
and the content chunk handling before a switch (#2015).

The migration is covered in ``tests/services/test_embedding_migration_service.py``
and ``tests/services/test_embedding_migration_resource_rows.py``; this pins
what the command prints about resource-ingested memories it could not rebuild.
"""

from __future__ import annotations

import contextlib
import io
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
    EmbeddingSwitchBusyError,
    MigrationPlan,
    ReembedResult,
    RollbackBoundary,
    SwitchResult,
    VerifyResult,
)
from utils.exceptions import ValidationError  # noqa: E402

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
    db: MagicMock | None = None,
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
        return await cli._one_context(db or MagicMock(), context_id, args)


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
    assert "content chunks became active after the plan" in out
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
    """The shared session is rolled back for the next context of --all, and a
    store or provider error's text (it can quote chunk text) is not printed."""
    switch = AsyncMock()
    db = MagicMock()
    db.rollback = AsyncMock()
    code = await _run(
        ["--run", "--yes"],
        reembed=_reembed([]),
        verify=_chunks_missing(uuid4()),
        rebuild=AsyncMock(side_effect=RuntimeError("upsert failed: <chunk text>")),
        switch=switch,
        db=db,
    )
    assert code == 1
    switch.assert_not_awaited()
    db.rollback.assert_awaited_once()
    out = capsys.readouterr().out
    assert "chunk rebuild failed, NOT switched (RuntimeError); retry" in out
    assert "chunk text" not in out


@pytest.mark.asyncio
async def test_a_refused_chunk_rebuild_prints_why(capsys):
    db = MagicMock()
    db.rollback = AsyncMock()
    code = await _run(
        ["--run", "--yes"],
        reembed=_reembed([]),
        verify=_chunks_missing(uuid4()),
        rebuild=AsyncMock(side_effect=ValidationError("already routes to the target")),
        db=db,
    )
    assert code == 1
    assert "(ValidationError: already routes to the target)" in capsys.readouterr().out


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
async def test_switch_reports_when_stale_pruning_was_deferred(capsys):
    switched = SwitchResult(
        previous_model="text-embedding-3-small",
        previous_dimensions=512,
        model=_MODEL,
        dimensions=2560,
        requeued=1,
        prune_pending=True,
    )
    code = await _run(
        ["--switch", "--yes"], reembed=_reembed([]), switch=AsyncMock(return_value=switched)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "the route is already switched" in out
    assert "sweep_orphan_vectors, then apply with" in out
    assert "sweep_orphan_vectors --apply --yes --grace-hours 0" in out


# ------------------------------------------- a refused switch prints its retry (#2156)

_WRITERS_BUSY = "Point writers are still active; the context route was not changed."
_ROWS_BUSY = (
    "Another transaction holds memories this switch must re-queue; "
    "the context route was not changed."
)


def _busy(message: str = _WRITERS_BUSY) -> AsyncMock:
    return AsyncMock(side_effect=EmbeddingSwitchBusyError(message))


@pytest.mark.asyncio
async def test_busy_run_prints_a_retry_that_carries_requeue_since(capsys):
    """After --run the retry must repeat the re-embed start: a bare --switch
    would re-queue nothing, and --run would re-embed the whole context."""
    code = await _run(
        ["--run", "--yes"], reembed=_reembed([]), verify=_chunks_complete(), switch=_busy()
    )
    assert code == 1
    assert (
        "  NOT switched: Point writers are still active; the context route was not changed. "
        "Retry with --verify --switch --requeue-since 2026-10-01T00:00:00 once the current "
        "write finishes (no full re-embed).\n"
    ) in capsys.readouterr().out


@pytest.mark.asyncio
async def test_busy_bare_switch_prints_a_plain_retry(capsys):
    code = await _run(["--switch", "--yes"], reembed=_reembed([]), switch=_busy(_ROWS_BUSY))
    out = capsys.readouterr().out
    assert code == 1
    assert (
        f"  NOT switched: {_ROWS_BUSY} Retry --switch once the current write finishes.\n"
    ) in out
    assert "--requeue-since 2" not in out
    assert "switched: text-embedding-3-small" not in out


@pytest.mark.asyncio
async def test_busy_switch_with_explicit_requeue_since_repeats_it(capsys):
    switch = _busy()
    code = await _run(
        ["--switch", "--yes", "--requeue-since", "2026-10-02T03:04:05"],
        reembed=_reembed([]),
        switch=switch,
    )
    assert code == 1
    assert switch.await_args.kwargs["requeue_since"] == datetime(2026, 10, 2, 3, 4, 5)
    assert (
        f"  NOT switched: {_WRITERS_BUSY} Retry with --verify --switch --requeue-since "
        "2026-10-02T03:04:05 once the current write finishes (no full re-embed).\n"
    ) in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_busy_context_does_not_stop_an_all_run(capsys):
    """--all: the refused context exits 1, the next one is still switched."""
    first, second = uuid4(), uuid4()
    args = cli._parse(["--to", _MODEL, "--all", "--switch", "--yes"])

    async def db_sessions():
        yield MagicMock()

    plans = {first: _plan(first), second: _plan(second)}
    switch = AsyncMock(side_effect=[EmbeddingSwitchBusyError(_ROWS_BUSY), _SWITCHED])
    with (
        patch.object(cli, "get_db", return_value=db_sessions()),
        patch.object(cli, "get_settings", return_value=MagicMock()),
        patch.object(cli, "list_migratable_context_ids", AsyncMock(return_value=[first, second])),
        patch.object(
            cli,
            "plan_context_migration",
            AsyncMock(side_effect=lambda db, context_id, *a, **k: plans[context_id]),
        ),
        patch.object(cli, "switch_context_embedding", switch),
    ):
        code = await cli._main(args)

    out = capsys.readouterr().out
    assert code == 1
    assert switch.await_count == 2
    assert "NOT switched: " in out
    assert "switched: text-embedding-3-small -> qwen3-embedding:4b" in out
    assert "skip " not in out


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


def test_requeue_since_with_an_offset_is_parsed_as_naive_utc():
    """The memories timestamps are naive UTC; asyncpg refuses an aware bound (#1525)."""
    args = cli._parse(
        ["--to", _MODEL, "--context", str(uuid4()), "--requeue-since", "2026-10-18T09:00:00+09:00"]
    )
    assert args.requeue_since == datetime(2026, 10, 18, 0, 0, 0)
    assert args.requeue_since.tzinfo is None


def test_requeue_since_without_an_offset_is_taken_as_utc():
    args = cli._parse(
        ["--to", _MODEL, "--context", str(uuid4()), "--requeue-since", "2026-10-18T09:00:00"]
    )
    assert args.requeue_since == datetime(2026, 10, 18, 9, 0, 0)


# ---------------------------------------------------------------- --rollback-to (#2098)

_ROLLBACK_TO = "text-embedding-3-small"
_SWITCH_TIME = datetime(2026, 10, 1, 12, 30, 0)


async def _rollback(argv: list[str], boundary, capsys, *, answer: str = "y"):
    """Run ``--rollback-to`` without ``--yes``; returns (code, stdout at the
    prompt, rollback mock, resolve mock)."""
    context_id = uuid4()
    args = cli._parse(["--rollback-to", _ROLLBACK_TO, "--context", str(context_id), *argv])
    rolled = SwitchResult(_MODEL, 2560, _ROLLBACK_TO, 512, requeued=2)
    at_prompt: list[str] = []

    def _input(prompt: str) -> str:
        at_prompt.append(capsys.readouterr().out)
        return answer

    rolled.requeue_since = boundary.since
    resolve = AsyncMock(return_value=boundary)
    rollback = AsyncMock(return_value=rolled)
    db = MagicMock()

    async def _end_read_transaction() -> None:
        # The read transaction ends before the prompt can wait (#2098).
        assert not at_prompt

    db.rollback = AsyncMock(side_effect=_end_read_transaction)
    with (
        patch.object(cli, "check_rollback_target", AsyncMock(return_value=512)) as check,
        patch.object(cli, "resolve_rollback_boundary", resolve),
        patch.object(cli, "rollback_context_embedding", rollback),
        patch("builtins.input", _input),
    ):
        code = await cli._one_context(db, context_id, args)
    check.assert_awaited_once_with(db, context_id, _ROLLBACK_TO)
    db.rollback.assert_awaited_once()
    return code, at_prompt, rollback, resolve


@pytest.mark.asyncio
async def test_rollback_prints_the_recorded_switch_time_before_asking(capsys):
    code, at_prompt, rollback, resolve = await _rollback(
        [], RollbackBoundary(_SWITCH_TIME, "switch_time"), capsys
    )
    assert code == 0
    assert at_prompt == [
        f"  re-queue since {_SWITCH_TIME.isoformat()} (the recorded switch time)\n"
    ]
    assert resolve.await_args.args[2] is None
    assert rollback.await_args.kwargs["requeue_since"] == _SWITCH_TIME
    assert f"requeued 2 written since {_SWITCH_TIME.isoformat()}" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_rollback_warns_when_only_updated_at_is_recorded(capsys):
    code, at_prompt, rollback, _ = await _rollback(
        [], RollbackBoundary(_SWITCH_TIME, "config_updated_at"), capsys, answer="n"
    )
    assert code == 0
    assert len(at_prompt) == 1
    assert "WARNING: no recorded switch time" in at_prompt[0]
    assert _SWITCH_TIME.isoformat() in at_prompt[0]
    assert "--requeue-since <switch time>" in at_prompt[0]
    rollback.assert_not_awaited()  # declined after reading the warning
    assert "skipped" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_rollback_passes_an_explicit_requeue_since_through(capsys):
    explicit = datetime(2026, 9, 30, 0, 0, 0)
    code, at_prompt, rollback, resolve = await _rollback(
        ["--requeue-since", explicit.isoformat()],
        RollbackBoundary(explicit, "explicit"),
        capsys,
    )
    assert code == 0
    assert at_prompt == [f"  re-queue since {explicit.isoformat()} (--requeue-since)\n"]
    assert resolve.await_args.args[2] == explicit
    assert rollback.await_args.kwargs["requeue_since"] == explicit


def test_requeue_since_help_names_the_recorded_switch_time():
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        cli._parse(["--help"])
    assert "embedding_switched_at" in out.getvalue()


@pytest.mark.asyncio
async def test_a_refused_rollback_prints_no_boundary_and_does_not_ask(capsys):
    """#2098: 'already routes' / 'no collection' come before the boundary."""
    context_id = uuid4()
    args = cli._parse(["--rollback-to", _ROLLBACK_TO, "--context", str(context_id)])
    refused = ValidationError(f"Context {context_id} already routes to {_ROLLBACK_TO!r}")
    with (
        patch.object(cli, "check_rollback_target", AsyncMock(side_effect=refused)),
        patch.object(cli, "resolve_rollback_boundary", AsyncMock()) as resolve,
        patch.object(cli, "rollback_context_embedding", AsyncMock()) as rollback,
        patch("builtins.input", MagicMock(side_effect=AssertionError("asked"))),
        pytest.raises(ValidationError, match="already routes"),
    ):
        await cli._one_context(MagicMock(), context_id, args)
    resolve.assert_not_awaited()
    rollback.assert_not_awaited()
    assert capsys.readouterr().out == ""


async def _busy_rollback(argv: list[str], boundary, capsys) -> tuple[int, str]:
    context_id = uuid4()
    args = cli._parse(["--rollback-to", _ROLLBACK_TO, "--context", str(context_id), "--yes", *argv])
    db = MagicMock()
    db.rollback = AsyncMock()
    with (
        patch.object(cli, "check_rollback_target", AsyncMock(return_value=512)),
        patch.object(cli, "resolve_rollback_boundary", AsyncMock(return_value=boundary)),
        patch.object(cli, "rollback_context_embedding", _busy()),
    ):
        code = await cli._one_context(db, context_id, args)
    return code, capsys.readouterr().out


@pytest.mark.asyncio
async def test_busy_rollback_names_rollback_to(capsys):
    """#2156: a bare retry re-queues from the recorded switch time again (#2098)."""
    code, out = await _busy_rollback([], RollbackBoundary(_SWITCH_TIME, "switch_time"), capsys)
    assert code == 1
    assert (
        f"  NOT rolled back: {_WRITERS_BUSY} Retry --rollback-to {_ROLLBACK_TO} "
        "once the current write finishes.\n"
    ) in out
    assert "rolled back " not in out.replace("NOT rolled back", "")


@pytest.mark.asyncio
async def test_busy_rollback_repeats_an_explicit_requeue_since(capsys):
    explicit = datetime(2026, 9, 30, 0, 0, 0)
    code, out = await _busy_rollback(
        ["--requeue-since", explicit.isoformat()], RollbackBoundary(explicit, "explicit"), capsys
    )
    assert code == 1
    assert (
        f"  NOT rolled back: {_WRITERS_BUSY} Retry --rollback-to {_ROLLBACK_TO} "
        "--requeue-since 2026-09-30T00:00:00 once the current write finishes.\n"
    ) in out
