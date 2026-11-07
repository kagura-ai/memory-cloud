"""Dated stock, explicit writes and CSV-only stdout (#2704)."""

import csv
import io
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli import beta_invite_batches as cli
from models.beta_invite import BetaInvite
from services.beta_invite_service import MintedBetaInvite


def args(*extra):
    return cli._parse(
        [
            "mint",
            "--inviter",
            "admin-id",
            "--start",
            "2030-01-01",
            "--days",
            "2",
            "--per-day",
            "3",
            "--label-prefix",
            "waitlist",
            *extra,
        ]
    )


def test_calendar_expiry_is_relative_to_each_sending_day():
    batches = cli.dated_batches(args())
    assert batches == [
        ("waitlist-2030-01-01", datetime(2030, 1, 8, 1), 3),
        ("waitlist-2030-01-02", datetime(2030, 1, 9, 1), 3),
    ]


def test_timezone_and_time_override():
    batches = cli.dated_batches(args("--timezone", "UTC", "--expiry-time", "16:30"))
    assert batches[0][1] == datetime(2030, 1, 8, 16, 30)


@pytest.mark.parametrize("options", [("--apply",), ("--yes",)])
def test_requires_both_write_flags(options):
    with pytest.raises(SystemExit):
        args(*options)


@pytest.mark.parametrize(
    "options",
    [
        ("--days", "0"),
        ("--per-day", "0"),
        ("--days", "60", "--per-day", "1000"),
        ("--label-prefix", "=formula"),
        ("--expiry-time", "10:00+09:00"),
    ],
)
def test_invalid_schedule(options):
    with pytest.raises(ValueError):
        cli.dated_batches(args(*options))


async def test_stdout_is_only_csv_and_plan_mints_nothing(monkeypatch, capsys):
    db = MagicMock()
    svc = MagicMock()
    invite = BetaInvite(
        id="invite-id", label="waitlist-2030-01-01", expires_at=datetime(2030, 1, 8, 1)
    )
    svc.create_batches = AsyncMock(
        side_effect=[[], [MintedBetaInvite(invite, "https://app/join/secret")]]
    )
    monkeypatch.setattr(cli, "BetaInviteService", lambda _: svc)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr("cli._oneshot.require_settings", lambda: None)

    async def get_db():
        yield db

    monkeypatch.setattr("cli._oneshot.get_db", get_db)
    assert await cli._main(args("--apply", "--yes")) == 0
    captured = capsys.readouterr()
    rows = list(csv.DictReader(io.StringIO(captured.out)))
    assert rows[0]["url"] == "https://app/join/secret"
    assert "secret" not in captured.err
    assert "waitlist" not in captured.err
    assert svc.create_batches.await_args_list[0].kwargs == {"dry_run": True}
    assert svc.create_batches.await_args_list[1].kwargs == {"dry_run": False}


async def test_sql_error_does_not_print_parameters(monkeypatch, capsys):
    svc = MagicMock()
    svc.create_batches = AsyncMock(side_effect=RuntimeError("secret-label token-hash"))
    monkeypatch.setattr(cli, "BetaInviteService", lambda _: svc)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr("cli._oneshot.require_settings", lambda: None)

    async def get_db():
        yield MagicMock()

    monkeypatch.setattr("cli._oneshot.get_db", get_db)
    assert await cli._main(args()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret-label" not in captured.err


@pytest.mark.parametrize("count", [0, 3])
async def test_revoke_prints_count_including_noop(monkeypatch, capsys, count):
    svc = MagicMock()
    svc.revoke_unused_by_label = AsyncMock(return_value=count)
    monkeypatch.setattr(cli, "BetaInviteService", lambda _: svc)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr("cli._oneshot.require_settings", lambda: None)

    async def get_db():
        yield MagicMock()

    monkeypatch.setattr("cli._oneshot.get_db", get_db)
    parsed = cli._parse(["revoke", "--inviter", "admin", "--label", "private", "--apply", "--yes"])
    assert await cli._main(parsed) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{count}\n"
    assert "private" not in captured.err


def test_rejects_nonexistent_dst_expiry():
    parsed = args(
        "--start",
        "2030-03-03",
        "--timezone",
        "America/New_York",
        "--expiry-time",
        "02:30",
        "--days",
        "1",
    )
    with pytest.raises(ValueError, match="DST"):
        cli.dated_batches(parsed)
