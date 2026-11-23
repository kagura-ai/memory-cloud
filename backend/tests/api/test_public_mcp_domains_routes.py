"""Owner routes of custom domains: authority, limits and errors (#2809).

``api.main.app`` with ``get_db`` on the test database and the caller injected
through ``get_user_from_api_key_or_session`` (the real
``require_workspace_owner`` then checks the membership row). The resolver is
a scripted fake that counts its calls; the two Redis counters run on an
in-memory counter whose failure and TTL the tests choose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Context, User, Workspace, WorkspaceMember
from models.integration_entitlement import WorkspaceIntegrationLicense, WorkspaceIntegrationState
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_domain import (
    ACTIVATIONS_PER_DAY,
    DOMAIN_SERVICE_ID,
    VERIFY_LIMIT_PER_HOUR,
    PublicMcpDomain,
)
from models.resource import Resource
from services import public_mcp_domain_dns as domain_dns
from services import public_mcp_domains as service
from services.public_mcp_domain_dns import DomainCheck
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


class Dns:
    def __init__(self) -> None:
        self.txt: dict[str, set[str]] = {}
        self.calls: list[str] = []

    def publish(self, hostname: str, token: str) -> None:
        self.txt.setdefault(hostname, set()).add(token)

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck:
        self.calls.append(hostname)
        return DomainCheck("ok" if token in self.txt.get(hostname, set()) else "missing", "ok")


class Counters:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.down = False
        self.ttl = 1234

    async def incr(self, key: str, amount: int, ttl: int | None = None) -> int:
        if self.down:
            raise ConnectionError("redis down")
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    def get(self, kind: str, ws: UUID) -> int:
        return self.values.get(f"public_mcp_domain_{kind}:{ws}", 0)


class _RedisClient:
    def __init__(self, counters: Counters) -> None:
        self.counters = counters

    async def ttl(self, key: str) -> int:
        return self.counters.ttl


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    client: httpx.AsyncClient
    dns: Dns
    counters: Counters
    tag: str
    users: dict[str, str] = field(default_factory=dict)
    ws: dict[str, UUID] = field(default_factory=dict)
    slug: dict[str, str] = field(default_factory=dict)

    def name(self) -> str:
        return f"docs-{uuid4().hex[:10]}.example.org"

    def path(self, who: str, suffix: str = "") -> str:
        return f"/api/v1/resources/{self.slug[who]}/public-mcp/domains{suffix}"

    async def post(self, who: str, suffix: str = "", **kw: Any) -> httpx.Response:
        return await self.client.post(self.path(who, suffix), headers={"x-test-user": who}, **kw)

    async def attach(self, who: str, hostname: str, as_user: str | None = None) -> httpx.Response:
        return await self.client.post(
            self.path(who), json={"hostname": hostname}, headers={"x-test-user": as_user or who}
        )

    async def verify(self, who: str, domain_id: str, as_user: str | None = None) -> httpx.Response:
        return await self.client.post(
            self.path(who, f"/{domain_id}/verify"), headers={"x-test-user": as_user or who}
        )

    async def detach(self, who: str, domain_id: str, as_user: str | None = None) -> httpx.Response:
        return await self.client.delete(
            self.path(who, f"/{domain_id}"), headers={"x-test-user": as_user or who}
        )

    async def sql(self, statement: str, **params: Any) -> None:
        async with self.factory() as s:
            await s.execute(text(statement), params)
            await s.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, monkeypatch) -> AsyncIterator[World]:
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    users = {"a": f"rt-a-{tag}", "a_member": f"rt-am-{tag}", "b": f"rt-b-{tag}"}
    home = {"a": "a", "a_member": "a", "b": "b"}
    ws: dict[str, UUID] = {}
    slug: dict[str, str] = {}
    now = utcnow()
    async with factory() as s:
        for uid in users.values():
            s.add(User(email=f"{uid}@example.org", user_id=uid, name=uid, role="user"))
        await s.flush()
        for key in ("a", "b"):
            row = Workspace(
                id=uuid4(),
                name=f"rt-{key}-{tag}",
                plan_name="promax",
                owner_user_id=users[key],
                daily_api_limit=50000,
                weekly_api_limit=250000,
            )
            s.add(row)
            await s.flush()
            ws[key], slug[key] = row.id, f"docs-{key}-{tag}"
            s.add(
                WorkspaceMember(workspace_id=row.id, user_id=users[key], role=WorkspaceRole.OWNER)
            )
            resource = Resource(workspace_id=row.id, resource_id=slug[key], created_by=users[key])
            ctx = Context(
                id=uuid4(),
                workspace_id=row.id,
                name=slug[key],
                created_by=users[key],
                is_private=False,
                is_public=True,
                resource_id=slug[key],
            )
            s.add_all([resource, ctx])
            await s.flush()
            s.add(
                PublicMcpEndpoint(
                    workspace_id=row.id,
                    context_id=ctx.id,
                    resource_pk=resource.id,
                    resource_id=slug[key],
                    public_name=f"Docs {key}",
                    created_by=users[key],
                )
            )
            s.add(
                WorkspaceIntegrationState(
                    workspace_id=row.id,
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
                    workspace_id=row.id,
                    service_id=DOMAIN_SERVICE_ID,
                    valid_from=now - timedelta(days=1),
                    valid_until=now + timedelta(days=30),
                    evidence_ref="fixture-license",
                )
            )
        s.add(
            WorkspaceMember(
                workspace_id=ws["a"], user_id=users["a_member"], role=WorkspaceRole.OWNER
            )
        )
        await s.commit()

    settings = get_settings()
    for name, value in {
        "mcp_public_domains_enabled": True,
        "mcp_public_endpoints_enabled": True,
        "mcp_public_domains_authority": "entitlement",
        "mcp_public_domains_routing_target": "edge.example.net",
        "mcp_public_domains_max_per_workspace": 3,
    }.items():
        monkeypatch.setattr(settings, name, value)
    dns, counters = Dns(), Counters()
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: dns)
    monkeypatch.setattr(service, "incrby_counter", counters.incr)
    monkeypatch.setattr(service, "get_redis_client", lambda: _RedisClient(counters))

    async def _db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def _user(request: Request) -> dict[str, Any]:
        who = request.headers.get("x-test-user")
        if not who:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {
            "user_id": users[who],
            "email": f"{users[who]}@example.org",
            "role": "user",
            "current_workspace_id": ws[home[who]],
        }

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_user_from_api_key_or_session] = _user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield World(factory, client, dns, counters, tag, users, ws, slug)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)
        async with factory() as s:
            ids = list(ws.values())
            await s.execute(delete(PublicMcpDomain).where(PublicMcpDomain.workspace_id.in_(ids)))
            await s.execute(delete(Workspace).where(Workspace.id.in_(ids)))
            await s.execute(delete(User).where(User.user_id.in_(list(users.values()))))
            await s.commit()


async def _active(w: World, who: str, hostname: str | None = None) -> dict[str, Any]:
    hostname = hostname or w.name()
    r = await w.attach(who, hostname)
    assert r.status_code == 201, r.text
    body = r.json()
    w.dns.publish(hostname, body["verification"]["record_value"].split("=", 1)[1])
    r = await w.verify(who, body["domain_id"])
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "active"
    return r.json()


# ----------------------------------------------------------------------------
# Shape
# ----------------------------------------------------------------------------


async def test_add_list_verify_remove(world: World) -> None:
    w = world
    hostname = w.name()
    r = await w.attach("a", hostname.upper())
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {
        "domain_id",
        "hostname",
        "url",
        "state",
        "suspended_reason",
        "serving",
        "verification",
        "routing",
        "last_check",
        "proof_ok_at",
        "proof_missing_checks",
        "serve_until",
        "created_at",
        "verified_at",
    }
    assert body["hostname"] == hostname
    assert body["url"] == f"https://{hostname}/mcp"
    assert (body["state"], body["serving"], body["suspended_reason"]) == ("pending", False, None)
    assert body["verification"]["record_type"] == "TXT"
    assert body["verification"]["record_name"] == f"_kagura-public-mcp.{hostname}"
    assert body["verification"]["record_value"].startswith("kagura-public-mcp-verification=")
    assert body["routing"] == {"target": "edge.example.net", "status": None}
    assert body["last_check"] == {"result": None, "checked_at": None}
    assert body["created_at"].endswith("Z")

    listed = await w.client.get(w.path("a"), headers={"x-test-user": "a"})
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert [d["domain_id"] for d in listed.json()["domains"]] == [body["domain_id"]]

    token = body["verification"]["record_value"].split("=", 1)[1]
    w.dns.publish(hostname, token)
    r = await w.verify("a", body["domain_id"])
    assert r.status_code == 200, r.text
    active = r.json()
    assert (active["state"], active["serving"]) == ("active", True)
    assert active["serve_until"].endswith("Z") and active["verified_at"].endswith("Z")
    assert active["last_check"]["result"] == "ok"
    assert active["routing"]["status"] == "ok"

    r = await w.detach("a", body["domain_id"])
    assert r.status_code == 204
    assert r.headers["cache-control"] == "no-store"
    listed = await w.client.get(w.path("a"), headers={"x-test-user": "a"})
    assert listed.json()["domains"] == []


async def test_refusals_carry_no_store_and_the_typed_code(world: World) -> None:
    w = world
    for bad in ("localhost", "x.onion", "edge.example.net", "a b.example.org", "*.example.org"):
        r = await w.attach("a", bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"] == "public_mcp_domain_hostname_not_allowed"
        assert r.json()["message"] == "This host name cannot be used as a custom domain."
        assert r.headers["cache-control"] == "no-store"
    r = await w.client.post(w.path("a"), json={"hostname": "x" * 256})
    assert r.status_code in (401, 422)
    r = await w.verify("a", str(uuid4()))
    assert (r.status_code, r.json()["error"]) == (404, "RES-001")
    assert r.headers["cache-control"] == "no-store"


async def test_switch_off_every_route_is_404(world: World, monkeypatch) -> None:
    w = world
    monkeypatch.setattr(get_settings(), "mcp_public_domains_enabled", False)
    domain_id = str(uuid4())
    answers = [
        await w.client.get(w.path("a"), headers={"x-test-user": "a"}),
        await w.attach("a", w.name()),
        await w.verify("a", domain_id),
        await w.detach("a", domain_id),
        await w.client.get(w.path("a")),  # not even authenticated
    ]
    for r in answers:
        assert (r.status_code, r.json()["error"]) == (404, "RES-001")
        assert r.headers["cache-control"] == "no-store"
    assert w.dns.calls == []


# ----------------------------------------------------------------------------
# Authority
# ----------------------------------------------------------------------------


async def test_owner_member_who_is_not_the_canonical_owner(world: World) -> None:
    w = world
    r = await w.attach("a", w.name(), as_user="a_member")
    assert (r.status_code, r.json()["error"]) == (403, "public_mcp_domain_owner_required")

    created = (await w.attach("a", w.name())).json()
    r = await w.verify("a", created["domain_id"], as_user="a_member")
    assert (r.status_code, r.json()["error"]) == (403, "public_mcp_domain_owner_required")
    assert w.counters.get("verify", w.ws["a"]) == 0
    assert w.dns.calls == []

    listed = await w.client.get(w.path("a"), headers={"x-test-user": "a_member"})
    assert listed.status_code == 200 and len(listed.json()["domains"]) == 1
    r = await w.detach("a", created["domain_id"], as_user="a_member")
    assert r.status_code == 204


async def test_a_domain_of_another_workspace_or_endpoint_is_404_without_the_counter(
    world: World,
) -> None:
    w = world
    theirs = (await w.attach("b", w.name())).json()
    r = await w.verify("a", theirs["domain_id"])
    assert (r.status_code, r.json()["error"]) == (404, "RES-001")
    r = await w.detach("a", theirs["domain_id"])
    assert r.status_code == 404
    assert w.counters.get("verify", w.ws["a"]) == 0
    assert w.dns.calls == []
    # Another endpoint of the same workspace: a second publication.
    async with w.factory() as s:
        slug = f"other-{w.tag}"
        ctx = Context(
            id=uuid4(),
            workspace_id=w.ws["a"],
            name=slug,
            created_by=w.users["a"],
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
        resource = Resource(workspace_id=w.ws["a"], resource_id=slug, created_by=w.users["a"])
        s.add_all([ctx, resource])
        await s.flush()
        s.add(
            PublicMcpEndpoint(
                workspace_id=w.ws["a"],
                context_id=ctx.id,
                resource_pk=resource.id,
                resource_id=slug,
                public_name="Other",
                created_by=w.users["a"],
            )
        )
        await s.commit()
    w.slug["other"] = slug
    mine = (await w.attach("a", w.name())).json()
    r = await w.client.post(
        f"/api/v1/resources/{slug}/public-mcp/domains/{mine['domain_id']}/verify",
        headers={"x-test-user": "a"},
    )
    assert (r.status_code, r.json()["error"]) == (404, "RES-001")
    assert w.counters.get("verify", w.ws["a"]) == 0
    # Attach to another workspace's resource: no such publication here.
    r = await w.client.post(
        f"/api/v1/resources/{w.slug['b']}/public-mcp/domains",
        json={"hostname": w.name()},
        headers={"x-test-user": "a"},
    )
    assert (r.status_code, r.json()["error"]) == (404, "RES-001")


# ----------------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------------


async def test_verify_limit_per_hour(world: World) -> None:
    w = world
    created = (await w.attach("a", w.name())).json()
    for _ in range(VERIFY_LIMIT_PER_HOUR):
        r = await w.verify("a", created["domain_id"])
        assert r.status_code == 200, r.text
    calls = len(w.dns.calls)
    r = await w.verify("a", created["domain_id"])
    assert r.status_code == 429
    assert r.json()["error"] == "RATE-001"
    assert r.json()["details"]["limit"] == "verify_per_hour"
    assert r.headers["retry-after"] == "1234"
    assert r.headers["cache-control"] == "no-store"
    assert len(w.dns.calls) == calls  # no lookup over the limit


async def test_retry_after_falls_back_to_the_window(world: World) -> None:
    w = world
    created = (await w.attach("a", w.name())).json()
    w.counters.values[f"public_mcp_domain_verify:{w.ws['a']}"] = VERIFY_LIMIT_PER_HOUR
    w.counters.ttl = -1
    r = await w.verify("a", created["domain_id"])
    assert r.status_code == 429
    assert r.headers["retry-after"] == "3600"


async def test_activation_limit_per_day_and_reinstatement_is_not_counted(world: World) -> None:
    w = world
    theirs = await _active(w, "b")  # the name is held by B
    mine = (await w.attach("a", theirs["hostname"])).json()
    w.dns.publish(theirs["hostname"], mine["verification"]["record_value"].split("=", 1)[1])
    for _ in range(ACTIVATIONS_PER_DAY):
        r = await w.verify("a", mine["domain_id"])
        assert (r.status_code, r.json()["error"]) == (409, "public_mcp_domain_held")
    r = await w.verify("a", mine["domain_id"])
    assert r.status_code == 429
    assert r.json()["details"]["limit"] == "activations_per_day"
    assert r.headers["retry-after"] == "1234"
    assert w.counters.get("activate", w.ws["a"]) == ACTIVATIONS_PER_DAY + 1

    # B: a suspended row reinstated by verify is not an activation.
    before = w.counters.get("activate", w.ws["b"])
    await w.sql(
        "UPDATE public_mcp_domains SET state = 'suspended', suspended_reason = 'proof_lost', "
        "suspended_at = timezone('UTC', statement_timestamp()) WHERE id = :id",
        id=theirs["domain_id"],
    )
    r = await w.verify("b", theirs["domain_id"])
    assert (r.status_code, r.json()["state"]) == (200, "active")
    assert w.counters.get("activate", w.ws["b"]) == before


async def test_redis_down_is_503(world: World) -> None:
    w = world
    created = (await w.attach("a", w.name())).json()
    w.counters.down = True
    r = await w.verify("a", created["domain_id"])
    assert r.status_code == 503
    assert r.json()["error"] == "public_mcp_domain_verification_unavailable"
    assert w.dns.calls == []


async def test_activation_counter_down_is_503_and_the_row_stays_pending(
    world: World, monkeypatch
) -> None:
    w = world
    hostname = w.name()
    created = (await w.attach("a", hostname)).json()
    w.dns.publish(hostname, created["verification"]["record_value"].split("=", 1)[1])

    async def verify_only(key: str, amount: int, ttl: int | None = None) -> int:
        if "activate" in key:
            raise ConnectionError("redis down")
        return 1

    monkeypatch.setattr(service, "incrby_counter", verify_only)
    r = await w.verify("a", created["domain_id"])
    assert (r.status_code, r.json()["error"]) == (
        503,
        "public_mcp_domain_verification_unavailable",
    )
    listed = await w.client.get(w.path("a"), headers={"x-test-user": "a"})
    assert listed.json()["domains"][0]["state"] == "pending"


async def test_held_and_cooldown_never_before_a_valid_proof(world: World) -> None:
    w = world
    theirs = await _active(w, "b")
    mine = (await w.attach("a", theirs["hostname"])).json()
    assert mine["state"] == "pending" and mine["serving"] is False
    r = await w.verify("a", mine["domain_id"])
    assert r.status_code == 200 and r.json()["state"] == "pending"
    assert "held" not in r.text and "cooldown" not in r.text
    await w.detach("b", theirs["domain_id"])
    r = await w.verify("a", mine["domain_id"])
    assert r.status_code == 200 and r.json()["state"] == "pending"
    w.dns.publish(theirs["hostname"], mine["verification"]["record_value"].split("=", 1)[1])
    r = await w.verify("a", mine["domain_id"])
    assert (r.status_code, r.json()["error"]) == (409, "public_mcp_domain_in_cooldown")
    assert r.json()["details"]["available_after"].endswith("Z")
    assert str(w.ws["b"]) not in r.text and w.users["b"] not in r.text


async def test_not_entitled_is_403(world: World) -> None:
    w = world
    await w.sql("DELETE FROM workspace_integration_licenses WHERE workspace_id = :ws", ws=w.ws["a"])
    r = await w.attach("a", w.name())
    assert (r.status_code, r.json()["error"]) == (403, "public_mcp_domain_not_entitled")
    assert r.headers["cache-control"] == "no-store"


async def test_every_route_needs_a_signed_in_owner(world: World) -> None:
    w = world
    domain_id = str(uuid4())
    for method, suffix in (
        ("GET", ""),
        ("POST", ""),
        ("POST", f"/{domain_id}/verify"),
        ("DELETE", f"/{domain_id}"),
    ):
        r = await w.client.request(method, w.path("a", suffix), json={"hostname": w.name()})
        assert r.status_code == 401, (method, suffix, r.text)
        assert r.headers["cache-control"] == "no-store"


# ----------------------------------------------------------------------------
# #2979: the CNAME proof
# ----------------------------------------------------------------------------


class CnameDns(Dns):
    """``Dns`` that also answers a row's CNAME target: ``cname[host]`` is the
    target the name's chain holds. The proof combines as the adapter does."""

    def __init__(self) -> None:
        super().__init__()
        self.cname: dict[str, str] = {}
        self.cname_targets: list[str | None] = []

    async def check(  # type: ignore[override]
        self, hostname: str, token: str, routing_target: str, *, cname_target: str | None = None
    ) -> DomainCheck:
        self.calls.append(hostname)
        self.cname_targets.append(cname_target)
        txt = "ok" if token in self.txt.get(hostname, set()) else "missing"
        if cname_target is None:
            return DomainCheck(txt, "ok")
        cname = "ok" if self.cname.get(hostname) == cname_target else "missing"
        proof, via = domain_dns.combine_proof(txt, cname)
        return DomainCheck(proof, "ok", via)


