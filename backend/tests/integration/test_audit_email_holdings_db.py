"""#2359: erasure audit sweeps match an address only while the subject held it.

The holdings are rebuilt from existing data (``erasure_details.email_holdings``):
the subject's ``oauth_user_email_synced`` rows (HMACs of the old and new
address), the account's creation or first audit row, and the erasure
requests' times. Each test writes rows at fixed times (naive UTC) and checks
which of them the live erasure (``AccountErasureService._pseudonymize_audit_logs``)
and the re-run (``cli.reprocess_erasures``) rewrite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cli import reprocess_erasures as cli
from config.settings import get_settings
from models.auth import AuditLog, User
from models.erasure import ErasureRequest
from services.account_erasure_service import AccountErasureService
from services.erasure_details import EMAIL_SYNC_ACTION, erasure_pseudonym
from utils.hashing import hmac_sha256_hex, sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SALT = "salt-2359"
_QDRANT = "services.erasure_details.update_memory_payload_in_qdrant"
T0 = datetime(2026, 3, 1, 9, 0, 0)  # naive UTC, as audit_logs.created_at


def at(days: float) -> datetime:
    return T0 + timedelta(days=days)


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@dataclass
class World:
    """Rows of one test, removed afterwards."""

    engine: AsyncEngine
    tag: str
    rows: list[int] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    requests: list[uuid.UUID] = field(default_factory=list)
    subs: list[str] = field(default_factory=list)

    def sub(self, name: str) -> str:
        sub = f"google-oauth2|{name}-{self.tag}"
        self.subs.append(sub)
        return sub

    def email(self, name: str) -> str:
        return f"{name}-{self.tag}@example.com"

    async def user(self, sub: str, email: str, created: datetime) -> None:
        async with _maker(self.engine)() as db:
            db.add(User(user_id=sub, email=email, name="u", role="user", created_at=created))
            await db.commit()
        self.users.append(sub)

    async def row(
        self, user_id: str, user_email: str, resource: str, when: datetime, **extra: Any
    ) -> int:
        async with _maker(self.engine)() as db:
            row = AuditLog(
                user_id=user_id,
                user_email=user_email,
                action=extra.pop("action", "role_assign"),
                resource=resource,
                created_at=when,
                ip_address="192.0.2.9",
                user_agent="agent",
                **extra,
            )
            db.add(row)
            await db.commit()
        self.rows.append(row.id)
        return row.id

    async def sync(
        self, sub: str, old: str, new: str, when: datetime, *, key: str | None = None
    ) -> int:
        """What ``RoleManager._sync_existing_user`` writes on an email change."""
        key = key or get_settings().audit_hmac_key
        return await self.row(
            sub,
            "oauth-callback",
            f"user:{sub}",
            when,
            action=EMAIL_SYNC_ACTION,
            old_value_hash=hmac_sha256_hex(old, key),
            new_value_hash=hmac_sha256_hex(new, key),
        )

    async def erased_before_the_fix(self, sub: str) -> None:
        """What an erasure before #2287 left: the user row gone, the own rows
        carrying the pseudonym (the sub in ``resource`` too), every address
        on them and on other actors' rows raw."""
        p = erasure_pseudonym(sub)
        async with _maker(self.engine)() as db:
            await db.execute(delete(User).where(User.user_id == sub))
            await db.execute(
                update(AuditLog)
                .where(AuditLog.user_id == sub, AuditLog.resource == f"user:{sub}")
                .values(resource=f"user:{p}")
            )
            await db.execute(update(AuditLog).where(AuditLog.user_id == sub).values(user_id=p))
            await db.commit()

    async def request(self, sub: str, email: str, completed: datetime) -> uuid.UUID:
        request = ErasureRequest(
            id=uuid.uuid4(),
            user_id=erasure_pseudonym(sub),
            user_email_hash=sha256_hex(email),
            initiated_by="admin-x",
            is_self_service=False,
            reason_code="other",
            status="complete",
            completed_at=completed.replace(tzinfo=UTC),
            deleted_data_summary={"postgres": {"memories_scrubbed": 0}},
        )
        async with _maker(self.engine)() as db:
            db.add(request)
            await db.commit()
        self.requests.append(request.id)
        return request.id

    async def live_erasure(self, sub: str) -> dict[str, int]:
        """The live erasure's audit step for ``sub``, committed."""
        async with _maker(self.engine)() as db:
            user = (await db.execute(select(User).where(User.user_id == sub))).scalar_one()
            counts = await AccountErasureService(db, MagicMock())._pseudonymize_audit_logs(user)
            await db.commit()
        return counts

    async def values(self, *ids: int) -> list[tuple[str, str]]:
        async with _maker(self.engine)() as db:
            rows = {
                row.id: (row.user_email, row.resource)
                for row in (
                    await db.execute(select(AuditLog).where(AuditLog.id.in_(ids)))
                ).scalars()
            }
        return [rows[i] for i in ids]


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine) -> AsyncIterator[World]:
    w = World(async_engine, uuid.uuid4().hex[:8])
    try:
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            yield w
    finally:
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            pseudonyms = [erasure_pseudonym(s) for s in w.subs]
        async with _maker(async_engine)() as db:
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.id.in_(w.rows)
                    | AuditLog.user_id.in_(pseudonyms)
                    | AuditLog.user_id.in_(w.subs)
                )
            )
            await db.execute(delete(ErasureRequest).where(ErasureRequest.id.in_(w.requests)))
            await db.execute(delete(User).where(User.user_id.in_(w.users)))
            await db.commit()


