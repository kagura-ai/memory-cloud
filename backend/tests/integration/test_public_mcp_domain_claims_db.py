"""The edge agent's claims list on PostgreSQL (#2993).

``GET /internal/public-mcp-domains/claims`` lists exactly the ``active`` and
``suspended`` rows, in byte order of host name then domain id, in the versioned
shape; a pending, released or deleted row is absent, and a name released and
claimed again shows only its new row. Without the gate no statement runs, the
read holds no lock, more rows than ``CLAIMS_LIMIT`` answer 503, and the
statement is answered from an index, not a sequential scan. Every test uses its own
host names and deletes its rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import internal_public_mcp_domains as routes
from config.settings import get_settings
from models.public_mcp_domain import PublicMcpDomain
from utils.datetime import to_utc_iso, utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

ROUTE = "/internal/public-mcp-domains/claims"
TOKEN = "reporter-" + "m" * 40
CLAIM_KEYS = {"hostname", "state", "domain_id", "endpoint_id", "check_seq", "created_at"}


@pytest_asyncio.fixture(loop_scope="session")
async def edge(async_engine, monkeypatch) -> AsyncIterator[SimpleNamespace]:
    settings = get_settings()
    for key, value in {
        "mcp_public_domains_enabled": True,
        "mcp_public_endpoints_enabled": True,
        "mcp_public_domains_routing_target": "edge.example.com",
        "mcp_public_domains_tls_reporter_token": TOKEN,
    }.items():
        monkeypatch.setattr(settings, key, value)
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    monkeypatch.setattr(routes, "get_db", sessions)
    app = FastAPI()
    app.include_router(routes.router)
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    ids: list = []
    try:
        yield SimpleNamespace(
            app=app, factory=factory, statements=statements, ids=ids, monkeypatch=monkeypatch
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)
        async with factory() as db:
            await db.execute(delete(PublicMcpDomain).where(PublicMcpDomain.id.in_(ids)))
            await db.commit()


def hostname(label: str) -> str:
    return f"{label}-{uuid4().hex[:10]}.example.org"


async def add(edge, name: str, state: str, *, seq: int = 0) -> PublicMcpDomain:
    at = utcnow().replace(microsecond=0) - timedelta(minutes=10)
    row = PublicMcpDomain(
        id=uuid4(),
        workspace_id=uuid4(),
        endpoint_id=uuid4(),
        hostname=name,
        state=state,
        verification_token="b" * 64,
        check_seq=seq,
        created_at=at,
        verified_at=at if state in {"active", "suspended"} else None,
        proof_ok_at=at if state in {"active", "suspended"} else None,
        serve_until=utcnow() + timedelta(hours=1) if state == "active" else None,
        suspended_reason="proof_lost" if state == "suspended" else None,
        suspended_at=at if state == "suspended" else None,
        released_at=at if state == "released" else None,
        cooldown_until=utcnow() + timedelta(days=1) if state == "released" else None,
    )
    async with edge.factory() as db:
        db.add(row)
        await db.commit()
    edge.ids.append(row.id)
    return row


async def claims(edge, token: str | None = TOKEN) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with AsyncClient(
        transport=ASGITransport(app=edge.app), base_url="http://api-blue:8080"
    ) as client:
        return await client.get(ROUTE, headers=headers)


def mine(body: dict, rows: list[PublicMcpDomain]) -> list[dict]:
    names = {row.hostname for row in rows}
    return [claim for claim in body["claims"] if claim["hostname"] in names]


async def test_only_active_and_suspended_rows_are_listed(edge):
    active = await add(edge, hostname("active"), "active", seq=4)
    suspended = await add(edge, hostname("suspended"), "suspended", seq=9)
    pending = await add(edge, hostname("pending"), "pending")
    released = await add(edge, hostname("released"), "released")
    deleted = await add(edge, hostname("deleted"), "active")
    async with edge.factory() as db:
        await db.execute(delete(PublicMcpDomain).where(PublicMcpDomain.id == deleted.id))
        await db.commit()
    r = await claims(edge)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"schema_version", "generated_at", "claims"}
    assert body["schema_version"] == 1
    listed = mine(body, [active, suspended, pending, released, deleted])
    assert listed == [
        {
            "hostname": active.hostname,
            "state": "active",
            "domain_id": str(active.id),
            "endpoint_id": str(active.endpoint_id),
            "check_seq": 4,
            "created_at": to_utc_iso(active.created_at),
        },
        {
            "hostname": suspended.hostname,
            "state": "suspended",
            "domain_id": str(suspended.id),
            "endpoint_id": str(suspended.endpoint_id),
            "check_seq": 9,
            "created_at": to_utc_iso(suspended.created_at),
        },
    ]
    assert all(set(claim) == CLAIM_KEYS for claim in body["claims"])


async def test_a_name_claimed_again_lists_only_its_new_row(edge):
    name = hostname("again")
    old = await add(edge, name, "released")
    new = await add(edge, name, "active")
    listed = mine((await claims(edge)).json(), [old, new])
    assert [claim["domain_id"] for claim in listed] == [str(new.id)]


async def test_the_list_is_in_byte_order_of_host_name_then_domain_id(edge):
    """Byte order whatever the collation: ``a-c`` (``-`` is 0x2d) before ``ab``."""
    tag = "o" + uuid4().hex[:10]
    rows = [
        await add(edge, f"{tag}ab.example.org", "active"),
        await add(edge, f"{tag}a-c.example.org", "suspended"),
        await add(edge, f"{tag}a.example.org", "active"),
    ]
    body = (await claims(edge)).json()
    listed = [claim["hostname"] for claim in mine(body, rows)]
    assert listed == [f"{tag}a-c.example.org", f"{tag}a.example.org", f"{tag}ab.example.org"]
    keys = [(claim["hostname"], claim["domain_id"]) for claim in body["claims"]]
    assert keys == sorted(keys)


@pytest.mark.parametrize(
    "switch",
    [
        ("mcp_public_domains_tls_reporter_token", ""),
        ("mcp_public_domains_enabled", False),
        ("mcp_public_endpoints_enabled", False),
    ],
)
async def test_not_configured_is_404_before_any_statement(edge, switch):
    await add(edge, hostname("gate"), "active")
    edge.monkeypatch.setattr(get_settings(), *switch)
    start = len(edge.statements)
    r = await claims(edge)
    assert (r.status_code, r.content) == (404, b"")
    assert edge.statements[start:] == []


@pytest.mark.parametrize("token", [None, "", "wrong-" + "w" * 40, TOKEN + "x"])
async def test_a_missing_or_wrong_credential_is_401_before_any_statement(edge, token):
    await add(edge, hostname("gate"), "active")
    start = len(edge.statements)
    r = await claims(edge, token)
    assert (r.status_code, r.content) == (401, b"")
    assert edge.statements[start:] == []


async def test_the_read_is_one_statement_without_a_lock(edge):
    await add(edge, hostname("lock"), "active")
    start = len(edge.statements)
    assert (await claims(edge)).status_code == 200
    seen = [s for s in edge.statements[start:] if "public_mcp_domains" in s]
    assert len(seen) == 1
    assert "FOR UPDATE" not in seen[0].upper() and "FOR SHARE" not in seen[0].upper()
    assert "state IN ('active', 'suspended')" in seen[0]


async def test_more_rows_than_the_limit_is_503(edge):
    await add(edge, hostname("limit"), "active")
    await add(edge, hostname("limit"), "suspended")
    held = len((await claims(edge)).json()["claims"])
    edge.monkeypatch.setattr(routes, "CLAIMS_LIMIT", held)
    assert len((await claims(edge)).json()["claims"]) == held
    edge.monkeypatch.setattr(routes, "CLAIMS_LIMIT", held - 1)
    r = await claims(edge)
    assert (r.status_code, r.content) == (503, b"")


async def test_the_statement_is_answered_from_an_index(edge):
    """With sequential scans off the plan reads an index of the table (which one
    is the planner's choice), never a sequential scan of it."""
    await add(edge, hostname("index"), "active")
    sql = str(
        routes.claims_statement().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    async with edge.factory() as db:
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(row[0] for row in (await db.execute(text("EXPLAIN " + sql))).all())
        await db.rollback()
    assert "Seq Scan on public_mcp_domains" not in plan, plan
    assert "Index" in plan and "public_mcp_domains" in plan, plan
