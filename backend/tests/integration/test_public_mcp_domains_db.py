"""Custom domains of public MCP endpoints on real rows (#2809).

The service (``services.public_mcp_domains``) on the test database: attach,
verify, detach, the check writer, the right and its end, reassignment, the
cooldown, and the interleavings that must never produce two holders or a row
of a revoked publication. Each owner action runs in its own session, as a
request does; the concurrency cases run on two real connections.

The resolver is a scripted fake (``Dns``); the two owner counters run on an
in-memory counter. Time is moved by updating row timestamps.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from mcp_server.public.publication import load_serving_endpoint, load_serving_endpoint_for_alias
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from models.integration_entitlement import WorkspaceIntegrationLicense, WorkspaceIntegrationState
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_domain import DOMAIN_SERVICE_ID, SERVE_LEASE_HOURS, PublicMcpDomain
from models.resource import Resource
from services import public_mcp_domain_dns as domain_dns
from services import public_mcp_domains as service
from services.public_mcp_domain_dns import DomainCheck
from services.public_mcp_domains import (
    SYSTEM_ACTOR,
    PublicMcpDomainExistsError,
    PublicMcpDomainHeldError,
    PublicMcpDomainHostnameNotAllowedError,
    PublicMcpDomainInCooldownError,
    PublicMcpDomainLimitReachedError,
    PublicMcpDomainNotEntitledError,
    PublicMcpDomainOwnerRequiredError,
    PublicMcpDomainVerificationUnavailableError,
)
from utils.datetime import utcnow
from utils.exceptions import NotFoundException

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


class Dns:
    """Scripted resolver: a proof is ``ok`` when its token is published for the name."""

    def __init__(self) -> None:
        self.txt: dict[str, set[str]] = {}
        self.unavailable: set[str] = set()
        self.calls: list[str] = []

    def publish(self, hostname: str, token: str) -> None:
        self.txt.setdefault(hostname, set()).add(token)

    def withdraw(self, hostname: str) -> None:
        self.txt.pop(hostname, None)

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck:
        self.calls.append(hostname)
        if hostname in self.unavailable:
            return DomainCheck("unavailable", "unresolved")
        return DomainCheck("ok" if token in self.txt.get(hostname, set()) else "missing", "ok")


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    dns: Dns
    tag: str
    users: dict[str, str] = field(default_factory=dict)
    ws: dict[str, UUID] = field(default_factory=dict)
    slug: dict[str, str] = field(default_factory=dict)
    endpoint: dict[str, UUID] = field(default_factory=dict)
    public_id: dict[str, str] = field(default_factory=dict)
    pauses: list[Pause] = field(default_factory=list)

    def name(self, label: str = "shop") -> str:
        return f"{label}-{uuid4().hex[:10]}.example.org"

    async def run(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        async with self.factory() as s:
            return await fn(s, *args)

    async def attach(self, who: str, hostname: str, slug_of: str | None = None) -> PublicMcpDomain:
        return await self.run(
            service.attach_domain,
            self.users[who],
            self.ws[who],
            self.slug[slug_of or who],
            hostname,
        )

    async def verify(self, who: str, domain_id: UUID) -> PublicMcpDomain:
        return await self.run(
            service.verify_domain, self.users[who], self.ws[who], self.slug[who], domain_id
        )

    async def detach(self, who: str, domain_id: UUID) -> None:
        await self.run(
            service.detach_domain, self.users[who], self.ws[who], self.slug[who], domain_id
        )

    async def row(self, domain_id: UUID) -> PublicMcpDomain | None:
        async with self.factory() as s:
            return await s.get(PublicMcpDomain, domain_id)

    async def serves(self, hostname: str) -> UUID | None:
        async with self.factory() as s:
            endpoint = await load_serving_endpoint_for_alias(s, hostname)
            return endpoint.id if endpoint else None

    async def sql(self, statement: str, **params: Any) -> None:
        async with self.factory() as s:
            await s.execute(text(statement), params)
            await s.commit()

    async def active(self, who: str, hostname: str | None = None) -> PublicMcpDomain:
        hostname = hostname or self.name()
        row = await self.attach(who, hostname)
        self.dns.publish(hostname, row.verification_token)
        verified = await self.verify(who, row.id)
        assert verified.state == "active"
        return verified


def _entitlement_rows(ws: UUID, *, license_: bool = True) -> list[Any]:
    now = utcnow()
    rows: list[Any] = [
        WorkspaceIntegrationState(
            workspace_id=ws,
            revision=1,
            event_id=f"evt-{uuid4().hex}",
            source="external_billing",
            source_ref="fixture",
            max_valid_from=now - timedelta(days=1),
            max_valid_until=now + timedelta(days=30),
            max_evidence_ref="fixture-max",
        )
    ]
    if license_:
        rows.append(_license(ws, DOMAIN_SERVICE_ID))
    return rows


def _license(ws: UUID, service_id: str) -> WorkspaceIntegrationLicense:
    now = utcnow()
    return WorkspaceIntegrationLicense(
        workspace_id=ws,
        service_id=service_id,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=30),
        evidence_ref="fixture-license",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, monkeypatch) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    w = World(factory=factory, dns=Dns(), tag=tag)
    w.users = {
        "a": f"dom-a-{tag}",
        "a_member": f"dom-am-{tag}",
        "b": f"dom-b-{tag}",
        "c": f"dom-c-{tag}",
    }
    async with factory() as s:
        for uid in w.users.values():
            s.add(User(email=f"{uid}@example.org", user_id=uid, name=uid, role="user"))
        await s.flush()
        for key in ("a", "b", "c"):
            ws = Workspace(
                id=uuid4(),
                name=f"dom-{key}-{tag}",
                plan_name="promax",
                owner_user_id=w.users[key],
                daily_api_limit=50000,
                weekly_api_limit=250000,
            )
            s.add(ws)
            await s.flush()
            w.ws[key] = ws.id
            w.slug[key] = f"faq-{key}-{tag}"
            s.add(
                WorkspaceMember(workspace_id=ws.id, user_id=w.users[key], role=WorkspaceRole.OWNER)
            )
            resource = Resource(
                workspace_id=ws.id, resource_id=w.slug[key], created_by=w.users[key]
            )
            ctx = Context(
                id=uuid4(),
                workspace_id=ws.id,
                name=w.slug[key],
                created_by=w.users[key],
                is_private=False,
                is_public=True,
                resource_id=w.slug[key],
            )
            s.add_all([resource, ctx])
            await s.flush()
            endpoint = PublicMcpEndpoint(
                workspace_id=ws.id,
                context_id=ctx.id,
                resource_pk=resource.id,
                resource_id=w.slug[key],
                public_name=f"FAQ {key}",
                created_by=w.users[key],
            )
            s.add(endpoint)
            await s.flush()
            w.endpoint[key], w.public_id[key] = endpoint.id, endpoint.public_id
            # c has the Max window but not the domain license.
            for entitlement_row in _entitlement_rows(ws.id, license_=key != "c"):
                s.add(entitlement_row)
                await s.flush()
        s.add(
            WorkspaceMember(
                workspace_id=w.ws["a"], user_id=w.users["a_member"], role=WorkspaceRole.OWNER
            )
        )
        await s.commit()

    settings = get_settings()
    for name, value in {
        "mcp_public_domains_enabled": True,
        "mcp_public_endpoints_enabled": True,
        "mcp_public_domains_authority": "entitlement",
        "mcp_public_domains_routing_target": "edge.example.net",
        "mcp_public_domains_reassign_cooldown_hours": 168,
        "mcp_public_domains_max_per_workspace": 3,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: w.dns)
    counts: dict[str, int] = {}

    async def incr(key: str, amount: int, ttl: int | None = None) -> int:
        counts[key] = counts.get(key, 0) + amount
        return counts[key]

    monkeypatch.setattr(service, "incrby_counter", incr)
    try:
        yield w
    finally:
        for pause in w.pauses:
            pause.release.set()
        await asyncio.sleep(0.5)
        async with factory() as s:
            ids = list(w.ws.values())
            await s.execute(delete(PublicMcpDomain).where(PublicMcpDomain.workspace_id.in_(ids)))
            await s.execute(delete(Workspace).where(Workspace.id.in_(ids)))
            await s.execute(delete(User).where(User.user_id.in_(list(w.users.values()))))
            await s.commit()


class Pause:
    """Hold one call of an async function of the service until released.

    The world releases every pause before its teardown, so that a failed
    assertion never leaves a transaction waiting.
    """

    def __init__(
        self,
        w: World,
        monkeypatch,
        name: str,
        when: Callable[..., bool] = lambda *a, **k: True,
    ) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = True
        w.pauses.append(self)
        original = getattr(service, name)

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            if self.armed and when(*args, **kwargs):
                self.armed = False
                self.reached.set()
                await self.release.wait()
            return result

        monkeypatch.setattr(service, name, wrapper)


async def _holders(w: World, hostname: str) -> list[tuple[UUID, str]]:
    async with w.factory() as s:
        rows = (
            await s.execute(
                select(PublicMcpDomain.workspace_id, PublicMcpDomain.state).where(
                    PublicMcpDomain.hostname == hostname,
                    PublicMcpDomain.state.in_(("active", "suspended")),
                )
            )
        ).all()
    return [(ws, state) for ws, state in rows]


# ----------------------------------------------------------------------------
# Attach, verify, the lease
# ----------------------------------------------------------------------------


async def test_attach_then_verify_activates_with_a_lease(world: World) -> None:
    w = world
    hostname = w.name()
    row = await w.attach("a", f"  {hostname.upper()}. ")
    assert (row.hostname, row.state, row.endpoint_id) == (hostname, "pending", w.endpoint["a"])
    assert len(row.verification_token) == 64
    assert await w.serves(hostname) is None

    still = await w.verify("a", row.id)  # nothing published yet
    assert still.state == "pending"
    assert still.last_proof_result == "missing"
    assert still.check_seq == 1

    w.dns.publish(hostname, row.verification_token)
    active = await w.verify("a", row.id)
    assert active.state == "active"
    assert active.verified_at == active.proof_ok_at
    assert active.serve_until is not None
    lease = active.serve_until - active.verified_at
    assert timedelta(hours=SERVE_LEASE_HOURS) - timedelta(seconds=5) <= lease
    assert lease <= timedelta(hours=SERVE_LEASE_HOURS)
    assert await w.serves(hostname) == w.endpoint["a"]

    async with w.factory() as s:
        audits = (
            await s.execute(
                select(
                    AuditLog.action, AuditLog.user_id, AuditLog.user_email, AuditLog.user_metadata
                )
                .where(AuditLog.resource == f"public_mcp_domain:{row.id}")
                .order_by(AuditLog.id)
            )
        ).all()
    assert [a.action for a in audits] == [
        "public_mcp_domain.attached",
        "public_mcp_domain.verified",
    ]
    assert {a.user_id for a in audits} == {w.users["a"]}
    assert {a.user_email for a in audits} == {f"{w.users['a']}@example.org"}
    meta = audits[1].user_metadata
    assert meta["from_state"] == "pending" and meta["to_state"] == "active"
    assert meta["workspace_id"] == str(w.ws["a"]) and meta["endpoint_id"] == str(w.endpoint["a"])
    assert hostname not in str(meta) and len(meta["hostname_sha256"]) == 64


async def test_attach_refusals(world: World) -> None:
    w = world
    with pytest.raises(PublicMcpDomainOwnerRequiredError):
        await w.run(service.attach_domain, w.users["a_member"], w.ws["a"], w.slug["a"], w.name())
    for bad in ("localhost", "x.onion", "app.example", "has space.example.org", "edge.example.net"):
        with pytest.raises(PublicMcpDomainHostnameNotAllowedError):
            await w.attach("a", bad)
    # Another workspace's resource: no live endpoint of that slug here.
    with pytest.raises(NotFoundException):
        await w.attach("a", w.name(), slug_of="b")


async def test_one_domain_per_publication_and_the_workspace_limit(
    world: World, monkeypatch
) -> None:
    w = world
    first = await w.attach("b", w.name())
    with pytest.raises(PublicMcpDomainExistsError):
        await w.attach("b", w.name())
    monkeypatch.setattr(get_settings(), "mcp_public_domains_max_per_workspace", 1)
    await w.detach("b", first.id)
    second = await w.attach("b", w.name())
    async with w.factory() as s:
        ctx = Context(
            id=uuid4(),
            workspace_id=w.ws["b"],
            name=f"two-{w.tag}",
            created_by=w.users["b"],
            is_private=False,
            is_public=True,
            resource_id=f"two-{w.tag}",
        )
        resource = Resource(
            workspace_id=w.ws["b"], resource_id=f"two-{w.tag}", created_by=w.users["b"]
        )
        s.add_all([ctx, resource])
        await s.flush()
        s.add(
            PublicMcpEndpoint(
                workspace_id=w.ws["b"],
                context_id=ctx.id,
                resource_pk=resource.id,
                resource_id=f"two-{w.tag}",
                public_name="Two",
                created_by=w.users["b"],
            )
        )
        await s.commit()
    with pytest.raises(PublicMcpDomainLimitReachedError):
        await w.run(service.attach_domain, w.users["b"], w.ws["b"], f"two-{w.tag}", w.name())
    await w.detach("b", second.id)


async def test_unavailable_lookup_writes_nothing(world: World) -> None:
    w = world
    hostname = w.name()
    row = await w.attach("a", hostname)
    w.dns.unavailable.add(hostname)
    with pytest.raises(PublicMcpDomainVerificationUnavailableError):
        await w.verify("a", row.id)
    after = await w.row(row.id)
    assert after is not None
    assert (after.state, after.check_seq, after.last_checked_at) == ("pending", 0, None)
    w.dns.unavailable.discard(hostname)
    await w.detach("a", row.id)


async def test_pending_detach_deletes_the_row(world: World) -> None:
    w = world
    row = await w.attach("a", w.name())
    await w.detach("a", row.id)
    assert await w.row(row.id) is None
    with pytest.raises(NotFoundException):
        await w.detach("a", row.id)


async def test_detach_releases_with_a_cooldown_and_stops_serving(world: World) -> None:
    w = world
    active = await w.active("a")
    await w.detach("a", active.id)
    released = await w.row(active.id)
    assert released is not None and released.state == "released"
    assert released.cooldown_until - released.released_at == timedelta(hours=168)
    assert await w.serves(active.hostname) is None
    with pytest.raises(NotFoundException):
        await w.verify("a", active.id)


# ----------------------------------------------------------------------------
# Another tenant's domain
# ----------------------------------------------------------------------------


async def test_another_workspace_cannot_take_an_active_name(world: World) -> None:
    w = world
    a = await w.active("a")
    b = await w.attach("b", a.hostname)  # pending, nothing about A
    assert b.state == "pending" and b.workspace_id == w.ws["b"]

    # A's token is published, B's is not: B stays pending.
    after = await w.verify("b", b.id)
    assert after.state == "pending" and after.last_proof_result == "missing"

    # B's own token published: only now B learns the name is held.
    w.dns.publish(a.hostname, b.verification_token)
    with pytest.raises(PublicMcpDomainHeldError):
        await w.verify("b", b.id)
    assert (await w.row(b.id)).state == "pending"
    assert (await w.row(a.id)).state == "active"
    assert await w.serves(a.hostname) == w.endpoint["a"]
    await w.detach("b", b.id)
    await w.detach("a", a.id)


async def test_a_row_whose_workspace_differs_from_its_endpoint_never_resolves(world: World) -> None:
    w = world
    hostname = w.name()
    async with w.factory() as s:
        s.add(
            PublicMcpDomain(
                hostname=hostname,
                workspace_id=w.ws["b"],
                endpoint_id=w.endpoint["a"],
                state="active",
                verification_token="0" * 64,
                serve_until=utcnow() + timedelta(hours=1),
                verified_at=utcnow(),
                proof_ok_at=utcnow(),
            )
        )
        await s.commit()
    assert await w.serves(hostname) is None
    await w.sql("DELETE FROM public_mcp_domains WHERE hostname = :h", h=hostname)


async def test_two_workspaces_verifying_one_name_at_once(world: World) -> None:
    w = world
    hostname = w.name()
    a = await w.attach("a", hostname)
    b = await w.attach("b", hostname)
    w.dns.publish(hostname, a.verification_token)
    w.dns.publish(hostname, b.verification_token)
    results = await asyncio.gather(w.verify("a", a.id), w.verify("b", b.id), return_exceptions=True)
    won = [r for r in results if isinstance(r, PublicMcpDomain)]
    lost = [r for r in results if isinstance(r, Exception)]
    assert len(won) == 1 and won[0].state == "active"
    assert len(lost) == 1 and isinstance(lost[0], PublicMcpDomainHeldError)
    assert len(await _holders(w, hostname)) == 1
    for who, row in (("a", a), ("b", b)):
        await w.detach(who, row.id)


async def test_the_name_lock_orders_two_activations(world: World, monkeypatch) -> None:
    """The second activation waits for the name lock and is refused by the I1
    statement itself, not by the unique index."""
    w = world
    hostname = w.name()
    a = await w.attach("a", hostname)
    b = await w.attach("b", hostname)
    w.dns.publish(hostname, a.verification_token)
    w.dns.publish(hostname, b.verification_token)
    pause = Pause(w, monkeypatch, "_refuse_other_holder")
    first = asyncio.create_task(w.verify("a", a.id))
    await asyncio.wait_for(pause.reached.wait(), 5)  # A passed I1, not yet written
    second = asyncio.create_task(w.verify("b", b.id))
    await asyncio.sleep(0.5)
    assert not second.done()
    pause.release.set()
    assert (await first).state == "active"
    with pytest.raises(PublicMcpDomainHeldError) as raised:
        await second
    assert raised.value.__cause__ is None
    for who, row in (("a", a), ("b", b)):
        await w.detach(who, row.id)


async def test_the_unique_index_answers_held(world: World, monkeypatch) -> None:
    """The I1 statement replaced by one that sees nothing: the index still refuses (409)."""
    w = world
    a = await w.active("a")
    b = await w.attach("b", a.hostname)
    w.dns.publish(a.hostname, b.verification_token)

    async def blind(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(service, "_refuse_other_holder", blind)
    with pytest.raises(PublicMcpDomainHeldError) as raised:
        await w.verify("b", b.id)
    assert isinstance(raised.value.__cause__, IntegrityError)
    assert (await w.row(b.id)).state == "pending"
    await w.detach("b", b.id)
    await w.detach("a", a.id)


# ----------------------------------------------------------------------------
# Reassignment and the cooldown
# ----------------------------------------------------------------------------


async def test_cooldown_refuses_another_workspace_only_after_its_proof(world: World) -> None:
    w = world
    a = await w.active("a")
    await w.detach("a", a.id)
    b = await w.attach("b", a.hostname)
    # Without B's proof: pending and silent about the cooldown.
    assert (await w.verify("b", b.id)).state == "pending"
    w.dns.publish(a.hostname, b.verification_token)
    with pytest.raises(PublicMcpDomainInCooldownError) as raised:
        await w.verify("b", b.id)
    released = await w.row(a.id)
    assert raised.value.details["available_after"].startswith(
        released.cooldown_until.isoformat()[:16]
    )
    # The cooldown passed (timestamps moved): allowed.
    await w.sql(
        "UPDATE public_mcp_domains SET released_at = released_at - interval '200 hours', "
        "cooldown_until = cooldown_until - interval '200 hours' WHERE id = :id",
        id=a.id,
    )
    assert (await w.verify("b", b.id)).state == "active"
    assert await w.serves(a.hostname) == w.endpoint["b"]
    await w.detach("b", b.id)


async def test_same_workspace_may_reattach_during_its_cooldown_with_a_new_token(
    world: World,
) -> None:
    w = world
    a = await w.active("a")
    old_token = a.verification_token
    await w.detach("a", a.id)
    again = await w.attach("a", a.hostname)
    assert again.verification_token != old_token
    # The old token is still published: it proves nothing for the new row.
    assert (await w.verify("a", again.id)).state == "pending"
    w.dns.publish(a.hostname, again.verification_token)
    assert (await w.verify("a", again.id)).state == "active"
    await w.detach("a", again.id)


async def test_detach_against_a_foreign_verify_detach_first(world: World, monkeypatch) -> None:
    w = world
    a = await w.active("a")
    b = await w.attach("b", a.hostname)
    w.dns.publish(a.hostname, b.verification_token)
    # A's detach holds its row, released but not committed.
    pause = Pause(w, monkeypatch, "_db_now")
    detach = asyncio.create_task(w.detach("a", a.id))
    await asyncio.wait_for(pause.reached.wait(), 5)
    with pytest.raises(PublicMcpDomainHeldError):
        await w.verify("b", b.id)
    pause.release.set()
    await detach
    assert await _holders(w, a.hostname) == []
    with pytest.raises(PublicMcpDomainInCooldownError):
        await w.verify("b", b.id)
    assert await _holders(w, a.hostname) == []
    await w.detach("b", b.id)


async def test_detach_against_a_foreign_verify_verify_first(world: World, monkeypatch) -> None:
    w = world
    a = await w.active("a")
    b = await w.attach("b", a.hostname)
    w.dns.publish(a.hostname, b.verification_token)
    # B's activation holds the name lock and its row, before the I1 statement.
    pause = Pause(w, monkeypatch, "_fenced_row")
    verify = asyncio.create_task(w.verify("b", b.id))
    await asyncio.wait_for(pause.reached.wait(), 5)
    await w.detach("a", a.id)  # needs no lock: not blocked
    pause.release.set()
    with pytest.raises(PublicMcpDomainInCooldownError):
        await verify
    assert await _holders(w, a.hostname) == []
    await w.detach("b", b.id)


# ----------------------------------------------------------------------------
# Unpublish and attach on two connections
# ----------------------------------------------------------------------------


async def _unpublish(s: AsyncSession, endpoint_id: UUID) -> None:
    """What the unpublish route does in its transaction (switch on)."""
    await s.execute(
        update(PublicMcpEndpoint)
        .where(PublicMcpEndpoint.id == endpoint_id)
        .values(revoked_at=utcnow())
    )
    await service.release_domains_of_endpoint(s, endpoint_id, SYSTEM_ACTOR)


async def _republish(w: World, key: str) -> None:
    async with w.factory() as s:
        old = await s.get(PublicMcpEndpoint, w.endpoint[key])
        endpoint = PublicMcpEndpoint(
            workspace_id=old.workspace_id,
            context_id=old.context_id,
            resource_pk=old.resource_pk,
            resource_id=old.resource_id,
            public_name=old.public_name,
            created_by=old.created_by,
        )
        s.add(endpoint)
        await s.commit()
        w.endpoint[key], w.public_id[key] = endpoint.id, endpoint.public_id


async def _open_rows_of(w: World, endpoint_id: UUID) -> list[str]:
    async with w.factory() as s:
        return list(
            (
                await s.execute(
                    select(PublicMcpDomain.state).where(
                        PublicMcpDomain.endpoint_id == endpoint_id,
                        PublicMcpDomain.state != "released",
                    )
                )
            ).scalars()
        )


async def test_unpublish_releases_holders_and_deletes_claims(world: World) -> None:
    w = world
    a = await w.active("a")
    old_endpoint = w.endpoint["a"]
    async with w.factory() as s:
        await _unpublish(s, old_endpoint)
        await s.commit()
    assert (await w.row(a.id)).state == "released"
    assert await w.serves(a.hostname) is None
    await _republish(w, "a")
    assert await w.serves(a.hostname) is None  # the alias bound the old publication
    pending = await w.attach("a", w.name())
    async with w.factory() as s:
        await _unpublish(s, w.endpoint["a"])
        await s.commit()
    assert await w.row(pending.id) is None
    await _republish(w, "a")


async def test_attach_against_unpublish_unpublish_first(world: World) -> None:
    w = world
    endpoint_id = w.endpoint["c"]
    await w.sql(
        "INSERT INTO workspace_integration_licenses "
        "(workspace_id, service_id, valid_from, valid_until, evidence_ref) "
        "VALUES (:ws, :sid, now() - interval '1 day', now() + interval '30 days', 'x')",
        ws=w.ws["c"],
        sid=DOMAIN_SERVICE_ID,
    )
    try:
        async with w.factory() as unpublisher:
            await _unpublish(unpublisher, endpoint_id)  # endpoint row written, not committed
            attach = asyncio.create_task(w.attach("c", w.name()))
            await asyncio.sleep(0.5)
            assert not attach.done()  # waits on the endpoint row
            await unpublisher.commit()
        with pytest.raises(NotFoundException):
            await attach
        assert await _open_rows_of(w, endpoint_id) == []
    finally:
        await w.sql(
            "DELETE FROM workspace_integration_licenses WHERE workspace_id = :ws AND service_id = :sid",
            ws=w.ws["c"],
            sid=DOMAIN_SERVICE_ID,
        )
        await _republish(w, "c")


async def test_attach_against_unpublish_attach_first(world: World, monkeypatch) -> None:
    w = world
    endpoint_id = w.endpoint["b"]
    # Attach holds the endpoint row FOR SHARE, its pending row not committed.
    pause = Pause(w, monkeypatch, "_live_endpoint", when=lambda *a, **k: k.get("share") is True)
    attach = asyncio.create_task(w.attach("b", w.name()))
    await asyncio.wait_for(pause.reached.wait(), 5)

    async def unpublish() -> None:
        async with w.factory() as s:
            await _unpublish(s, endpoint_id)
            await s.commit()

    unpublishing = asyncio.create_task(unpublish())
    await asyncio.sleep(0.5)
    assert not unpublishing.done()  # waits on the attach's share lock
    pause.release.set()
    created = await attach
    await unpublishing
    assert await w.row(created.id) is None  # the unpublish deleted the claim
    assert await _open_rows_of(w, endpoint_id) == []
    await _republish(w, "b")


# ----------------------------------------------------------------------------
# The right
# ----------------------------------------------------------------------------


async def _check(w: World, row: PublicMcpDomain) -> service.AppliedCheck:
    current = await w.row(row.id)
    check = await service.lookup(current.hostname, current.verification_token)
    return await w.run(service.apply_check, row.id, current.check_seq, check, SYSTEM_ACTOR)


async def _set_license(w: World, key: str, present: bool) -> None:
    if present:
        async with w.factory() as s:
            s.add(_license(w.ws[key], DOMAIN_SERVICE_ID))
            await s.commit()
    else:
        await w.sql(
            "DELETE FROM workspace_integration_licenses WHERE workspace_id = :ws AND service_id = :sid",
            ws=w.ws[key],
            sid=DOMAIN_SERVICE_ID,
        )


async def test_max_window_without_the_license_cannot_attach(world: World) -> None:
    with pytest.raises(PublicMcpDomainNotEntitledError):
        await world.attach("c", world.name())
    assert await _open_rows_of(world, world.endpoint["c"]) == []


async def test_license_removed_suspends_at_the_next_check_and_comes_back(world: World) -> None:
    w = world
    a = await w.active("a")
    await _set_license(w, "a", False)
    try:
        assert await w.serves(a.hostname) == w.endpoint["a"]  # until the next check
        applied = await _check(w, a)
        assert (applied.right_active, applied.audit_action) == (False, "suspended")
        row = await w.row(a.id)
        assert (row.state, row.suspended_reason) == ("suspended", "right_ended")
        assert await w.serves(a.hostname) is None
        async with w.factory() as s:
            assert await load_serving_endpoint(s, w.public_id["a"]) is not None
    finally:
        await _set_license(w, "a", True)
    applied = await _check(w, a)  # proof still published
    assert applied.audit_action == "reinstated"
    assert await w.serves(a.hostname) == w.endpoint["a"]
    await w.detach("a", a.id)


async def test_owner_verify_after_the_license_was_removed_suspends_and_answers_403(
    world: World,
) -> None:
    w = world
    a = await w.active("a")
    await _set_license(w, "a", False)
    try:
        with pytest.raises(PublicMcpDomainNotEntitledError):
            await w.verify("a", a.id)
        row = await w.row(a.id)
        assert (row.state, row.suspended_reason) == ("suspended", "right_ended")
    finally:
        await _set_license(w, "a", True)
    assert (await w.verify("a", a.id)).state == "active"
    await w.detach("a", a.id)


async def test_another_license_removed_changes_nothing(world: World) -> None:
    """Ending another paid integration (the sync add-on) leaves both entries serving."""
    w = world
    async with w.factory() as s:
        s.add(_license(w.ws["a"], "sync-fixture-service"))
        await s.commit()
    a = await w.active("a")
    await w.sql(
        "DELETE FROM workspace_integration_licenses WHERE workspace_id = :ws AND service_id = :sid",
        ws=w.ws["a"],
        sid="sync-fixture-service",
    )
    applied = await _check(w, a)
    assert applied.right_active and applied.audit_action is None
    assert await w.serves(a.hostname) == w.endpoint["a"]
    async with w.factory() as s:
        assert await load_serving_endpoint(s, w.public_id["a"]) is not None
    await w.detach("a", a.id)


@pytest.mark.parametrize("authority", ["entitlement", "operator"])
async def test_lease_passed_without_a_check_stops_the_alias(
    world: World, monkeypatch, authority: str
) -> None:
    w = world
    a = await w.active("a")
    monkeypatch.setattr(get_settings(), "mcp_public_domains_authority", authority)
    await w.sql(
        "UPDATE public_mcp_domains SET serve_until = timezone('UTC', statement_timestamp()) "
        "- interval '1 second' WHERE id = :id",
        id=a.id,
    )
    assert await w.serves(a.hostname) is None
    assert (await w.row(a.id)).state == "active"
    applied = await _check(w, a)  # a completed check renews the lease
    assert applied.right_active
    assert await w.serves(a.hostname) == w.endpoint["a"]
    await w.detach("a", a.id)


async def test_operator_authority_needs_no_publisher(world: World, monkeypatch) -> None:
    w = world
    monkeypatch.setattr(get_settings(), "mcp_public_domains_authority", "operator")
    c = await w.active("c")  # c has no domain license
    lease = c.serve_until - c.verified_at
    assert timedelta(hours=SERVE_LEASE_HOURS) - timedelta(seconds=5) <= lease
    assert lease <= timedelta(hours=SERVE_LEASE_HOURS)
    # Back to entitlement without a license: serving until the next check.
    monkeypatch.setattr(get_settings(), "mcp_public_domains_authority", "entitlement")
    assert await w.serves(c.hostname) == w.endpoint["c"]
    applied = await _check(w, c)
    assert (applied.right_active, applied.to_state) == (False, "suspended")
    assert await w.serves(c.hostname) is None
    await w.detach("c", c.id)


async def test_entitlement_lease_never_passes_the_published_window(world: World) -> None:
    w = world
    await w.sql(
        "UPDATE workspace_integration_licenses SET valid_until = now() + interval '2 hours' "
        "WHERE workspace_id = :ws AND service_id = :sid",
        ws=w.ws["a"],
        sid=DOMAIN_SERVICE_ID,
    )
    try:
        a = await w.active("a")
        assert a.serve_until - a.verified_at <= timedelta(hours=2, seconds=5)
    finally:
        await w.sql(
            "UPDATE workspace_integration_licenses SET valid_until = now() + interval '30 days' "
            "WHERE workspace_id = :ws AND service_id = :sid",
            ws=w.ws["a"],
            sid=DOMAIN_SERVICE_ID,
        )
    await w.detach("a", a.id)


# ----------------------------------------------------------------------------
# Workspace lifecycle and the fence
# ----------------------------------------------------------------------------


async def test_workspace_soft_delete_stops_serving_and_restore_serves(world: World) -> None:
    w = world
    a = await w.active("a")
    await w.sql("UPDATE workspaces SET deleted_at = now() WHERE id = :id", id=w.ws["a"])
    try:
        assert await w.serves(a.hostname) is None
    finally:
        await w.sql("UPDATE workspaces SET deleted_at = NULL WHERE id = :id", id=w.ws["a"])
    assert await w.serves(a.hostname) == w.endpoint["a"]
    await w.detach("a", a.id)


async def test_workspace_hard_delete_leaves_the_row(world: World) -> None:
    w = world
    async with w.factory() as s:
        ws = Workspace(
            id=uuid4(),
            name=f"gone-{w.tag}",
            plan_name="promax",
            owner_user_id=w.users["b"],
            daily_api_limit=1,
            weekly_api_limit=1,
        )
        s.add(ws)
        await s.flush()
        row = PublicMcpDomain(
            hostname=w.name(),
            workspace_id=ws.id,
            endpoint_id=uuid4(),
            state="active",
            verification_token="1" * 64,
            serve_until=utcnow() + timedelta(hours=1),
            verified_at=utcnow(),
            proof_ok_at=utcnow(),
        )
        s.add(row)
        await s.commit()
        await s.execute(delete(Workspace).where(Workspace.id == ws.id))
        await s.commit()
        left = await s.scalar(select(func.count()).where(PublicMcpDomain.id == row.id))
        assert left == 1
        await s.execute(delete(PublicMcpDomain).where(PublicMcpDomain.id == row.id))
        await s.commit()


async def test_a_row_changed_during_the_lookup_is_not_overwritten(world: World) -> None:
    w = world
    a = await w.active("a")
    seen = (await w.row(a.id)).check_seq
    await w.sql("UPDATE public_mcp_domains SET check_seq = check_seq + 1 WHERE id = :id", id=a.id)
    applied = await w.run(
        service.apply_check, a.id, seen, DomainCheck("missing", "ok"), SYSTEM_ACTOR
    )
    assert applied.outcome == "fenced"
    after = await w.row(a.id)
    assert after.proof_missing_checks == 0 and after.check_seq == seen + 1
    await w.detach("a", a.id)


async def test_unavailable_check_keeps_state_and_renews_the_lease(world: World) -> None:
    w = world
    a = await w.active("a")
    await w.sql(
        "UPDATE public_mcp_domains SET serve_until = serve_until - interval '70 hours', "
        "last_checked_at = last_checked_at - interval '2 hours' WHERE id = :id",
        id=a.id,
    )
    before = await w.row(a.id)
    w.dns.unavailable.add(a.hostname)
    applied = await _check(w, a)
    w.dns.unavailable.discard(a.hostname)
    after = await w.row(a.id)
    assert applied.audit_action is None
    assert after.state == "active"
    assert after.last_proof_result == "unavailable"
    assert after.last_checked_at > before.last_checked_at
    assert after.serve_until > before.serve_until
    assert after.proof_ok_at == before.proof_ok_at
    await w.detach("a", a.id)
