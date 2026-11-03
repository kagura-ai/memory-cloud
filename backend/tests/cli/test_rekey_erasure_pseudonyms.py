"""Tests for ``cli/rekey_erasure_pseudonyms`` (#2598).

The re-key itself runs against Postgres in
``tests/integration/test_rekey_erasure_pseudonyms_db.py``; this pins the command
around it — a read-only plan by default, ``--apply`` after the confirmation,
``--stamp-current`` — the plan's rendering (counts only, never a value), the
per-row ``resource`` rewrite, and that the column list covers every column
the erasure writes a pseudonym to.
"""

from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import rekey_erasure_pseudonyms as cli  # noqa: E402
from utils.hashing import sha256_hex  # noqa: E402

P = sha256_hex("sub", salt="old")
E = sha256_hex("member@example.com", salt="old")
OTHER = sha256_hex("other", salt="old")
NEW = {value: sha256_hex(value, salt="new") for value in (P, E, OTHER)}
SUBS = {P: NEW[P], OTHER: NEW[OTHER]}
VALUES = {**SUBS, E: NEW[E]}
KEYS = sorted(VALUES)


def _result(*, dry_run: bool, changed: bool = True, stamp_current: bool = False) -> cli.RekeyResult:
    result = cli.RekeyResult(
        dry_run=dry_run, stamp_current=stamp_current, current=1, account_id_rows=1
    )
    if changed:
        result.other_salt, result.unmarked, result.stamped = 1, 1, 2
        if not stamp_current:
            result.columns = {"memories.user_id": 3, "audit_logs.user_email": 2}
            result.payload_memories = 3
            result.kept_current = 1
    if not dry_run and not stamp_current:
        result.payload = cli.PayloadCounts(memories=3, points=2)
    return result


async def _no_db():
    yield object()


def _run(argv: list[str], worker: AsyncMock) -> int:
    with (
        patch.object(cli, "rekey_erasure_pseudonyms", worker),
        patch("cli._oneshot.get_db", _no_db),
        patch.object(cli, "configure_logging", lambda level: None),
    ):
        return asyncio.run(cli._main(cli._parse(argv)))


class TestTheCommand:
    @pytest.mark.parametrize("argv", [[], ["--plan"], ["--dry-run"]])
    def test_the_default_is_a_read_only_plan(self, argv, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True))

        assert _run(argv, worker) == 0

        assert [call.kwargs for call in worker.await_args_list] == [
            {"dry_run": True, "stamp_current": False}
        ]
        out = capsys.readouterr().out
        assert "erasure requests that store a pseudonym: 3" in out
        assert "  no salt fingerprint: 1" in out
        assert re.search(r"memories\.user_id\s+3", out)
        assert "would re-key 2 request(s), 5 row(s)" in out
        assert "dry run — pass --apply to write" in out

    def test_apply_writes_after_the_plan(self, capsys):
        worker = AsyncMock(side_effect=[_result(dry_run=True), _result(dry_run=False)])

        assert _run(["--apply", "--yes"], worker) == 0

        assert [call.kwargs["dry_run"] for call in worker.await_args_list] == [True, False]
        out = capsys.readouterr().out
        assert "updated 2 erasure request(s) to the current salt (5 row(s) in 2 column(s))" in out
        assert "payloads  patched 2 point(s) on 3 memory(ies), failed 0" in out

    def test_stamp_current_reaches_the_worker(self, capsys):
        worker = AsyncMock(
            side_effect=[
                _result(dry_run=True, stamp_current=True),
                _result(dry_run=False, stamp_current=True),
            ]
        )

        assert _run(["--stamp-current", "--apply", "--yes"], worker) == 0

        assert [call.kwargs for call in worker.await_args_list] == [
            {"dry_run": True, "stamp_current": True},
            {"dry_run": False, "stamp_current": True},
        ]
        out = capsys.readouterr().out
        assert "would stamp 2 request(s) with the current salt's fingerprint" in out
        assert "updated 2 erasure request(s) with the current salt's fingerprint" in out

    def test_the_confirmation_is_honoured(self, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True))

        with patch("builtins.input", return_value="n") as prompt:
            assert _run(["--apply"], worker) == 0

        assert "Update 2 erasure request(s) to the current salt" in prompt.call_args.args[0]
        assert [call.kwargs["dry_run"] for call in worker.await_args_list] == [True]
        assert "skipped" in capsys.readouterr().out

    def test_nothing_to_change_never_writes(self, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True, changed=False))

        assert _run(["--apply", "--yes"], worker) == 0

        assert [call.kwargs["dry_run"] for call in worker.await_args_list] == [True]
        assert "dry run" not in capsys.readouterr().out

    def test_an_error_is_an_error_exit(self, capsys):
        worker = AsyncMock(side_effect=RuntimeError("database down"))

        assert _run([], worker) == 1

        assert "database down" in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [["--yes"], ["--plan", "--yes"]])
    def test_yes_without_apply_is_a_usage_error(self, argv, capsys):
        worker = AsyncMock(return_value=_result(dry_run=True))

        assert _run(argv, worker) == 2

        worker.assert_not_awaited()

    def test_plan_and_apply_are_exclusive(self):
        with pytest.raises(SystemExit) as exc:
            cli._parse(["--plan", "--apply"])
        assert exc.value.code == 2


