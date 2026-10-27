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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
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


def _change(**kwargs: Any) -> cli.RequestChange:
    fields: dict[str, Any] = {
        "request_id": REQUEST_ID,
        "completed_at": None,
        "details_restored": 0,
        "details_restored_unavailable": 0,
        "details_stripped": 0,
        "scrub_marked": 0,
    }
    return cli.RequestChange(**{**fields, **kwargs})


def test_a_pseudonymized_note_row_is_listed_and_counted(capsys):
    """#2302: a legacy note row's repair is a change of its own; the plan and
    the apply name it, never the account id."""
    for dry_run, line in ((True, "would pseudonymize note row"), (False, "pseudonymized note row")):
        result = cli.ReprocessResult(dry_run=dry_run, scanned=1)
        result.changed.append(_change(note_repaired=True))
        assert result.changes == 1
        (cli._print_plan if dry_run else cli._print_applied)(result)
        out = capsys.readouterr().out
        assert f"{line}  {REQUEST_ID}  marked complete without running before v0.125.0" in out
        assert cli._summary(result).endswith(", 1 note row(s) pseudonymized")


@pytest.mark.parametrize("memories", [0, 2])
def test_a_payload_resync_is_listed_and_reported(capsys, memories):
    """#2302: the backstop of the sweep's re-drive is a change even with
    nothing to patch (its counts are written); the apply prints the payload
    line."""
    plan = cli.ReprocessResult(dry_run=True, scanned=1)
    plan.changed.append(_change(payload_resync=memories))
    assert plan.changes == 1
    cli._print_plan(plan)
    assert f"would resync payloads  {REQUEST_ID}  {memories} memory(ies)" in capsys.readouterr().out
    applied = cli.ReprocessResult(dry_run=False, scanned=1)
    applied.changed.append(
        _change(payload_resync=memories, payload={"details_payload_synced": memories})
    )
    cli._print_applied(applied)
    assert f"payload  {REQUEST_ID}  synced {memories}," in capsys.readouterr().out
    assert cli._summary(applied).endswith(", 1 request(s) payloads resynced")


def test_no_note_or_resync_adds_nothing_to_the_report(capsys):
    result = cli.ReprocessResult(dry_run=True, scanned=1)
    result.changed.append(_change(details_stripped=1))
    cli._print_plan(result)
    out = capsys.readouterr().out
    assert "note row" not in out and "resync" not in out
    assert "note row" not in cli._summary(result) and "resynced" not in cli._summary(result)


def test_merged_syncs_prefer_this_runs_restore():

    from services.erasure_details import PayloadSync

    a, b, c = sorted([uuid4(), uuid4(), uuid4()])
    merged = cli._merge_syncs(
        [PayloadSync(b, 7), PayloadSync(a, 2)], [PayloadSync(b, 5), PayloadSync(c, 3)]
    )
    assert merged == [PayloadSync(a, 2), PayloadSync(b, 7), PayloadSync(c, 3)]


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


@pytest.mark.parametrize("recovered", [True, False])
def test_pseudonymized_audit_rows_are_reported_and_counted(capsys, recovered):
    """#2287: the subject's audit rows are a change of their own, in the plan,
    the applied report and the summary; an email that could not be
    recovered is named. Never the account id or the email."""
    kept = uuid4()

    def result(dry_run: bool) -> cli.ReprocessResult:
        r = cli.ReprocessResult(dry_run=dry_run, scanned=2)
        r.changed.append(
            cli.RequestChange(
                request_id=REQUEST_ID,
                completed_at=None,
                details_restored=0,
                details_restored_unavailable=0,
                details_stripped=0,
                scrub_marked=0,
                audit_logs_pseudonymized=2,
                audit_email_recovered=recovered,
            )
        )
        r.audit_kept.append(kept)
        return r

    plan = result(True)
    assert plan.changes == 2
    cli._print_plan(plan)
    cli._print_applied(result(False))
    out = capsys.readouterr().out
    suffix = "" if recovered else ", email not recovered"
    assert f"would pseudonymize audit rows  {REQUEST_ID}  2 row(s){suffix}\n" in out
    assert f"pseudonymized audit rows  {REQUEST_ID}  2 row(s){suffix}\n" in out
    assert f"audit  {kept}  rows kept: the account id is in use again" in out
    assert cli._summary(plan).endswith(", 2 audit row(s) pseudonymized")


def test_no_audit_rows_add_nothing_to_the_report(capsys):
    result = _result(dry_run=True)
    cli._print_plan(result)
    out = capsys.readouterr().out
    assert "audit" not in out
    assert "audit" not in cli._summary(result)