async def test_with_the_zone_a_claim_carries_its_cname_target_and_the_proof_kind(
    world: World, monkeypatch
) -> None:
    from mcp_server.public.domains import cname_proof_target

    w = world
    dns = CnameDns()
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: dns)
    monkeypatch.setattr(get_settings(), "mcp_public_domains_cname_proof_zone", "proof.example.net")
    hostname = w.name()
    r = await w.attach("a", hostname)
    assert r.status_code == 201, r.text
    body = r.json()
    token = body["verification"]["record_value"].split("=", 1)[1]
    target = cname_proof_target(token)
    assert target is not None and target.endswith(".proof.example.net")
    assert body["verification"] == {
        "record_type": "TXT",
        "record_name": f"_kagura-public-mcp.{hostname}",
        "record_value": f"kagura-public-mcp-verification={token}",
        "cname_target": target,
    }
    assert body["last_check"] == {"result": None, "checked_at": None, "proof": None}

    # Only the CNAME is published: it proves control.
    dns.cname[hostname] = target
    r = await w.verify("a", body["domain_id"])
    assert r.status_code == 200, r.text
    active = r.json()
    assert active["state"] == "active"
    assert (active["last_check"]["result"], active["last_check"]["proof"]) == ("ok", "cname")
    assert dns.cname_targets == [target]
    listed = await w.client.get(w.path("a"), headers={"x-test-user": "a"})
    (row,) = listed.json()["domains"]
    assert row["verification"]["cname_target"] == target
    assert row["last_check"]["proof"] == "cname"

    # The zone emptied: the same row answers exactly as before the feature.
    monkeypatch.setattr(get_settings(), "mcp_public_domains_cname_proof_zone", "")
    listed = await w.client.get(w.path("a"), headers={"x-test-user": "a"})
    (row,) = listed.json()["domains"]
    assert set(row["verification"]) == {"record_type", "record_name", "record_value"}
    assert set(row["last_check"]) == {"result", "checked_at"}