class TestThePlan:
    def test_it_prints_counts_and_never_a_value(self, capsys):
        result = _result(dry_run=True)
        result.payload_pending = 1
        result.kept_live = 1

        cli._print_plan(result)
        cli._print_applied(_result(dry_run=False))

        out = capsys.readouterr().out
        # no pseudonym, prefix or fingerprint: no run of 8 hex digits at all
        assert re.search(r"[0-9a-f]{8,}", out) is None, out
        assert cli.UNMARKED_HINT in out
        assert cli.ACCOUNT_ID_ROWS in out
        assert cli.KNOWN_LIMIT in out
        assert "kept  1 value(s) of a request already under the current salt" in out
        assert "kept  1 value(s) a live account uses as its id" in out
        assert "payload patch pending from an earlier apply: 1 request(s)" in out
        assert "vector payloads: 3 memory(ies)" in out

    @pytest.mark.parametrize("stamp_current", [False, True])
    def test_unfinished_erasures_and_scopes_are_reported(self, stamp_current, capsys):
        """What has to finish before a rotation, in the plan of both modes."""
        result = _result(dry_run=True, stamp_current=stamp_current)
        result.in_progress, result.scopes_pending = 1, 2

        cli._print_plan(result)

        out = capsys.readouterr().out
        assert re.search(r"[0-9a-f]{8,}", out) is None, out
        assert f"requests  1  {cli.IN_PROGRESS_ROWS}" in out
        assert f"vector erasure scopes  2  {cli.SCOPES_PENDING}" in out

    def test_a_placeholder_salt_re_keys_nothing(self, capsys):
        result = _result(dry_run=True)
        result.placeholder_salt = True

        cli._print_plan(result)

        assert result.changes == 0
        assert cli.PLACEHOLDER_SALT in capsys.readouterr().out

    def test_a_failed_payload_patch_is_reported(self, capsys):
        result = _result(dry_run=False)
        result.payload = cli.PayloadCounts(memories=2, points=1, failed=1, requests_pending=1)

        cli._print_applied(result)

        out = capsys.readouterr().out
        assert "failed 1; 1 request(s) keep the mark and are patched again" in out
        assert "erasure_rekey_payload_failed" in out


def _rekeyed(resource: str, user_id: str | None, length: int = 255) -> str | None:
    return cli.rekeyed_resource(resource, user_id, SUBS, VALUES, KEYS, length)


