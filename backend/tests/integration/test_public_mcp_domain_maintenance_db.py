"""The custom domain maintenance run on real rows (#2809).

``services.public_mcp_domain_maintenance.run_maintenance`` on the test
database, under the ``entitlement`` authority (each check takes the paid
guard): housekeeping, the due order (rows whose last lookup was
indeterminate last), every completed lookup applied through the one writer,
the fence, the run budget, no connection held during a lookup, and two runs
overlapping. The resolver is a scripted fake; time is moved by updating row
timestamps.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import structlog
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from mcp_server.public.publication import load_serving_endpoint_for_alias
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from models.integration_entitlement import WorkspaceIntegrationLicense, WorkspaceIntegrationState
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_domain import DOMAIN_SERVICE_ID, PublicMcpDomain
from models.resource import Resource
from services import public_mcp_domain_dns as domain_dns
from services import public_mcp_domain_maintenance as maintenance
from services import public_mcp_domains as service
from services.public_mcp_domain_dns import DomainCheck
from services.public_mcp_domains import hostname_digest

pytestmark = pytest.mark.asyncio(loop_scope="session")

PERMISSION = "/internal/public-mcp-domains/tls-permission"
SYSTEM = "system:public-mcp-domains"


class Dns:
    """Scripted resolver. Per name: ``ok`` / ``missing`` / ``unavailable`` /
    ``slow`` (never answers: the caller's timeout makes it ``unavailable``);
    an optional hook runs inside the lookup."""

    def __init__(self) -> None:
        self.mode: dict[str, str] = {}
        self.routing: dict[str, str] = {}
        self.calls: list[str] = []
        self.hook: Any = None
        self.delay = 0.0

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck:
        self.calls.append(hostname)
        if self.hook is not None:
            await self.hook(hostname)
        if self.delay:
            await asyncio.sleep(self.delay)
        mode = self.mode.get(hostname, "ok")
        if mode == "slow":
            await asyncio.sleep(3600)
        routing: Any = self.routing.get(hostname, "ok")
        return DomainCheck(mode, routing)  # type: ignore[arg-type]


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    dns: Dns
    tag: str
    user: str
    ws: UUID
    endpoints: list[UUID] = field(default_factory=list)
    extra_ws: list[UUID] = field(default_factory=list)
    added: list[UUID] = field(default_factory=list)
    _free: int = 0

    def name(self, label: str = "m") -> str:
        return f"{label}-{uuid4().hex[:10]}.example.org"

    def endpoint(self) -> UUID:
        endpoint = self.endpoints[self._free]
        self._free += 1
        return endpoint

    async def sql(self, statement: str, **params: Any) -> None:
        async with self.factory() as s:
            await s.execute(text(statement), params)
            await s.commit()

    async def add(self, state: str = "active", **values: Any) -> PublicMcpDomain:
        """A row inserted directly; times are relative to the database clock."""
        async with self.factory() as s:
            now = await s.scalar(text("SELECT timezone('UTC', statement_timestamp())"))
        assert isinstance(now, datetime)
        fields: dict[str, Any] = {
            "hostname": self.name(),
            "workspace_id": self.ws,
            "endpoint_id": values.pop("endpoint_id", None) or self.endpoint(),
            "state": state,
            "verification_token": uuid4().hex * 2,
        }
        if state in ("active", "suspended"):
            fields |= {
                "verified_at": now - timedelta(days=10),
                "proof_ok_at": now - timedelta(hours=1),
                "last_checked_at": now - timedelta(hours=2),
                "last_proof_result": "ok",
                "routing_status": "ok",
            }
        if state == "active":
            fields["serve_until"] = now + timedelta(hours=60)
        if state == "suspended":
            fields |= {"suspended_reason": "proof_lost", "suspended_at": now - timedelta(days=1)}
        if state == "released":
            fields |= {"released_at": now, "cooldown_until": now + timedelta(days=3)}
        for key, value in values.items():
            fields[key] = now + value if isinstance(value, timedelta) else value
        async with self.factory() as s:
            row = PublicMcpDomain(**fields)
            s.add(row)
            await s.commit()
            await s.refresh(row)
        self.added.append(row.id)
        return row

    async def row(self, domain_id: UUID) -> PublicMcpDomain | None:
        async with self.factory() as s:
            return await s.get(PublicMcpDomain, domain_id)

    async def serves(self, hostname: str) -> UUID | None:
        async with self.factory() as s:
            endpoint = await load_serving_endpoint_for_alias(s, hostname)
            return endpoint.id if endpoint else None

    async def audits(self, domain_id: UUID) -> list[AuditLog]:
        async with self.factory() as s:
            return list(
                (
                    await s.execute(
                        select(AuditLog)
                        .where(AuditLog.resource == f"public_mcp_domain:{domain_id}")
                        .order_by(AuditLog.id)
                    )
                )
                .scalars()
                .all()
            )

    async def age(self, ids: list[UUID], interval: str = "61 minutes") -> None:
        """Make rows due again: their last check moves ``interval`` back."""
        await self.sql(
            f"UPDATE public_mcp_domains SET last_checked_at = last_checked_at - interval '{interval}'"
            " WHERE id = ANY(:ids)",
            ids=ids,
        )

    async def run(self) -> maintenance.MaintenanceCounts:
        return await maintenance.run_maintenance(self.factory)


async def _endpoints(s: AsyncSession, ws: UUID, user: str, tag: str, n: int) -> list[UUID]:
    ids: list[UUID] = []
    for i in range(n):
        slug = f"m-{tag}-{i}"
        resource = Resource(workspace_id=ws, resource_id=slug, created_by=user)
        ctx = Context(
            id=uuid4(),
            workspace_id=ws,
            name=slug,
            created_by=user,
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
        s.add_all([resource, ctx])
        await s.flush()
        endpoint = PublicMcpEndpoint(
            workspace_id=ws,
            context_id=ctx.id,
            resource_pk=resource.id,
            resource_id=slug,
            public_name=f"Docs {i}",
            created_by=user,
        )
        s.add(endpoint)
        await s.flush()
        ids.append(endpoint.id)
    return ids


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, monkeypatch) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    user = f"maint-{tag}"
    async with factory() as s:
        now = await s.scalar(text("SELECT timezone('UTC', statement_timestamp())"))
        s.add(User(email=f"{user}@example.org", user_id=user, name=user, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"maint-{tag}",
            plan_name="promax",
            owner_user_id=user,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add(ws)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user, role=WorkspaceRole.OWNER))
        endpoints = await _endpoints(s, ws.id, user, tag, 70)
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
        ws_id = ws.id

    settings = get_settings()
    for name, value in {
        "mcp_public_domains_enabled": True,
        "mcp_public_endpoints_enabled": True,
        "mcp_public_domains_authority": "entitlement",
        "mcp_public_domains_routing_target": "edge.example.net",
        "mcp_public_domains_reserved_suffixes": "",
        "mcp_public_domains_reassign_cooldown_hours": 168,
    }.items():
        monkeypatch.setattr(settings, name, value)
    dns = Dns()
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: dns)
    w = World(factory=factory, dns=dns, tag=tag, user=user, ws=ws_id, endpoints=endpoints)
    try:
        yield w
    finally:
        async with factory() as s:
            ids = [ws_id, *w.extra_ws]
            await s.execute(delete(PublicMcpDomain).where(PublicMcpDomain.workspace_id.in_(ids)))
            await s.execute(
                delete(AuditLog).where(
                    AuditLog.resource.in_([f"public_mcp_domain:{i}" for i in w.added])
                )
            )
            await s.execute(delete(Workspace).where(Workspace.id.in_(ids)))
            await s.execute(delete(User).where(User.user_id == user))
            await s.commit()


async def _permission(name: str) -> int:
    from api.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api-blue:8080"
    ) as c:
        r = await c.get(PERMISSION, params={"domain": name})
    return r.status_code


async def _alias(name: str) -> int:
    from api.main import app

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{name}"
    ) as c:
        r = await c.post("/mcp", json=body)
    return r.status_code


# ----------------------------------------------------------------------------
# Proof and lease over several runs
# ----------------------------------------------------------------------------


async def test_unavailable_for_a_week_keeps_state_and_lease(world: World) -> None:
    w = world
    row = await w.add(
        proof_ok_at=timedelta(days=-7),
        serve_until=timedelta(minutes=30),
        last_checked_at=timedelta(hours=-2),
    )
    w.dns.mode[row.hostname] = "unavailable"
    last = row.last_checked_at
    for _ in range(3):
        counts = await w.run()
        assert counts.dns_unavailable >= 1
        after = await w.row(row.id)
        assert after is not None
        assert after.state == "active"
        assert after.last_proof_result == "unavailable"
        assert after.proof_missing_checks == 0
        assert after.proof_ok_at == row.proof_ok_at
        assert after.last_checked_at is not None and after.last_checked_at > last
        assert after.serve_until is not None
        assert after.serve_until - after.last_checked_at == timedelta(hours=72)
        last = after.last_checked_at
        await w.age([row.id])
    assert await w.serves(row.hostname) == row.endpoint_id
    assert await w.audits(row.id) == []


async def test_missing_inside_the_grace_keeps_serving(world: World) -> None:
    w = world
    row = await w.add(proof_ok_at=timedelta(hours=-1))
    w.dns.mode[row.hostname] = "missing"
    for checks in range(1, 5):
        await w.run()
        after = await w.row(row.id)
        assert after is not None
        assert (after.state, after.proof_missing_checks) == ("active", checks)
        await w.age([row.id])
    assert await w.serves(row.hostname) == row.endpoint_id


async def test_proof_lost_after_three_absences_then_back(world: World) -> None:
    w = world
    row = await w.add(proof_ok_at=timedelta(days=-5))
    w.dns.mode[row.hostname] = "missing"
    for _ in range(2):
        await w.run()
        after = await w.row(row.id)
        assert after is not None and after.state == "active"
        assert await w.serves(row.hostname) == row.endpoint_id
        await w.age([row.id])

    counts = await w.run()
    assert counts.suspended >= 1
    after = await w.row(row.id)
    assert after is not None
    assert (after.state, after.suspended_reason) == ("suspended", "proof_lost")
    assert await w.serves(row.hostname) is None
    assert await _permission(row.hostname) == 404
    assert await _alias(row.hostname) == 404
    (audit,) = await w.audits(row.id)
    assert audit.action == "public_mcp_domain.suspended"
    assert audit.user_id == audit.user_email == SYSTEM
    assert audit.user_metadata["hostname_sha256"] == hostname_digest(row.hostname)

    # The record is back: the next check reinstates it.
    w.dns.mode[row.hostname] = "ok"
    await w.age([row.id])
    counts = await w.run()
    assert counts.reinstated >= 1
    after = await w.row(row.id)
    assert after is not None and after.state == "active" and after.suspended_reason is None
    assert await w.serves(row.hostname) == row.endpoint_id
    assert await _permission(row.hostname) == 200
    assert [a.action for a in await w.audits(row.id)] == [
        "public_mcp_domain.suspended",
        "public_mcp_domain.reinstated",
    ]


async def test_thirty_days_suspended_is_released_with_a_cooldown(world: World) -> None:
    w = world
    row = await w.add("suspended", suspended_at=timedelta(days=-31))
    w.dns.mode[row.hostname] = "missing"
    counts = await w.run()
    assert counts.released >= 1
    after = await w.row(row.id)
    assert after is not None and after.state == "released"
    assert after.released_at is not None and after.cooldown_until is not None
    assert after.cooldown_until - after.released_at == timedelta(hours=168)
    (audit,) = await w.audits(row.id)
    assert audit.action == "public_mcp_domain.released"
    assert audit.user_metadata["from_state"] == "suspended"


async def test_routing_lost_alone_changes_no_state(world: World) -> None:
    w = world
    row = await w.add()
    w.dns.routing[row.hostname] = "mismatch"
    await w.run()
    after = await w.row(row.id)
    assert after is not None
    assert (after.state, after.routing_status, after.last_proof_result) == (
        "active",
        "mismatch",
        "ok",
    )
    assert await w.serves(row.hostname) == row.endpoint_id


async def test_license_removed_suspends_at_the_next_check(world: World) -> None:
    w = world
    row = await w.add()
    await w.sql(
        "DELETE FROM workspace_integration_licenses WHERE workspace_id = :ws AND service_id = :sid",
        ws=w.ws,
        sid=DOMAIN_SERVICE_ID,
    )
    await w.run()
    after = await w.row(row.id)
    assert after is not None
    assert (after.state, after.suspended_reason) == ("suspended", "right_ended")
    assert await w.serves(row.hostname) is None


# ----------------------------------------------------------------------------
# The fence, the pool, the budget, overlapping runs
# ----------------------------------------------------------------------------


async def test_a_row_changed_during_the_lookup_is_fenced(world: World) -> None:
    w = world
    row = await w.add(proof_ok_at=timedelta(days=-5), proof_missing_checks=2)
    w.dns.mode[row.hostname] = "missing"

    async def owner_acts(hostname: str) -> None:
        if hostname == row.hostname:
            await w.sql(
                "UPDATE public_mcp_domains SET check_seq = check_seq + 1 WHERE id = :id",
                id=row.id,
            )

    w.dns.hook = owner_acts
    counts = await w.run()
    w.dns.hook = None
    assert counts.fenced >= 1
    after = await w.row(row.id)
    assert after is not None
    assert (after.state, after.proof_missing_checks) == ("active", 2)
    assert after.check_seq == row.check_seq + 1
    assert await w.audits(row.id) == []


async def test_the_lookup_holds_no_connection(world: World, async_engine) -> None:
    w = world
    rows = [await w.add() for _ in range(3)]
    for row in rows:
        w.dns.mode[row.hostname] = "missing"
    engine = create_async_engine(
        async_engine.url.render_as_string(hide_password=False),
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
    )
    one = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    got: list[str] = []

    async def take_the_connection(hostname: str) -> None:
        # The run's only connection must be free while a lookup runs.
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        got.append(hostname)

    w.dns.hook = take_the_connection
    try:
        counts = await maintenance.run_maintenance(one)
    finally:
        w.dns.hook = None
        await engine.dispose()
    assert {row.hostname for row in rows} <= set(got)
    assert counts.checked >= 3
    for row in rows:
        after = await w.row(row.id)
        assert after is not None and after.proof_missing_checks == 1


async def test_sixty_slow_rows_never_hold_up_a_missing_proof(world: World, monkeypatch) -> None:
    w = world
    monkeypatch.setattr(service, "CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(maintenance, "RUN_BUDGET_SECONDS", 1.2)
    # Indeterminate before, and older: by age alone they would come first.
    slow = [
        await w.add(last_proof_result="unavailable", last_checked_at=timedelta(hours=-3))
        for _ in range(60)
    ]
    for row in slow:
        w.dns.mode[row.hostname] = "slow"
    lost = await w.add(proof_ok_at=timedelta(days=-5), last_checked_at=timedelta(hours=-2))
    w.dns.mode[lost.hostname] = "missing"
    ids = [lost.id, *(row.id for row in slow)]
    slow_ids = {row.id for row in slow}

    async def last_checks() -> dict[UUID, datetime | None]:
        async with w.factory() as s:
            return dict(
                (
                    await s.execute(
                        select(PublicMcpDomain.id, PublicMcpDomain.last_checked_at).where(
                            PublicMcpDomain.id.in_(ids)
                        )
                    )
                ).all()
            )

    checked_before: set[UUID] = set()
    for run in (1, 2, 3):
        async with w.factory() as s:
            order = [item.id for item in await maintenance.due_domains(s) if item.id in set(ids)]
        # The missing proof first; the slow rows after it, and among them the
        # ones checked in the previous run last.
        assert order[0] == lost.id
        assert set(order[1:]) == slow_ids
        positions = {domain_id: i for i, domain_id in enumerate(order)}
        unchecked = [positions[i] for i in slow_ids - checked_before]
        rechecked = [positions[i] for i in checked_before]
        if unchecked and rechecked:
            assert max(unchecked) < min(rechecked)

        before = await last_checks()
        counts = await w.run()
        after = await last_checks()
        assert counts.budget_skipped > 0
        assert after[lost.id] is not None and after[lost.id] > (before[lost.id] or datetime.min)
        checked_before = {i for i in slow_ids if after[i] != before[i]}
        assert 0 < len(checked_before) < len(slow_ids)
        row = await w.row(lost.id)
        assert row is not None
        assert row.proof_missing_checks == run
        assert row.state == ("suspended" if run == 3 else "active")
        await w.age(ids)

    row = await w.row(lost.id)
    assert row is not None and row.suspended_reason == "proof_lost"
    assert await w.serves(lost.hostname) is None
    for slow_row in slow:
        after_row = await w.row(slow_row.id)
        assert after_row is not None and after_row.state == "active"
        assert after_row.proof_missing_checks == 0
        assert after_row.proof_ok_at == slow_row.proof_ok_at


async def test_two_overlapping_runs_process_no_transition_twice(world: World) -> None:
    w = world
    row = await w.add(proof_ok_at=timedelta(days=-5), proof_missing_checks=2)
    w.dns.mode[row.hostname] = "missing"
    orphan = await w.add(endpoint_id=uuid4())  # no such publication
    expired = await w.add("pending", created_at=timedelta(days=-8))
    w.dns.delay = 0.2
    try:
        first, second = await asyncio.gather(w.run(), w.run())
    finally:
        w.dns.delay = 0.0
    after = await w.row(row.id)
    assert after is not None
    assert (after.state, after.proof_missing_checks) == ("suspended", 3)
    assert [a.action for a in await w.audits(row.id)] == ["public_mcp_domain.suspended"]
    assert first.fenced + second.fenced >= 1
    assert [a.action for a in await w.audits(orphan.id)] == ["public_mcp_domain.released"]
    assert [a.action for a in await w.audits(expired.id)] == ["public_mcp_domain.released"]
    assert await w.row(expired.id) is None


# ----------------------------------------------------------------------------
# Housekeeping
# ----------------------------------------------------------------------------


async def test_housekeeping(world: World) -> None:
    w = world
    old_pending = await w.add("pending", created_at=timedelta(days=-8))
    new_pending = await w.add("pending", created_at=timedelta(days=-6))
    cooled = await w.add("released", cooldown_until=timedelta(seconds=-1))
    cooling = await w.add("released", cooldown_until=timedelta(hours=1))

    # A publication that was unpublished while the switch was off.
    revoked = w.endpoint()
    await w.sql("UPDATE public_mcp_endpoints SET revoked_at = now() WHERE id = :id", id=revoked)
    revoked_holder = await w.add(endpoint_id=revoked)
    revoked_two = w.endpoint()
    await w.sql("UPDATE public_mcp_endpoints SET revoked_at = now() WHERE id = :id", id=revoked_two)
    revoked_claim = await w.add("pending", endpoint_id=revoked_two)

    # A workspace that was deleted for good: the row is a tombstone.
    async with w.factory() as s:
        gone = Workspace(
            id=uuid4(),
            name=f"gone-{w.tag}",
            plan_name="promax",
            owner_user_id=w.user,
            daily_api_limit=1,
            weekly_api_limit=1,
        )
        s.add(gone)
        await s.commit()
        gone_id = gone.id
    w.extra_ws.append(gone_id)
    tomb = await w.add("suspended", workspace_id=gone_id, endpoint_id=uuid4())
    await w.sql("DELETE FROM workspaces WHERE id = :id", id=gone_id)
    assert await w.row(tomb.id) is not None

    counts = await w.run()
    assert counts.pending_expired >= 1
    assert counts.purged >= 1
    assert counts.orphans_released >= 3

    assert await w.row(old_pending.id) is None
    assert await w.row(new_pending.id) is not None
    assert await w.row(cooled.id) is None
    assert await w.row(cooling.id) is not None
    assert await w.row(revoked_claim.id) is None
    for held in (revoked_holder, tomb):
        after = await w.row(held.id)
        assert after is not None
        assert (after.state, after.suspended_reason) == ("released", None)
        assert after.released_at is not None and after.cooldown_until is not None
        assert after.cooldown_until - after.released_at == timedelta(hours=168)
        assert after.check_seq == held.check_seq + 1
        (audit,) = await w.audits(held.id)
        assert audit.action == "public_mcp_domain.released"
        assert audit.user_id == audit.user_email == SYSTEM
        meta = audit.user_metadata
        assert (meta["from_state"], meta["to_state"], meta["reason"]) == (
            held.state,
            "released",
            "orphaned",
        )
        assert meta["hostname_sha256"] == hostname_digest(held.hostname)
        assert held.hostname not in str(meta)
    (claim_audit,) = await w.audits(revoked_claim.id)
    assert claim_audit.user_metadata["from_state"] == "pending"
    assert claim_audit.user_metadata["reason"] == "orphaned"
    (expiry_audit,) = await w.audits(old_pending.id)
    assert expiry_audit.user_metadata["reason"] == "expired"
    assert await w.audits(cooled.id) == []

    # The tombstone's name is free for another workspace after the cooldown.
    await w.sql(
        "UPDATE public_mcp_domains SET cooldown_until = timezone('UTC', statement_timestamp()) "
        "- interval '1 second' WHERE id = :id",
        id=tomb.id,
    )
    await w.run()
    assert await w.row(tomb.id) is None


async def test_housekeeping_leaves_live_rows_alone(world: World) -> None:
    w = world
    active = await w.add(last_checked_at=timedelta(minutes=-1))
    pending = await w.add("pending")
    await w.run()
    for row in (active, pending):
        after = await w.row(row.id)
        assert after is not None and after.state == row.state
        assert after.check_seq == row.check_seq


async def test_a_failing_report_purge_never_stops_the_checks(world: World, monkeypatch) -> None:
    """#2941: the purge of expired certificate reports runs after the due checks, on its own.

    While reporting is configured, a purge that fails (here: a statement on a
    table that does not exist) logs one error event; the due row is still
    checked and the run returns its counts.
    """
    w = world
    monkeypatch.setattr(
        get_settings(), "mcp_public_domains_tls_reporter_token", "reporter-" + "p" * 40
    )
    monkeypatch.setattr(
        maintenance,
        "_PURGE_TLS_REPORTS",
        text("DELETE FROM public_mcp_domain_tls_reports_absent RETURNING 1"),
    )
    row = await w.add(last_checked_at=timedelta(hours=-2))
    with structlog.testing.capture_logs() as events:
        counts = await w.run()
    assert counts.checked >= 1
    after = await w.row(row.id)
    assert after is not None and after.last_checked_at > row.last_checked_at
    failures = [e for e in events if e["event"] == "public_mcp_domain_tls_report_purge_failed"]
    assert len(failures) == 1 and failures[0]["log_level"] == "error"
    assert [e for e in events if e["event"] == "public_mcp_domain_maintenance_run"]