def test_the_audit_subs_select_is_correlated_to_each_audit_row():
    """#2287 review: ``in_use`` is an EXISTS over ``users`` alone, correlated
    to the audit row's ``user_id`` — never a cross join, which would mark
    every sub in use (or none)."""
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    rows = MagicMock()
    rows.all.return_value = []
    db.execute = AsyncMock(return_value=rows)
    rows.__aiter__.return_value = []
    db.stream = AsyncMock(return_value=rows)

    asyncio.run(cli._audit_subs(db))

    sql = str(db.stream.await_args_list[0].args[0].compile(dialect=postgresql.dialect()))
    assert "WHERE users.user_id = anon_1.sub" in sql
    assert "FROM users, " not in sql
    assert "UNION SELECT" in sql
    assert "user_metadata" not in sql
    metadata_sql = str(db.stream.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "SELECT audit_logs.user_metadata" in metadata_sql
    assert "CAST" not in metadata_sql and "->" not in metadata_sql
    assert "audit_logs.user_email" in sql and "audit_logs.resource" in sql


def test_the_dry_run_counts_what_the_apply_would_change():
    """#2287 review: the plan's count uses the apply's ``orphaned_only``
    predicate, correlated the same way."""
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=2)))

    assert asyncio.run(cli._count_audit_rows(db, "raw-sub")) == 2

    compiled = db.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    assert str(compiled).endswith(
        "WHERE audit_logs.user_id = %(user_id_1)s::VARCHAR AND NOT (EXISTS (SELECT * \n"
        "FROM users \nWHERE users.user_id = audit_logs.user_id))"
    )
    assert compiled.params == {"user_id_1": "raw-sub"}


def test_pseudonymized_audit_mentions_are_reported_and_counted(capsys):
    """#2298: the rows naming the subject are a change of their own, in the
    plan, the applied report and the summary; an email in use again is
    listed. Never an email or the account id."""
    kept = uuid4()

    def result(dry_run: bool) -> cli.ReprocessResult:
        r = cli.ReprocessResult(dry_run=dry_run, scanned=2)
        r.changed.append(
            cli.RequestChange(
                request_id=REQUEST_ID,
                completed_at=None,
                details_restored=0,
                details_restored_unavailable=0,
                details_stripped=0,
                scrub_marked=0,
                audit_mentions_pseudonymized=3,
            )
        )
        r.mentions_kept.append(kept)
        return r

    plan = result(True)
    assert plan.changes == 3
    cli._print_plan(plan)
    cli._print_applied(result(False))
    out = capsys.readouterr().out
    assert f"would pseudonymize audit mentions  {REQUEST_ID}  3 row(s)\n" in out
    assert f"pseudonymized audit mentions  {REQUEST_ID}  3 row(s)\n" in out
    assert f"audit mentions  {kept}  rows kept: the email is in use again" in out
    assert cli._summary(plan).endswith(", 3 audit mention(s) pseudonymized")


def test_the_mentioned_emails_are_matched_by_hash_in_the_database():
    """#2298: two SELECTs (the distinct ``user_email`` values and the
    ``user:`` resources, the prefix cut in SQL), each filtered by its
    unsalted sha256 in SQL; the matches come back keyed by that hash."""
    from sqlalchemy.dialects import postgresql

    from utils.hashing import sha256_hex

    db = MagicMock()
    emails = MagicMock()
    emails.scalars.return_value = iter(["a@x.io"])
    resources = MagicMock()
    resources.scalars.return_value = iter(["b@x.io", "a@x.io"])
    db.execute = AsyncMock(side_effect=[emails, resources])
    hashes = {sha256_hex("a@x.io"), sha256_hex("b@x.io"), "0" * 64}

    found = asyncio.run(cli._mentioned_emails(db, hashes))

    assert found == {sha256_hex("a@x.io"): "a@x.io", sha256_hex("b@x.io"): "b@x.io"}
    (first, first_params), (second, second_params) = (
        (str(compiled), compiled.params)
        for compiled in (
            c.args[0].compile(dialect=postgresql.dialect()) for c in db.execute.await_args_list
        )
    )
    digest = "encode(sha256(convert_to({}, %(convert_to_1)s::VARCHAR)), %(encode_1)s::VARCHAR)"
    assert first.startswith("SELECT DISTINCT audit_logs.user_email \nFROM audit_logs \nWHERE ")
    assert digest.format("audit_logs.user_email") + " IN (" in first
    assert first_params["convert_to_1"] == "UTF8" and first_params["encode_1"] == "hex"
    assert sorted(hashes) in first_params.values()
    assert second.startswith("SELECT DISTINCT substr(audit_logs.resource, ")
    assert "LIKE" in second and "sha256(convert_to(substr(audit_logs.resource" in second
    assert sorted(hashes) in second_params.values()