async def test_with_the_zone_neither_record_is_missing(world: World, monkeypatch) -> None:
    w = world
    dns = CnameDns()
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: dns)
    monkeypatch.setattr(get_settings(), "mcp_public_domains_cname_proof_zone", "proof.example.net")
    hostname = w.name()
    body = (await w.attach("a", hostname)).json()
    dns.cname[hostname] = f"{'f' * 32}.proof.example.net"  # not this row's target
    r = await w.verify("a", body["domain_id"])
    assert r.status_code == 200, r.text
    pending = r.json()
    assert pending["state"] == "pending"
    assert pending["last_check"]["result"] == "missing"
    assert pending["last_check"]["proof"] is None


async def test_without_the_zone_no_new_key_and_no_cname_target(world: World, monkeypatch) -> None:
    w = world
    dns = CnameDns()
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: dns)
    monkeypatch.setattr(get_settings(), "mcp_public_domains_cname_proof_zone", "")
    hostname = w.name()
    body = (await w.attach("a", hostname)).json()
    assert set(body["verification"]) == {"record_type", "record_name", "record_value"}
    assert body["last_check"] == {"result": None, "checked_at": None}
    dns.publish(hostname, body["verification"]["record_value"].split("=", 1)[1])
    active = (await w.verify("a", body["domain_id"])).json()
    assert active["state"] == "active"
    assert set(active["last_check"]) == {"result", "checked_at"}
    assert dns.cname_targets == [None]
