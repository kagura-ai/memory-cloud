"""#2984: ``python -m src.cli.rebuild_keyword_vectors`` (parse and report)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cli import rebuild_keyword_vectors as cli
from tasks.keyword_rebuild import KeywordRebuild


def test_one_target_is_required():
    with pytest.raises(SystemExit):
        cli._parse([])
    with pytest.raises(SystemExit):
        cli._parse(["--all", "--context", str(uuid4())])


def test_resume_and_limit_go_with_all():
    context = uuid4()
    args = cli._parse(["--all", "--after", str(context), "--limit", "5"])
    assert (args.after, args.limit, args.context) == (context, 5, None)
    with pytest.raises(SystemExit):
        cli._parse(["--context", str(uuid4()), "--limit", "5"])
    with pytest.raises(SystemExit):
        cli._parse(["--all", "--limit", "0"])


def test_plan_is_the_default():
    args = cli._parse(["--all"])
    assert args.apply is False


def test_the_plan_report(capsys):
    cli._print_plan(KeywordRebuild(dry_run=True, contexts=2, rows=10, rows_without_lengths=4))
    out = capsys.readouterr().out
    assert "contexts                      2" in out
    assert "rows without keyword lengths  4" in out
    assert cli._changes(KeywordRebuild(dry_run=True, rows=10)) == 10


def test_the_applied_report_ends_with_a_resume_line_when_stopped(capsys):
    last = uuid4()
    result = KeywordRebuild(
        dry_run=False, vectors_written=3, skipped=1, last_context=last, stopped="limit"
    )
    cli._print_applied(result)
    out = capsys.readouterr().out
    assert "vectors_written   3" in out
    assert "skipped           1" in out
    assert f"resume            --after {last}" in out
    assert cli._changes(result) == 3


def test_no_resume_line_when_the_run_finished(capsys):
    cli._print_applied(KeywordRebuild(dry_run=False, last_context=uuid4()))
    assert "resume" not in capsys.readouterr().out


def test_lance_plan_says_what_it_rewrites(capsys):
    result = KeywordRebuild(dry_run=True, store="lance", contexts=1, rows=5)
    cli._print_plan(result)
    out = capsys.readouterr().out
    assert "LanceDB (token fields and full-text column only, no vectors)" in out
    assert "embedded rows                 5" in out
    assert cli._changes(result) == 5
