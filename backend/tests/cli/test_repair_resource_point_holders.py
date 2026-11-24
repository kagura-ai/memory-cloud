"""``python -m src.cli.repair_resource_point_holders`` (parse and report)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cli import repair_resource_point_holders as cli
from tasks.resource_point_holders import HolderRepair


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
        cli._parse(["--context", str(uuid4()), "--after", str(context)])
    with pytest.raises(SystemExit):
        cli._parse(["--all", "--limit", "0"])


def test_plan_is_the_default():
    assert cli._parse(["--all"]).apply is False


def test_the_plan_reports_counts_only(capsys):
    result = HolderRepair(
        dry_run=True,
        contexts=3,
        released_rows=2,
        shared_points=1,
        foreign_points=4,
        missing_points=5,
    )
    cli._print_plan(result)
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "contexts        3",
        "released_rows   2",
        "shared_points   1",
        "foreign_points  4",
        "missing_points  5",
    ]
    assert cli._changes(result) == 7


def test_the_applied_report_ends_with_a_resume_line_when_stopped(capsys):
    last = uuid4()
    result = HolderRepair(
        dry_run=False, detached=2, rebuilt=1, skipped=1, last_context=last, stopped="limit"
    )
    cli._print_applied(result)
    out = capsys.readouterr().out
    assert "detached        2" in out
    assert "rebuilt         1" in out
    assert "skipped         1" in out
    assert f"resume          --after {last}" in out
    assert cli._changes(result) == 3


def test_no_resume_line_when_the_run_finished(capsys):
    cli._print_applied(HolderRepair(dry_run=False, last_context=uuid4()))
    assert "resume" not in capsys.readouterr().out