def test_no_request_hashes_read_nothing():
    db = MagicMock()
    db.execute = AsyncMock()
    assert asyncio.run(cli._mentioned_emails(db, set())) == {}
    db.execute.assert_not_awaited()


def test_cleared_request_metadata_is_reported_and_counted(capsys):
    """#2345: rows whose IP / user agent were cleared (and ``initiated_by``
    rewrites) are a change of their own; never an IP or a user agent."""

    def result(dry_run: bool) -> cli.ReprocessResult:
        r = cli.ReprocessResult(dry_run=dry_run, scanned=1)
        r.changed.append(
            cli.RequestChange(
                request_id=REQUEST_ID,
                completed_at=None,
                details_restored=0,
                details_restored_unavailable=0,
                details_stripped=0,
                scrub_marked=0,
                audit_metadata_cleared=4,
            )
        )
        return r

    plan = result(True)
    assert plan.changes == 4
    cli._print_plan(plan)
    cli._print_applied(result(False))
    out = capsys.readouterr().out
    assert f"would clear audit request metadata  {REQUEST_ID}  4 row(s)\n" in out
    assert f"cleared audit request metadata  {REQUEST_ID}  4 row(s)\n" in out
    assert cli._summary(plan).endswith(", 4 audit row(s) request metadata cleared")


@pytest.mark.parametrize("dry_run", [True, False])
def test_the_memories_are_locked_before_the_audit_rows(dry_run):
    """#2298 review: the live erasure locks the subject's memories, then the
    audit rows; the re-run takes the same order (the details step first),
    so a re-run and a live erasure that share rows cannot deadlock."""
    from services.erasure_details import RestoreReport, StripCounts, SubjectDetailsErasure

    order: list[str] = []

    async def step(name, result):
        order.append(name)
        return result

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    details = SubjectDetailsErasure(RestoreReport(), StripCounts(0, 0))
    with (
        patch.object(cli, "erase_subject_details", lambda *a, **k: step("details", details)),
        patch.object(cli, "_recover_emails", lambda *a, **k: step("recover", [])),
        patch.object(cli, "_live_emails", lambda *a, **k: step("live", set())),
        patch.object(cli, "_count_audit_rows", lambda *a, **k: step("audit", 0)),
        patch.object(cli, "count_audit_mentions", lambda *a, **k: step("audit", 0)),
        patch.object(cli, "pseudonymize_audit_trail", lambda *a, **k: step("audit", (0, 0))),
        patch.object(cli, "clear_audit_request_metadata", lambda *a, **k: step("audit", set())),
        patch.object(
            cli, "pseudonymize_erasure_audit_initiator", lambda *a, **k: step("audit", set())
        ),
        patch.object(cli, "sync_restored_payloads", AsyncMock()),
    ):
        asyncio.run(
            cli._reprocess_one(
                db,
                REQUEST_ID,
                "p" * 64,
                None,
                None,
                dry_run,
                frozenset(),
                audit_account_id="raw-sub",
                mentioned={},
                swept_emails=set(),
            )
        )

    assert order[0] == "details"
    assert "audit" in order and "details" not in order[1:]


def test_reverted_and_ambiguous_details_keys_are_counted_never_named(capsys):
    """#2275: the plan and the apply name the reverted keys' count and the
    ambiguous keys kept; the ambiguous line is printed only when there are
    any, also for a request with nothing else to change. Counts only."""
    other = uuid4()
    for dry_run, verb in ((True, "would revert details keys"), (False, "reverted details keys")):
        result = cli.ReprocessResult(dry_run=dry_run, scanned=2)
        result.changed.append(
            _change(details_restored=2, details_restored_unavailable=1, details_keys_reverted=4)
        )
        result.ambiguous.append((other, 3))
        assert result.changes == 3  # the memories, not the keys; ambiguity is no change
        (cli._print_plan if dry_run else cli._print_applied)(result)
        out = capsys.readouterr().out
        assert f"{verb}  {REQUEST_ID}  4 key(s) on 3 memory(ies)" in out
        assert (
            f"details keys ambiguous  {other}  3 key(s) kept (stripped history, see runbook)"
        ) in out
        assert ", 4 details key(s) reverted, 3 ambiguous key(s) kept" in cli._summary(result)

    quiet = cli.ReprocessResult(dry_run=True, scanned=1)
    quiet.changed.append(_change(details_stripped=1))
    cli._print_plan(quiet)
    out = capsys.readouterr().out
    assert "ambiguous" not in out and "details keys" not in out
    assert "key(s)" not in cli._summary(quiet)
