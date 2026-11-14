"""A verified custom domain end to end through ``api.main.app`` (#2809).

Real rows on the test database; nothing on the request path is patched: the
Host guard, the public transport's alias loader and the certificate
permission endpoint run against the database. The owner acts over REST
(caller injected through ``get_user_from_api_key_or_session``), the resolver
is a scripted fake, and the per-minute buckets run on an in-memory counter
that records its keys.

- every non-serving state answers the same 404 bytes on ``https://<name>/mcp``;
  an active alias serves the same endpoint as its fixed address;
- both entries share the buckets, the budget row, the usage records and the
  sessions;
- the permission endpoint and the request path agree at every moment;
- unpublishing releases the domain in the same transaction.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Context, User, Workspace, WorkspaceMember
from models.integration_entitlement import WorkspaceIntegrationLicense, WorkspaceIntegrationState
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_domain import DOMAIN_SERVICE_ID, PublicMcpDomain
from models.resource import Resource
from services import public_mcp_domain_dns as domain_dns
from services import public_mcp_domains as service
from services.public_mcp_domain_dns import DomainCheck
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
}
PERMISSION = "/internal/public-mcp-domains/tls-permission"


class Dns:
    def __init__(self) -> None:
        self.txt: dict[str, set[str]] = {}

    def publish(self, hostname: str, token: str) -> None:
        self.txt.setdefault(hostname, set()).add(token)

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck:
        return DomainCheck("ok" if token in self.txt.get(hostname, set()) else "missing", "ok")


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    owner: httpx.AsyncClient
    dns: Dns
    tag: str
    user: str
    ws: UUID
    slug: str
    endpoint: UUID
    public_id: str
    buckets: list[str] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)

    def name(self) -> str:
        return f"help-{uuid4().hex[:10]}.example.org"

    @property
    def domains(self) -> str:
        return f"/api/v1/resources/{self.slug}/public-mcp/domains"

    async def attach(self, hostname: str) -> dict[str, Any]:
        r = await self.owner.post(self.domains, json={"hostname": hostname})
        assert r.status_code == 201, r.text
        return r.json()

    async def activate(self, hostname: str | None = None) -> dict[str, Any]:
        row = await self.attach(hostname or self.name())
        self.dns.publish(row["hostname"], row["verification"]["record_value"].split("=", 1)[1])
        r = await self.owner.post(f"{self.domains}/{row['domain_id']}/verify")
        assert r.status_code == 200, r.text
        assert r.json()["serving"] is True
        return r.json()

    async def detach(self, domain_id: str) -> None:
        r = await self.owner.delete(f"{self.domains}/{domain_id}")
        assert r.status_code == 204, r.text

    async def sql(self, statement: str, **params: Any) -> None:
        async with self.factory() as s:
            await s.execute(text(statement), params)
            await s.commit()


def _client(base_url: str) -> httpx.AsyncClient:
    from api.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def _post(
    host: str, path: str, body: dict, headers: dict[str, str] | None = None
) -> httpx.Response:
    async with _client(f"http://{host}") as c:
        return await c.post(path, json=body, headers=headers or {})


async def _alias(host: str, body: dict = INIT, session: str | None = None) -> httpx.Response:
    return await _post(host, "/mcp", body, {"mcp-session-id": session} if session else None)


async def _fixed(w: World, body: dict = INIT, session: str | None = None) -> httpx.Response:
    return await _post(
        "testserver",
        f"/mcp/public/{w.public_id}",
        body,
        {"mcp-session-id": session} if session else None,
    )


async def _permission(name: str) -> int:
    async with _client("http://api-blue:8080") as c:
        r = await c.get(PERMISSION, params={"domain": name})
    assert r.content == b""
    return r.status_code


def _call(tool: str = "get_context_info", request_id: int = 2) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }


async def _not_published_bytes() -> bytes:
    r = await _post("testserver", f"/mcp/public/pmcp_{'Z' * 22}", INIT)
    assert r.status_code == 404
    return r.content


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, monkeypatch) -> AsyncIterator[World]:
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    user, slug = f"alias-owner-{tag}", f"help-{tag}"
    now = utcnow()
    async with factory() as s:
        s.add(User(email=f"{user}@example.org", user_id=user, name=user, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"alias-{tag}",
            plan_name="promax",
            owner_user_id=user,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add(ws)
        await s.flush()
        resource = Resource(workspace_id=ws.id, resource_id=slug, created_by=user)
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=slug,
            created_by=user,
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=user, role=WorkspaceRole.OWNER),
                resource,
                ctx,
            ]
        )
        await s.flush()
        endpoint = PublicMcpEndpoint(
            workspace_id=ws.id,
            context_id=ctx.id,
            resource_pk=resource.id,
            resource_id=slug,
            public_name="Help center",
            public_description="Answers.",
            created_by=user,
        )
        s.add(endpoint)
        s.add(
            WorkspaceIntegrationState(
                workspace_id=ws.id,
                revision=1,
                event_id=f"evt-{uuid4().hex}",
                source="external_billing",
                source_ref="fixture",
                max_valid_from=now - timedelta(days=1),
                max_valid_until=now + timedelta(days=30),
                max_evidence_ref="fixture-max",
            )
        )
        await s.flush()
        s.add(
            WorkspaceIntegrationLicense(
                workspace_id=ws.id,
                service_id=DOMAIN_SERVICE_ID,
                valid_from=now - timedelta(days=1),
                valid_until=now + timedelta(days=30),
                evidence_ref="fixture-license",
            )
        )
        await s.commit()
        ws_id, endpoint_id, public_id = ws.id, endpoint.id, endpoint.public_id

    settings = get_settings()
    for name, value in {
        "mcp_public_domains_enabled": True,
        "mcp_public_endpoints_enabled": True,
        "mcp_public_domains_authority": "entitlement",
        "mcp_public_domains_routing_target": "edge.example.net",
        "mcp_public_domains_reserved_suffixes": "",
        "frontend_url": "https://app.example.com",
    }.items():
        monkeypatch.setattr(settings, name, value)
    dns = Dns()
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: dns)
    monkeypatch.setattr(service, "incrby_counter", AsyncMock(return_value=1))

    async def _db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def _user(request: Request) -> dict[str, Any]:
        if request.headers.get("x-test-anonymous"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {
            "user_id": user,
            "email": f"{user}@example.org",
            "role": "user",
            "current_workspace_id": ws_id,
        }

    buckets: list[str] = []
    counts: dict[str, int] = {}

    async def incr(key: str, amount: int, ttl: int | None = None) -> int:
        buckets.append(key)
        counts[key] = counts.get(key, 0) + amount
        return counts[key]

    calls: list[Any] = []
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_user_from_api_key_or_session] = _user
    try:
        with (
            patch("mcp_server.public.limits.incrby_counter", new=incr),
            patch("mcp_server.public.dispatch.record_public_call", new=calls.append),
        ):
            async with _client("http://testserver") as owner:
                yield World(
                    factory=factory,
                    owner=owner,
                    dns=dns,
                    tag=tag,
                    user=user,
                    ws=ws_id,
                    slug=slug,
                    endpoint=endpoint_id,
                    public_id=public_id,
                    buckets=buckets,
                    calls=calls,
                )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)
        async with factory() as s:
            await s.execute(delete(PublicMcpDomain).where(PublicMcpDomain.workspace_id == ws_id))
            await s.execute(delete(Workspace).where(Workspace.id == ws_id))
            await s.execute(delete(User).where(User.user_id == user))
            await s.commit()


# ----------------------------------------------------------------------------
# What each Host gets on /mcp
# ----------------------------------------------------------------------------


async def test_every_non_serving_state_is_the_same_404(world: World) -> None:
    w = world
    reference = await _not_published_bytes()

    never_seen = w.name()
    pending = (await w.attach(w.name()))["hostname"]
    answers = {"never_seen": await _alias(never_seen), "pending": await _alias(pending)}
    await w.sql("DELETE FROM public_mcp_domains WHERE hostname = :h", h=pending)

    suspended = await w.activate()
    await w.sql(
        "UPDATE public_mcp_domains SET state = 'suspended', suspended_reason = 'proof_lost', "
        "suspended_at = timezone('UTC', statement_timestamp()) WHERE id = :id",
        id=suspended["domain_id"],
    )
    answers["suspended"] = await _alias(suspended["hostname"])
    await w.detach(suspended["domain_id"])
    answers["released_cooling_down"] = await _alias(suspended["hostname"])

    for state, r in answers.items():
        assert r.status_code == 404, state
        assert r.content == reference, state
        assert "set-cookie" not in r.headers and "www-authenticate" not in r.headers


async def test_an_active_alias_serves_its_endpoint(world: World) -> None:
    w = world
    domain = await w.activate()
    r = await _alias(domain["hostname"])
    assert r.status_code == 200, r.text
    assert "Help center" in r.text
    session = r.headers["mcp-session-id"]
    r = await _alias(domain["hostname"], _call(), session=session)
    assert r.status_code == 200
    info = json.loads(r.json()["result"]["content"][0]["text"])
    assert info["endpoint"]["public_id"] == w.public_id
    # Upper case and a trailing dot in the Host are the same name.
    r = await _alias(domain["hostname"].upper() + ".")
    assert r.status_code == 200
    await w.detach(domain["domain_id"])


async def test_a_reserved_name_with_an_active_row_is_404_before_any_job(
    world: World, monkeypatch
) -> None:
    w = world
    domain = await w.activate()
    reference = await _not_published_bytes()
    zone = domain["hostname"].split(".", 1)[1]
    monkeypatch.setattr(get_settings(), "mcp_public_domains_reserved_suffixes", f"x.org,{zone}")
    r = await _alias(domain["hostname"])
    assert (r.status_code, r.content) == (404, reference)
    assert await _permission(domain["hostname"]) == 404
    async with w.factory() as s:
        row = await s.get(PublicMcpDomain, UUID(domain["domain_id"]))
    assert row.state == "active"  # the row is marked later, by the job
    monkeypatch.setattr(get_settings(), "mcp_public_domains_reserved_suffixes", "")
    assert (await _alias(domain["hostname"])).status_code == 200
    await w.detach(domain["domain_id"])


async def test_the_fixed_address_of_another_endpoint_is_404_on_an_alias_host(
    world: World,
) -> None:
    w = world
    domain = await w.activate()
    for path in (
        f"/mcp/public/{w.public_id}",
        f"/mcp/public/pmcp_{'B' * 22}",
        "/api/v1/system/info",
    ):
        r = await _post(domain["hostname"], path, INIT)
        assert r.status_code == 404, path
    await w.detach(domain["domain_id"])


async def test_plan_pause_is_the_same_503_on_both_entries(world: World) -> None:
    w = world
    domain = await w.activate()
    await w.sql("UPDATE workspaces SET plan_name = 'free' WHERE id = :id", id=w.ws)
    try:
        fixed, alias = await _fixed(w), await _alias(domain["hostname"])
    finally:
        await w.sql("UPDATE workspaces SET plan_name = 'promax' WHERE id = :id", id=w.ws)
    assert fixed.status_code == alias.status_code == 503
    assert fixed.content == alias.content
    await w.detach(domain["domain_id"])


# ----------------------------------------------------------------------------
# Same endpoint, same counters
# ----------------------------------------------------------------------------


async def test_both_entries_share_buckets_budget_records_and_sessions(
    world: World, monkeypatch
) -> None:
    w = world
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_public_usage_budget_enabled", True)
    monkeypatch.setattr(settings, "mcp_public_usage_budget_calls", 100)
    domain = await w.activate()
    host = domain["hostname"]

    fixed_session = (await _fixed(w)).headers["mcp-session-id"]
    alias_session = (await _alias(host)).headers["mcp-session-id"]
    w.calls.clear()
    # A session opened on one entry works on the other.
    plan = [
        (True, 2, alias_session),
        (False, 2, fixed_session),
        (True, 3, fixed_session),
        (False, 3, alias_session),
    ]
    answers, keys = [], []
    for fixed, request_id, session in plan:
        w.buckets.clear()
        if fixed:
            r = await _fixed(w, _call(request_id=request_id), session=session)
        else:
            r = await _alias(host, _call(request_id=request_id), session=session)
        assert r.status_code == 200, r.text
        assert "isError" not in r.json()["result"]
        answers.append(r.json()["result"]["content"][0]["text"])
        keys.append(sorted(set(w.buckets)))
    assert answers[0] == answers[1] == answers[2] == answers[3]
    # One session, either entry: the same buckets (calls 0 and 3, calls 1 and 2).
    assert keys[0] and keys[0] == keys[3] and keys[1] == keys[2]
    shared = [[k for k in ks if ":sess:" not in k] for ks in keys]
    assert shared[0] and shared[0] == shared[1] == shared[2] == shared[3]
    assert all(str(w.endpoint) in k for k in keys[0])

    async with w.factory() as s:
        rows = (
            await s.execute(
                text("SELECT calls_used FROM public_usage_budgets WHERE workspace_id = :ws"),
                {"ws": w.ws},
            )
        ).all()
    assert len(rows) == 1 and rows[0][0] >= 4

    assert len(w.calls) == 4

    def comparable(record: Any) -> dict[str, Any]:
        values = dict(vars(record))
        for key in ("started_at", "duration_ms", "session_id"):
            values.pop(key, None)
        return values

    assert all(comparable(c) == comparable(w.calls[0]) for c in w.calls)
    assert w.calls[0].endpoint_id == w.endpoint and w.calls[0].public_id == w.public_id
    await w.detach(domain["domain_id"])


# ----------------------------------------------------------------------------
# Permission and request path agree
# ----------------------------------------------------------------------------


async def _agree(w: World, host: str, expected: int) -> None:
    permission = await _permission(host)
    served = (await _alias(host)).status_code
    assert (permission, served) == (expected, 200 if expected == 200 else 404)


async def test_permission_and_request_path_agree(world: World, monkeypatch) -> None:
    w = world
    # Active, then detached.
    domain = await w.activate()
    await _agree(w, domain["hostname"], 200)
    assert await _permission(w.name()) == 404
    await w.detach(domain["domain_id"])
    await _agree(w, domain["hostname"], 404)

    # Suspended.
    domain = await w.activate()
    await w.sql(
        "UPDATE public_mcp_domains SET state = 'suspended', suspended_reason = 'right_ended', "
        "suspended_at = timezone('UTC', statement_timestamp()) WHERE id = :id",
        id=domain["domain_id"],
    )
    await _agree(w, domain["hostname"], 404)
    await w.detach(domain["domain_id"])

    # Lease passed, under both authorities.
    domain = await w.activate()
    await w.sql(
        "UPDATE public_mcp_domains SET serve_until = timezone('UTC', statement_timestamp()) "
        "- interval '1 second' WHERE id = :id",
        id=domain["domain_id"],
    )
    for authority in ("entitlement", "operator"):
        monkeypatch.setattr(get_settings(), "mcp_public_domains_authority", authority)
        await _agree(w, domain["hostname"], 404)
    monkeypatch.setattr(get_settings(), "mcp_public_domains_authority", "entitlement")
    await w.detach(domain["domain_id"])

    # Workspace soft delete, then restore.
    domain = await w.activate()
    await w.sql("UPDATE workspaces SET deleted_at = now() WHERE id = :id", id=w.ws)
    try:
        await _agree(w, domain["hostname"], 404)
    finally:
        await w.sql("UPDATE workspaces SET deleted_at = NULL WHERE id = :id", id=w.ws)
    await _agree(w, domain["hostname"], 200)

    # The public switch off.
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", False)
    await _agree(w, domain["hostname"], 404)
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    await _agree(w, domain["hostname"], 200)
    await w.detach(domain["domain_id"])


async def test_a_session_opened_before_detach_is_refused(world: World) -> None:
    w = world
    domain = await w.activate()
    session = (await _alias(domain["hostname"])).headers["mcp-session-id"]
    await w.detach(domain["domain_id"])
    r = await _alias(domain["hostname"], _call(), session=session)
    assert r.status_code == 404
    assert r.content == await _not_published_bytes()


# ----------------------------------------------------------------------------
# Unpublish through the owner route
# ----------------------------------------------------------------------------


async def test_unpublish_releases_the_domain_and_a_republish_stays_404(world: World) -> None:
    w = world
    domain = await w.activate()
    r = await w.owner.delete(f"/api/v1/resources/{w.slug}/public-mcp")
    assert r.status_code == 204, r.text
    async with w.factory() as s:
        row = await s.get(PublicMcpDomain, UUID(domain["domain_id"]))
        audit = (
            await s.execute(
                text("SELECT action, user_id FROM audit_logs WHERE resource = :r ORDER BY id DESC"),
                {"r": f"public_mcp_domain:{domain['domain_id']}"},
            )
        ).first()
    assert row.state == "released" and row.cooldown_until is not None
    assert tuple(audit) == ("public_mcp_domain.released", w.user)
    await _agree(w, domain["hostname"], 404)

    r = await w.owner.post(
        f"/api/v1/resources/{w.slug}/public-mcp", json={"public_name": "Help center"}
    )
    assert r.status_code == 201, r.text
    w.public_id = r.json()["public_id"]
    async with w.factory() as s:
        w.endpoint = await s.scalar(
            select(PublicMcpEndpoint.id).where(PublicMcpEndpoint.public_id == w.public_id)
        )
    await _agree(w, domain["hostname"], 404)
    assert (await _fixed(w)).status_code == 200

    # A claim (pending) is deleted by the unpublish.
    claim = await w.attach(w.name())
    r = await w.owner.delete(f"/api/v1/resources/{w.slug}/public-mcp")
    assert r.status_code == 204
    async with w.factory() as s:
        assert await s.get(PublicMcpDomain, UUID(claim["domain_id"])) is None