class TestTheResourceRewrite:
    """Where the erasure put a pseudonym in ``resource``, the re-key finds it."""

    @pytest.mark.parametrize("value", [P, E, OTHER])
    def test_a_whole_user_value_on_any_row(self, value):
        assert _rekeyed(f"user:{value}", "someone-else") == f"user:{NEW[value]}"

    def test_a_user_value_not_stored_is_kept(self):
        assert _rekeyed(f"user:{'a' * 64}", P) == f"user:{'a' * 64}"
        assert _rekeyed(f"user:{E}-x", P) == f"user:{E}-x"

    def test_the_erasure_rows_marker(self):
        assert _rekeyed(f"user_pseudonym:{P[:16]}", P) == f"user_pseudonym:{NEW[P][:16]}"
        # another subject's marker on this subject's row is no marker of theirs
        assert _rekeyed(f"user_pseudonym:{OTHER[:16]}", P) == f"user_pseudonym:{OTHER[:16]}"

    def test_any_occurrence_on_the_subjects_own_row(self):
        assert _rekeyed(f"export/{E}/{P}.csv", P) == f"export/{NEW[E]}/{NEW[P]}.csv"
        # next to other hex digits, as the erasure's literal replacement left it
        assert _rekeyed(f"ab{E}cd", P) == f"ab{NEW[E]}cd"

    def test_never_a_substring_on_another_actors_row(self):
        assert _rekeyed(f"export/{E}", "someone-else") == f"export/{E}"
        assert _rekeyed(f"export/{E}", None) == f"export/{E}"

    def test_the_leading_part_of_a_value_cut_at_the_columns_length(self):
        resource = f"path/{P}/{E[:30]}"
        assert len(resource) == 100

        assert _rekeyed(resource, P, length=100) == f"path/{NEW[P]}/{NEW[E][:30]}"
        # not at the column's length: not a cut value
        assert _rekeyed(resource, P, length=101) == f"path/{NEW[P]}/{E[:30]}"

    def test_a_short_or_ambiguous_cut_is_left(self):
        short = f"{'x' * 93}{E[:7]}"
        assert _rekeyed(short, P, length=100) == short
        twin = E[:12] + "0" * 52
        values = {**VALUES, twin: "f" * 64}
        resource = f"{'x' * 88}{E[:12]}"
        assert cli.rekeyed_resource(resource, P, SUBS, values, sorted(values), 100) == resource


class TestTheColumnList:
    """Every column the erasure writes the subject's pseudonym to is re-keyed."""

    @staticmethod
    def _erasure_columns() -> set[tuple[str, str]]:
        source = (_BACKEND_SRC / "services" / "account_erasure_service.py").read_text()
        (function,) = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_delete_postgres"
        ]
        found: set[tuple[str, str]] = set()

        def pair(model: ast.expr, column: ast.expr) -> None:
            assert isinstance(model, ast.Name) and isinstance(column, ast.Attribute)
            found.add((model.id, column.attr))

        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_pseudonymize_field"
                # the loop over ``authorship_sweeps`` (parsed below) passes names
                and isinstance(node.args[1], ast.Attribute)
            ):
                pair(node.args[0], node.args[1])
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
                "authorship_sweeps"
            ):
                assert isinstance(node.value, ast.Tuple)
                for element in node.value.elts:
                    assert isinstance(element, ast.Tuple)
                    pair(element.elts[0], element.elts[1])
        return found

    def test_it_covers_the_erasures_columns(self):
        listed = {(c.class_.__name__, c.key) for c in (*cli.SUB_COLUMNS, *cli.REQUEST_COLUMNS)}
        swept = self._erasure_columns()
        assert len(swept) > 20  # the parse found the sweeps
        direct = {
            ("Memory", "user_id"),
            ("MemoryRevision", "actor_user_id"),
            ("MemorySessionRollback", "principal_user_id"),
            ("MemorySessionRollback", "requested_by"),
            ("MemoryAccessEvent", "user_id"),
            ("SecretAccessLog", "actor_user_id"),
            ("SecretAccessLog", "recipient_identity"),
            ("VectorErasureJob", "subject_pseudonym"),
            ("ErasureRequest", "user_id"),
            ("ErasureRequest", "initiated_by"),
        }
        assert swept | direct <= listed
        assert len(listed) == len(cli.SUB_COLUMNS) + len(cli.REQUEST_COLUMNS)

    def test_contexts_come_before_memories_and_memories_before_revisions(self):
        order = [cli._label(c) for c in cli.SUB_COLUMNS]
        assert order.index("contexts.created_by") < order.index("memories.user_id")
        assert order.index("memories.user_id") < order.index("memory_revisions.actor_user_id")