async def _run(engine: AsyncEngine, argv: list[str]) -> int:
    async def get_db() -> AsyncIterator[AsyncSession]:
        async with _maker(engine)() as session:
            yield session

    with (
        patch("cli._oneshot.get_db", get_db),
        patch.object(cli, "configure_logging", lambda level: None),
        patch(_QDRANT, AsyncMock()),
    ):
        return await cli._main(cli._parse(argv))


def _p(value: str) -> str:
    return erasure_pseudonym(value)


def _lines(out: str, needle: str) -> list[str]:
    return [line for line in out.splitlines() if needle in line]


@pytest.mark.parametrize("path", ["live", "rerun"])
async def test_an_address_reused_after_an_email_change(
    world: World, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    """S held x, synced to y; Q (signed up earlier) synced into x later.
    Rows naming x while S held it are S's; rows naming x after Q took it
    are Q's and stay; a row in the gap between (nobody known) stays too.
    The re-run's plan counts what its apply changes; a second run is a
    no-op; nothing identifying is printed."""
    s, q, admin = world.sub("s"), world.sub("q"), world.sub("admin")
    x, y, qx = world.email("x"), world.email("y"), world.email("q")
    await world.user(s, y, at(0))
    await world.user(q, x, at(1))
    own = await world.row(s, x, "workspace:w", at(2), action="member_add")
    await world.sync(s, x, y, at(10))
    await world.sync(q, qx, x, at(20))
    about_s = await world.row(admin, "admin@example.com", f"user:{x}", at(5))
    by_s = await world.row(q, x, f"user:{qx}", at(6))
    about_s_new = await world.row(admin, "admin@example.com", f"user:{y}", at(15))
    gap = await world.row(admin, "admin@example.com", f"user:{x}", at(15))
    about_q = await world.row(admin, "admin@example.com", f"user:{x}", at(25))
    by_q = await world.row(admin, x, "workspace:w", at(26))

    if path == "live":
        counts = await world.live_erasure(s)
        assert counts == {
            "audit_logs_pseudonymized": 2,  # its own row and its sync row
            "audit_mentions_pseudonymized": 3,
            "audit_mentions_kept": 0,
            "audit_addresses_not_recovered": 0,
        }
        assert (await world.values(own))[0][0] == _p(x)
    else:
        await world.erased_before_the_fix(s)
        request = await world.request(s, y, at(30))
        assert await _run(world.engine, ["--request", str(request)]) == 0
        plan = capsys.readouterr().out
        assert await _run(world.engine, ["--request", str(request), "--apply", "--yes"]) == 0
        out = capsys.readouterr().out
        # its own row (carrying the pseudonym, x raw) is a mention by address
        line = f"audit mentions  {request}  4 row(s)"
        assert _lines(plan, f"would pseudonymize {line}") and _lines(out, f"pseudonymized {line}")
        for text_ in (plan, out):
            assert x not in text_ and y not in text_ and s not in text_
            assert "kept" not in text_ and "not recovered" not in text_
        assert (await world.values(own))[0][0] == _p(x)
        assert await _run(world.engine, ["--request", str(request), "--apply", "--yes"]) == 0
        assert "would change 0" in capsys.readouterr().out

    assert await world.values(about_s, by_s, about_s_new, gap, about_q, by_q) == [
        ("admin@example.com", f"user:{_p(x)}"),
        (_p(x), f"user:{qx}"),
        ("admin@example.com", f"user:{_p(y)}"),
        ("admin@example.com", f"user:{x}"),
        ("admin@example.com", f"user:{x}"),
        (x, "workspace:w"),
    ]


@pytest.mark.parametrize("path", ["live", "rerun"])
async def test_an_address_an_earlier_deleted_account_held(
    world: World, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    """X held x and was deleted (no user row, never erased); S signed up
    with x later. X's rows — before S existed — stay raw."""
    s, gone, admin = world.sub("s"), world.sub("gone"), world.sub("admin")
    x = world.email("x")
    about_gone = await world.row(admin, "admin@example.com", f"user:{x}", at(1))
    by_gone = await world.row(gone, x, "workspace:w", at(2), action="member_add")
    await world.user(s, x, at(5))
    await world.row(s, x, "workspace:w", at(6), action="member_add")
    about_s = await world.row(admin, "admin@example.com", f"user:{x}", at(7))

    if path == "live":
        counts = await world.live_erasure(s)
        assert counts["audit_mentions_pseudonymized"] == 1
    else:
        await world.erased_before_the_fix(s)
        request = await world.request(s, x, at(30))
        assert await _run(world.engine, ["--request", str(request), "--apply", "--yes"]) == 0
        out = capsys.readouterr().out
        # the own row (x raw) and the admin's row about S
        assert _lines(out, f"pseudonymized audit mentions  {request}  2 row(s)")

    assert await world.values(about_gone, by_gone, about_s) == [
        ("admin@example.com", f"user:{x}"),
        (x, "workspace:w"),
        ("admin@example.com", f"user:{_p(x)}"),
    ]


@pytest.mark.parametrize("path", ["live", "rerun-no-user-row"])
async def test_every_older_address_is_pseudonymized(
    world: World, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    """x → y → z: rows naming each address while S held it are rewritten,
    the older ones recovered by HMAC from the table's values — also by the
    re-run, with no user row left to read anything from."""
    s, admin = world.sub("s"), world.sub("admin")
    x, y, z = world.email("x"), world.email("y"), world.email("z")
    await world.user(s, z, at(0))
    await world.row(s, x, "workspace:w", at(1), action="member_add")
    await world.sync(s, x, y, at(10))
    await world.sync(s, y, z, at(20))
    rows = [
        await world.row(admin, "admin@example.com", f"user:{address}", at(day))
        for address, day in ((x, 5), (y, 15), (z, 25))
    ]

    if path == "live":
        counts = await world.live_erasure(s)
        assert counts["audit_addresses_not_recovered"] == 0
    else:
        await world.erased_before_the_fix(s)
        request = await world.request(s, z, at(30))
        assert await _run(world.engine, ["--request", str(request), "--apply", "--yes"]) == 0
        assert "not recovered" not in capsys.readouterr().out

    assert await world.values(*rows) == [
        ("admin@example.com", f"user:{_p(address)}") for address in (x, y, z)
    ]


@pytest.mark.parametrize("other_holder", [False, True])
async def test_a_row_at_exactly_the_sync_time(world: World, other_holder: bool) -> None:
    """S held both addresses at the instant of its sync (closed holdings);
    an instant another account shares goes to the other account."""
    s, q, admin = world.sub("s"), world.sub("q"), world.sub("admin")
    x, y = world.email("x"), world.email("y")
    await world.user(s, y, at(0))
    await world.sync(s, x, y, at(10))
    if other_holder:
        await world.user(q, x, at(1))
        await world.sync(q, world.email("q"), x, at(10))
    before = await world.row(admin, "admin@example.com", f"user:{x}", at(9))
    old = await world.row(admin, "admin@example.com", f"user:{x}", at(10))
    new = await world.row(admin, "admin@example.com", f"user:{y}", at(10))

    counts = await world.live_erasure(s)

    assert counts["audit_mentions_kept"] == int(other_holder)
    assert await world.values(before, old, new) == [
        ("admin@example.com", f"user:{_p(x)}"),
        ("admin@example.com", f"user:{x}" if other_holder else f"user:{_p(x)}"),
        ("admin@example.com", f"user:{_p(y)}"),
    ]


async def test_an_account_re_created_under_the_same_sub_and_erased_twice(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """S held a and was erased (before the fix); G signed up with a and was
    deleted; S was re-created with b and erased again (live). The live
    erasure rewrites b only (its tenure starts at the new user row); the
    re-run rewrites a while the first S held it — never G's row in the gap
    — and is a no-op the second time."""
    s, gone, admin = world.sub("s"), world.sub("gone"), world.sub("admin")
    a, b = world.email("a"), world.email("b")
    await world.user(s, a, at(0))
    first_own = await world.row(s, a, "workspace:w", at(1), action="member_add")
    about_first = await world.row(admin, "admin@example.com", f"user:{a}", at(2))
    await world.erased_before_the_fix(s)
    first = await world.request(s, a, at(10))
    about_gone = await world.row(admin, "admin@example.com", f"user:{a}", at(13))
    await world.row(gone, a, "workspace:w", at(13), action="member_add")
    await world.user(s, b, at(15))
    second_own = await world.row(s, b, "workspace:w", at(16), action="member_add")
    about_second = await world.row(admin, "admin@example.com", f"user:{b}", at(17))

    counts = await world.live_erasure(s)
    assert (counts["audit_logs_pseudonymized"], counts["audit_mentions_pseudonymized"]) == (1, 1)
    assert await world.values(about_first, about_gone, about_second) == [
        ("admin@example.com", f"user:{a}"),
        ("admin@example.com", f"user:{a}"),
        ("admin@example.com", f"user:{_p(b)}"),
    ]
    async with _maker(world.engine)() as db:
        await db.execute(delete(User).where(User.user_id == s))
        await db.commit()
    await world.request(s, b, at(20))

    assert await _run(world.engine, ["--all", "--apply", "--yes"]) == 0
    out = capsys.readouterr().out
    assert _lines(out, f"pseudonymized audit mentions  {first}  2 row(s)")
    assert await world.values(first_own, about_first, about_gone, second_own, about_second) == [
        (_p(a), "workspace:w"),
        ("admin@example.com", f"user:{_p(a)}"),
        ("admin@example.com", f"user:{a}"),
        (_p(b), "workspace:w"),
        ("admin@example.com", f"user:{_p(b)}"),
    ]
    assert await _run(world.engine, ["--all", "--apply", "--yes"]) == 0
    out = capsys.readouterr().out
    assert not _lines(out, f"pseudonymized audit mentions  {first}")


async def test_an_hmac_that_matches_nothing_is_left_raw_and_counted(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sync row's HMACs were written under another AUDIT_HMAC_KEY: the
    older address cannot be recovered, its rows stay raw and the holding is
    counted — never the address. The current one is still rewritten from
    the last sync on (the last sync set it, whatever its HMAC)."""
    s, admin = world.sub("s"), world.sub("admin")
    x, y = world.email("x"), world.email("y")
    await world.user(s, y, at(0))
    await world.row(s, x, "workspace:w", at(1), action="member_add")
    await world.sync(s, x, y, at(10), key="a-rotated-key")
    about_x = await world.row(admin, "admin@example.com", f"user:{x}", at(5))
    about_y = await world.row(admin, "admin@example.com", f"user:{y}", at(15))

    await world.erased_before_the_fix(s)
    request = await world.request(s, y, at(30))
    assert await _run(world.engine, ["--request", str(request)]) == 0
    plan = capsys.readouterr().out
    assert await _run(world.engine, ["--request", str(request), "--apply", "--yes"]) == 0
    out = capsys.readouterr().out
    for text_ in (plan, out):
        assert _lines(text_, f"audit addresses  {request}  1 {cli.NOT_RECOVERED}")
        assert x not in text_ and y not in text_
    assert await world.values(about_x, about_y) == [
        ("admin@example.com", f"user:{x}"),
        ("admin@example.com", f"user:{_p(y)}"),
    ]
