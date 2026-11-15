"""``python -m src.cli.public_mcp_domains`` (#2809): list, show, release.

The parser and the plan / apply flow need no database. The commands on real
rows use the test database (skipped without one): they work while
``MCP_PUBLIC_DOMAINS_ENABLED`` is off, ``release`` plans by default, releases
a held name with the cooldown, deletes the row with ``--skip-cooldown`` (or a
``pending`` row), and audits as the command line's actor with the host name's
digest only. Host names go to stdout, never to a log line.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from cli import _oneshot
from cli import public_mcp_domains as cli
from config.settings import get_settings
from models.auth import AuditLog
from models.public_mcp_domain import PublicMcpDomain
from utils.datetime import utcnow

# ----------------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------------


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        cli._parse([])


def test_show_needs_exactly_one_selector() -> None:
    with pytest.raises(SystemExit):
        cli._parse(["show"])
    with pytest.raises(SystemExit):
        cli._parse(["show", "--domain-id", str(uuid4()), "--hostname", "a.example.org"])
    assert cli._parse(["show", "--hostname", "a.example.org"]).hostname == "a.example.org"


def test_release_needs_a_domain_id() -> None:
    with pytest.raises(SystemExit):
        cli._parse(["release"])
    with pytest.raises(SystemExit):
        cli._parse(["release", "--domain-id", "not-a-uuid"])
    args = cli._parse(["release", "--domain-id", str(uuid4())])
    assert (args.apply, args.yes, args.skip_cooldown) == (False, False, False)


def test_list_filters_are_validated() -> None:
    with pytest.raises(SystemExit):
        cli._parse(["list", "--state", "held"])
    with pytest.raises(SystemExit):
        cli._parse(["list", "--workspace", "nope"])
    args = cli._parse(["list", "--state", "active", "--workspace", str(uuid4())])
    assert args.state == "active"


def test_the_actor_is_not_an_operator_principal() -> None:
    # "operator:" ids are minted by auth.system_admin_mode only.
    assert cli.CLI_ACTOR.user_id == cli.CLI_ACTOR.email == "cli:public-mcp-domains"


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch):
    def opened():
        raise AssertionError("opened the database")

    monkeypatch.setattr(_oneshot, "get_db", opened)
    monkeypatch.setattr(_oneshot, "require_settings", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)


async def test_yes_without_apply_is_a_usage_error(no_db, capsys) -> None:
    code = await cli._main(cli._parse(["release", "--domain-id", str(uuid4()), "--yes"]))
    assert code == 2
    assert "--yes needs --apply" in capsys.readouterr().err


async def test_show_refuses_a_malformed_host_name(no_db, capsys) -> None:
    code = await cli._main(cli._parse(["show", "--hostname", "not a host"]))
    assert code == 2
    assert "not a host name" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# On real rows
# ----------------------------------------------------------------------------


@dataclass
class Rows:
    factory: async_sessionmaker[AsyncSession]
    ids: list[UUID] = field(default_factory=list)

    async def add(self, state: str, **values: Any) -> PublicMcpDomain:
        now = utcnow()
        fields: dict[str, Any] = {
            "hostname": f"cli-{uuid4().hex[:10]}.example.org",
            "workspace_id": uuid4(),
            "endpoint_id": uuid4(),
            "state": state,
            "verification_token": uuid4().hex * 2,
        }
        if state == "active":
            fields |= {"serve_until": now + timedelta(hours=1), "verified_at": now}
            fields |= {"proof_ok_at": now}
        if state == "suspended":
            fields |= {"suspended_reason": "proof_lost", "suspended_at": now}
            fields |= {"verified_at": now, "proof_ok_at": now}
        if state == "released":
            fields |= {"released_at": now, "cooldown_until": now + timedelta(days=3)}
        fields |= values
        async with self.factory() as s:
            row = PublicMcpDomain(**fields)
            s.add(row)
            await s.commit()
            await s.refresh(row)
        self.ids.append(row.id)
        return row

    async def get(self, domain_id: UUID) -> PublicMcpDomain | None:
        async with self.factory() as s:
            return await s.get(PublicMcpDomain, domain_id)

    async def audits(self, domain_id: UUID) -> list[AuditLog]:
        async with self.factory() as s:
            return list(
                (
                    await s.execute(
                        select(AuditLog).where(
                            AuditLog.resource == f"public_mcp_domain:{domain_id}"
                        )
                    )
                )
                .scalars()
                .all()
            )


@pytest_asyncio.fixture
async def rows(async_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rows]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    monkeypatch.setattr(_oneshot, "get_db", get_db)
    monkeypatch.setattr(_oneshot, "require_settings", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    # The command line is how an operator removes names with the switch off.
    monkeypatch.setattr(get_settings(), "mcp_public_domains_enabled", False)
    monkeypatch.setattr(get_settings(), "mcp_public_domains_reassign_cooldown_hours", 168)
    r = Rows(factory)
    try:
        yield r
    finally:
        async with factory() as s:
            await s.execute(delete(PublicMcpDomain).where(PublicMcpDomain.id.in_(r.ids)))
            await s.execute(
                delete(AuditLog).where(
                    AuditLog.resource.in_([f"public_mcp_domain:{i}" for i in r.ids])
                )
            )
            await s.commit()


async def _run(argv: list[str]) -> int:
    return await cli._main(cli._parse(argv))


async def test_list_filters_by_state_and_workspace(rows: Rows, capsys) -> None:
    ws = uuid4()
    active = await rows.add("active", workspace_id=ws)
    pending = await rows.add("pending", workspace_id=ws)
    other = await rows.add("active")

    assert await _run(["list", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert active.hostname in out and pending.hostname in out
    assert other.hostname not in out

    assert await _run(["list", "--workspace", str(ws), "--state", "pending"]) == 0
    out = capsys.readouterr().out
    assert pending.hostname in out and active.hostname not in out


async def test_show_by_hostname_prints_every_row_of_the_name(rows: Rows, capsys) -> None:
    released = await rows.add("released")
    active = await rows.add("active", hostname=released.hostname)

    assert await _run(["show", "--hostname", released.hostname.upper() + "."]) == 0
    out = capsys.readouterr().out
    assert str(released.id) in out and str(active.id) in out
    assert released.verification_token not in out and active.verification_token not in out

    assert await _run(["show", "--domain-id", str(active.id)]) == 0
    out = capsys.readouterr().out
    assert str(active.id) in out and str(released.id) not in out


async def test_show_unknown_exits_1(rows: Rows, capsys) -> None:
    assert await _run(["show", "--domain-id", str(uuid4())]) == 1
    assert "no public MCP domain" in capsys.readouterr().err


async def test_release_plans_by_default(rows: Rows, capsys) -> None:
    active = await rows.add("active")
    assert await _run(["release", "--domain-id", str(active.id)]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "release" in out
    after = await rows.get(active.id)
    assert after is not None and after.state == "active"
    assert after.check_seq == active.check_seq
    assert await rows.audits(active.id) == []


async def test_release_apply_releases_with_the_cooldown(rows: Rows, capsys) -> None:
    for state in ("active", "suspended"):
        row = await rows.add(state)
        with capture_logs() as logs:
            assert await _run(["release", "--domain-id", str(row.id), "--apply", "--yes"]) == 0
        assert "released 1 domain(s)" in capsys.readouterr().out
        after = await rows.get(row.id)
        assert after is not None
        assert (after.state, after.suspended_reason) == ("released", None)
        assert after.released_at is not None and after.cooldown_until is not None
        cooldown = after.cooldown_until - after.released_at
        assert cooldown == timedelta(hours=168)
        assert after.check_seq == row.check_seq + 1
        (audit,) = await rows.audits(row.id)
        assert audit.action == "public_mcp_domain.released"
        assert audit.user_id == audit.user_email == "cli:public-mcp-domains"
        meta = audit.user_metadata
        assert (meta["from_state"], meta["to_state"], meta["reason"]) == (
            state,
            "released",
            "operator",
        )
        assert meta["hostname_sha256"] == hashlib.sha256(row.hostname.encode()).hexdigest()
        assert row.hostname not in str(meta)
        assert all(row.hostname not in str(entry) for entry in logs)


async def test_release_skip_cooldown_deletes_the_row(rows: Rows, capsys) -> None:
    for state in ("active", "released"):
        row = await rows.add(state)
        argv = ["release", "--domain-id", str(row.id), "--skip-cooldown", "--apply", "--yes"]
        assert await _run(argv) == 0
        assert "deleted 1 domain(s)" in capsys.readouterr().out
        assert await rows.get(row.id) is None
        (audit,) = await rows.audits(row.id)
        assert audit.user_metadata["from_state"] == state
        assert audit.user_metadata["to_state"] is None


async def test_release_of_a_pending_row_deletes_it(rows: Rows, capsys) -> None:
    row = await rows.add("pending")
    assert await _run(["release", "--domain-id", str(row.id), "--apply", "--yes"]) == 0
    assert await rows.get(row.id) is None
    (audit,) = await rows.audits(row.id)
    assert audit.user_metadata["from_state"] == "pending"


async def test_release_of_a_released_row_without_skip_changes_nothing(rows: Rows, capsys) -> None:
    row = await rows.add("released")
    assert await _run(["release", "--domain-id", str(row.id), "--apply", "--yes"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    after = await rows.get(row.id)
    assert after is not None and after.check_seq == row.check_seq
    assert await rows.audits(row.id) == []


async def test_release_unknown_exits_1(rows: Rows, capsys) -> None:
    assert await _run(["release", "--domain-id", str(uuid4()), "--apply", "--yes"]) == 1
    assert "no public MCP domain" in capsys.readouterr().err
