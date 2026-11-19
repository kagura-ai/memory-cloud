"""The owner's custom-domain commands of an installation on real OIDC and PostgreSQL (#2921).

Claim, verify and remove through the installation's registered browser client
with the canonical owner's ``integrations:manage`` token: who is admitted, the
binding and the named row, the duplicates and the retries, the shared limits,
the lookup without a connection, the slot suspension and the shapes that must
not change. The resolver is a scripted fake (``Dns``); the two owner counters
run on an in-memory counter. Every test uses its own host names and deletes
the domain rows of its workspaces (and their audit rows) at the end.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select, text, update

from auth.oidc_server import credential_hash
from config.settings import get_settings
from models.auth import AuditLog, Context, Workspace, WorkspaceMember
from models.integration_entitlement_schemas import IntegrationEntitlementPush
from models.oidc import OIDCGrant, OIDCToken
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_domain import DOMAIN_SERVICE_ID, PublicMcpDomain
from models.resource import Resource
from models.sync_installation import SyncService
from models.workspace_slots import WorkspaceSlotSuspension
from services import integration_entitlements
from services import public_mcp_domain_dns as domain_dns
from services import public_mcp_domains as lifecycle
from services import sync_publication_domain_commands as commands
from services.public_mcp_domain_dns import DomainCheck
from services.public_mcp_domains import PublicMcpDomainLimitReachedError
from tests.integration import sync_installation_fixtures as fixtures
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store

ACTIONS = ("claim", "verify", "remove")
BINDING = (
    "application_id",
    "resource_id",
    "resource_pk",
    "context_id",
    "endpoint_id",
    "public_id",
    "generation",
)
NOT_FOUND = b'{"error":"not_found"}'
STALE = {"error": "public_mcp_domain_command_stale"}


# ----------------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------------


class Dns:
    """Scripted resolver: ``ok`` when the token is published; ``during`` runs once inside a check."""

    def __init__(self) -> None:
        self.txt: dict[str, set[str]] = {}
        self.unavailable: set[str] = set()
        self.calls: list[str] = []
        self.during: Callable[[], Awaitable[None]] | None = None

    def publish(self, hostname: str, token: str) -> None:
        self.txt.setdefault(hostname, set()).add(token)

    def withdraw(self, hostname: str) -> None:
        self.txt.pop(hostname, None)

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck:
        self.calls.append(hostname)
        if self.during is not None:
            hook, self.during = self.during, None
            await hook()
        if hostname in self.unavailable:
            return DomainCheck("unavailable", "unresolved")
        return DomainCheck("ok" if token in self.txt.get(hostname, set()) else "missing", "ok")


def enable(monkeypatch, authority: str = "operator") -> None:
    settings = get_settings()
    for name, value in {
        "mcp_public_usage_budget_enabled": True,
        "mcp_public_domains_enabled": True,
        "mcp_public_domains_routing_target": "edge.example.com",
        "sync_publication_domain_operations_enabled": True,
        "mcp_public_domains_authority": authority,
        "mcp_public_domains_reassign_cooldown_hours": 168,
        "mcp_public_domains_max_per_workspace": 3,
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest_asyncio.fixture(loop_scope="session")
async def env(store, monkeypatch):
    dns = Dns()
    counts: dict[str, int] = {}

    async def incr(key: str, amount: int, ttl: int | None = None) -> int:
        counts[key] = counts.get(key, 0) + amount
        return counts[key]

    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: dns)
    monkeypatch.setattr(lifecycle, "incrby_counter", incr)
    enable(monkeypatch)
    holders: list[Any] = []
    try:
        yield SimpleNamespace(
            store=store, dns=dns, counts=counts, holders=holders, monkeypatch=monkeypatch
        )
    finally:
        for holder in holders:
            await holder.rollback()
            await holder.close()
        async with store.factory() as db:
            workspaces = [str(store.wid), str(store.other)]
            await db.execute(
                text(
                    "DELETE FROM audit_logs WHERE action LIKE 'public_mcp_domain.%' "
                    "AND user_metadata->>'workspace_id' IN (:a, :b)"
                ),
                {"a": workspaces[0], "b": workspaces[1]},
            )
            await db.execute(
                delete(PublicMcpDomain).where(
                    PublicMcpDomain.workspace_id.in_([store.wid, store.other])
                )
            )
            await db.commit()


def name(label: str = "shop") -> str:
    return f"{label}-{uuid4().hex[:10]}.example.org"


async def installed(store) -> dict:
    """Paid, approved and activated: the installation view (its binding included)."""
    await fixtures.paid(store)
    approval = await fixtures.approve(store, fixtures.approval_body(application_id=str(uuid4())))
    response = await fixtures.http(
        store, "POST", "sync-installations", body=fixtures.activation(approval)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def rights(store, revision: int, *, domains: bool) -> None:
    """Publish the workspace's rights: the sync license, and the domain license or not."""
    window = {
        "valid_from": (store.clock.now - timedelta(hours=1)).replace(tzinfo=UTC),
        "valid_until": (store.clock.now + timedelta(days=14)).replace(tzinfo=UTC),
        "evidence_ref": "confirmed-term",
    }
    services = [{"service_id": store.service_id, **window}]
    if domains:
        services.append({"service_id": DOMAIN_SERVICE_ID, **window})
    body = IntegrationEntitlementPush(
        revision=revision,
        event_id=f"paid-{revision}",
        source_ref="billing-snapshot",
        max_access=window,
        services=services,
    )
    async with store.factory() as db:
        await integration_entitlements.apply_integration_entitlements(db, store.wid, body)
        await db.commit()


def binding(source: dict, **changes: Any) -> dict:
    return {**{key: source[key] for key in BINDING}, **changes}


def claim_body(target: dict, hostname: str, **changes: Any) -> dict:
    return {"expected": binding(target, **changes), "hostname": hostname}


def row_body(target: dict, domain_id: str | UUID, state: str, **changes: Any) -> dict:
    return {
        "expected": binding(target, **changes),
        "domain": {"domain_id": str(domain_id), "state": state},
    }


def domain_path(target: dict, action: str | None = None) -> str:
    suffix = f"/{action}" if action else ""
    return f"sync-installations/{target['installation_id']}/publication-domains{suffix}"


async def _request(store, method: str, path: str, token: str, body: Any = None) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=store.app), base_url="https://test"
    ) as client:
        return await client.request(
            method, path, json=body, headers={"Authorization": "Bearer " + token}
        )


async def command(
    store,
    target: dict,
    action: str,
    body: dict,
    *,
    role: str = "owner",
    token: str | None = None,
    workspace: UUID | None = None,
    installation: str | None = None,
) -> Response:
    selected = {**target, "installation_id": installation or target["installation_id"]}
    path = f"/api/v1/workspaces/{workspace or store.wid}/{domain_path(selected, action)}"
    return await _request(store, "POST", path, token or store.tokens[role], body)


async def observe(store, target: dict, *, role: str = "owner", token: str | None = None):
    path = f"/api/v1/workspaces/{store.wid}/{domain_path(target)}"
    return await _request(store, "GET", path, token or store.tokens[role])


async def token_for(
    store, role: str, *, client_id: str | None = None, scope: str | None = None
) -> str:
    """A fresh grant and access token for ``role`` (another client or scope)."""
    scope = scope or "openid workspaces:read integrations:manage"
    now = store.clock.now
    async with store.factory() as db:
        grant = OIDCGrant(
            client_id=client_id or store.client_id,
            user_id=store.users[role],
            scope=scope,
            nonce=uuid4().hex,
            auth_time=int(now.replace(tzinfo=UTC).timestamp()),
            expires_at=now + timedelta(hours=8),
        )
        db.add(grant)
        await db.flush()
        token = "kmc_oidc_at_" + secrets.token_urlsafe(32)
        db.add(
            OIDCToken(
                grant_id=grant.id,
                access_token_hash=credential_hash(token),
                refresh_token_hash=credential_hash(secrets.token_urlsafe(32)),
                scope=scope,
                issued_at=grant.auth_time,
                expires_in=3600,
            )
        )
        await db.commit()
    return token


async def rows_of(store) -> list[PublicMcpDomain]:
    async with store.factory() as db:
        return list(
            (
                await db.execute(
                    select(PublicMcpDomain)
                    .where(PublicMcpDomain.workspace_id == store.wid)
                    .order_by(PublicMcpDomain.created_at)
                )
            ).scalars()
        )


async def row_of(store, domain_id: str | UUID) -> PublicMcpDomain | None:
    async with store.factory() as db:
        return await db.get(PublicMcpDomain, UUID(str(domain_id)))


async def token_of(store, domain_id: str | UUID) -> str:
    row = await row_of(store, domain_id)
    assert row is not None
    return row.verification_token


async def audits(store, domain_id: str | UUID) -> list[tuple[str, str, dict]]:
    async with store.factory() as db:
        rows = (
            await db.execute(
                select(AuditLog.action, AuditLog.user_id, AuditLog.user_metadata)
                .where(AuditLog.resource == f"public_mcp_domain:{domain_id}")
                .order_by(AuditLog.id)
            )
        ).all()
    return [(action, user_id, meta) for action, user_id, meta in rows]


async def claimed(env, target: dict, label: str = "shop") -> tuple[str, str]:
    hostname = name(label)
    response = await command(env.store, target, "claim", claim_body(target, hostname))
    assert response.status_code == 200, response.text
    return hostname, response.json()["domain"]["domain_id"]


async def activated(env, target: dict, label: str = "shop") -> tuple[str, str]:
    hostname, domain_id = await claimed(env, target, label)
    env.dns.publish(hostname, await token_of(env.store, domain_id))
    response = await command(env.store, target, "verify", row_body(target, domain_id, "pending"))
    assert response.status_code == 200, response.text
    assert response.json()["domain"] == {"domain_id": domain_id, "state": "active"}
    return hostname, domain_id


async def second_publication(store) -> str:
    """Another public resource of the workspace, with its own live endpoint."""
    slug = "second-" + uuid4().hex[:12]
    async with store.factory() as db:
        resource = Resource(
            workspace_id=store.wid, resource_id=slug, created_by=store.users["owner"]
        )
        context = Context(
            id=uuid4(),
            workspace_id=store.wid,
            name=slug,
            created_by=store.users["owner"],
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
        db.add_all([resource, context])
        await db.flush()
        db.add(
            PublicMcpEndpoint(
                workspace_id=store.wid,
                context_id=context.id,
                resource_pk=resource.id,
                resource_id=slug,
                public_name="Second",
                created_by=store.users["owner"],
            )
        )
        await db.commit()
    return slug


def once_after(env, holder: Any, attribute: str, action: Callable[[], Awaitable[None]]) -> None:
    """Run ``action`` once, right after the first call of ``holder.attribute`` returns."""
    original = getattr(holder, attribute)
    fired: list[bool] = []

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        if not fired:
            fired.append(True)
            await action()
        return result

    env.monkeypatch.setattr(holder, attribute, wrapper)


def only_the_guard_checks_the_owner(env) -> None:
    """Neutralize the lifecycle's own plain owner read, so only the guard's admission can refuse.

    The pre-admission has already checked the owner; the lifecycle's read inside
    the write transaction would otherwise refuse a transfer too (as a 404).
    """

    async def always(*_args: Any) -> bool:
        return True

    env.monkeypatch.setattr(lifecycle, "_is_canonical_owner", always)


def verify_count(env) -> int:
    return env.counts.get(f"public_mcp_domain_verify:{env.store.wid}", 0)


def activation_count(env) -> int:
    return env.counts.get(f"public_mcp_domain_activate:{env.store.wid}", 0)


# ----------------------------------------------------------------------------
# The owner flow; read and management consent stay separate
# ----------------------------------------------------------------------------


async def _full_flow(env, target: dict) -> str:
    store = env.store
    hostname = name("flow")
    claim = await command(store, target, "claim", claim_body(target, hostname))
    assert claim.status_code == 200, claim.text
    receipt = claim.json()
    assert receipt["schema_version"] == "public-domain-command-v1"
    assert receipt["action"] == "claim" and receipt["domain"]["state"] == "pending"
    domain_id = receipt["domain"]["domain_id"]
    seen = (await observe(store, target)).json()["domain"]
    assert (seen["domain_id"], seen["state"]) == (domain_id, "pending")
    token = await token_of(store, domain_id)
    assert seen["verification"]["record_value"].endswith(token)

    body = row_body(target, domain_id, "pending")
    pending = await command(store, target, "verify", body)
    assert pending.status_code == 200, pending.text
    assert pending.json()["domain"] == {"domain_id": domain_id, "state": "pending"}
    env.dns.publish(hostname, token)
    active = await command(store, target, "verify", body)
    assert active.json() == {
        "schema_version": "public-domain-command-v1",
        "action": "verify",
        "domain": {"domain_id": domain_id, "state": "active"},
    }
    removed = await command(store, target, "remove", row_body(target, domain_id, "active"))
    assert removed.json() == {
        "schema_version": "public-domain-command-v1",
        "action": "remove",
        "domain": None,
    }
    for response in (claim, pending, active, removed):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
    row = await row_of(store, domain_id)
    assert row is not None and row.state == "released"
    return domain_id


async def test_owner_claims_verifies_and_removes_through_scoped_oidc(env):
    store = env.store
    target = await installed(store)
    domain_id = await _full_flow(env, target)
    rows = await audits(store, domain_id)
    assert [action for action, _, _ in rows] == [
        "public_mcp_domain.attached",
        "public_mcp_domain.verified",
        "public_mcp_domain.detached",
    ]
    for _action, user_id, meta in rows:
        assert user_id == store.users["owner"]
        assert meta["client_id"] == store.client_id
        assert meta["installation_id"] == target["installation_id"]


async def test_the_same_flow_under_the_entitlement_authority(env):
    store = env.store
    enable(env.monkeypatch, "entitlement")
    target = await installed(store)  # the sync license only
    refused = await command(store, target, "claim", claim_body(target, name("unlicensed")))
    assert (refused.status_code, refused.json()) == (
        403,
        {"error": "public_mcp_domain_not_entitled"},
    )
    assert await rows_of(store) == []
    await rights(store, 2, domains=True)
    await _full_flow(env, target)


async def test_a_read_token_is_refused_on_every_command(env):
    store = env.store
    target = await installed(store)
    reader = await token_for(store, "owner", scope="openid workspaces:read")
    bodies = {
        "claim": claim_body(target, name("read")),
        "verify": row_body(target, uuid4(), "pending"),
        "remove": row_body(target, uuid4(), "pending"),
    }
    for action, body in bodies.items():
        response = await command(store, target, action, body, token=reader)
        assert (response.status_code, response.json()) == (403, {"error": "insufficient_scope"})
    assert (await observe(store, target, token=reader)).status_code == 200
    assert await rows_of(store) == []


async def test_member_admin_viewer_and_outsider_get_one_404(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    async with store.factory() as db:
        await db.execute(
            update(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == store.wid,
                WorkspaceMember.user_id == store.users["next"],
            )
            .values(role="owner")
        )
        await db.commit()
    bodies = {
        "claim": claim_body(target, name("role")),
        "verify": row_body(target, domain_id, "pending"),
        "remove": row_body(target, domain_id, "pending"),
    }
    for role in ("member", "admin", "viewer", "outsider", "next"):
        for action, body in bodies.items():
            response = await command(store, target, action, body, role=role)
            assert (response.status_code, response.content) == (404, NOT_FOUND), (role, action)
    rows = await rows_of(store)
    assert [(str(row.id), row.state, row.check_seq) for row in rows] == [(domain_id, "pending", 0)]
    assert env.counts == {} and env.dns.calls == []


async def test_another_workspace_or_installation_is_404(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    bodies = {
        "claim": claim_body(target, name("elsewhere")),
        "verify": row_body(target, domain_id, "pending"),
        "remove": row_body(target, domain_id, "pending"),
    }
    for action, body in bodies.items():
        for selection in (
            {"workspace": store.other},
            {"installation": str(uuid4())},
            {"workspace": store.other, "installation": str(uuid4())},
        ):
            response = await command(store, target, action, body, **selection)
            assert (response.status_code, response.content) == (404, NOT_FOUND), selection
    assert [str(row.id) for row in await rows_of(store)] == [domain_id]


async def test_a_token_of_another_client_cannot_address_the_installation(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    foreign = await token_for(store, "owner", client_id=store.foreign_client)
    bodies = {
        "claim": claim_body(target, name("foreign")),
        "verify": row_body(target, domain_id, "pending"),
        "remove": row_body(target, domain_id, "pending"),
    }
    for action, body in bodies.items():
        response = await command(store, target, action, body, token=foreign)
        assert (response.status_code, response.json()) == (409, {"error": "RES-002"}), action
    rows = await rows_of(store)
    assert [(str(row.id), row.state, row.check_seq) for row in rows] == [(domain_id, "pending", 0)]


# ----------------------------------------------------------------------------
# Ownership change
# ----------------------------------------------------------------------------


async def test_the_previous_owner_is_refused_and_the_new_owner_acts(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    await fixtures.transfer(store, "next")
    bodies = {
        "claim": claim_body(target, name("previous")),
        "verify": row_body(target, domain_id, "pending"),
        "remove": row_body(target, domain_id, "pending"),
    }
    for action, body in bodies.items():
        response = await command(store, target, action, body)
        assert (response.status_code, response.content) == (404, NOT_FOUND), action

    current = (await observe(store, target, role="next")).json()
    verified = await command(
        store, current, "verify", row_body(current, domain_id, "pending"), role="next"
    )
    assert verified.status_code == 200, verified.text
    removed = await command(
        store, current, "remove", row_body(current, domain_id, "pending"), role="next"
    )
    assert removed.status_code == 200, removed.text
    claim = await command(store, current, "claim", claim_body(current, name("next")), role="next")
    assert claim.status_code == 200, claim.text
    new_id = claim.json()["domain"]["domain_id"]
    assert [user for _, user, _ in await audits(store, new_id)] == [store.users["next"]]


async def test_a_transfer_during_the_lookup_discards_the_result(env):
    store = env.store
    target = await installed(store)
    hostname, domain_id = await claimed(env, target)
    env.dns.publish(hostname, await token_of(store, domain_id))

    async def transfer() -> None:
        await fixtures.transfer(store, "next")

    env.dns.during = transfer
    only_the_guard_checks_the_owner(env)
    response = await command(store, target, "verify", row_body(target, domain_id, "pending"))
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    row = await row_of(store, domain_id)
    assert row is not None and (row.state, row.check_seq) == ("pending", 0)
    assert [action for action, _, _ in await audits(store, domain_id)] == [
        "public_mcp_domain.attached"
    ]


@pytest.mark.parametrize("action", ACTIONS)
async def test_a_transfer_after_admission_changes_nothing(env, action):
    """After the pre-admission and the lifecycle's plain reads: refused by the guard's admission."""
    store = env.store
    target = await installed(store)
    domain_id = None
    if action != "claim":
        _hostname, domain_id = await activated(env, target)
    before = [(str(row.id), row.state, row.check_seq) for row in await rows_of(store)]
    audited = len(await audits(store, domain_id)) if domain_id else 0

    async def transfer() -> None:
        await fixtures.transfer(store, "next")

    once_after(env, lifecycle, "owner_actor", transfer)
    only_the_guard_checks_the_owner(env)
    body = (
        claim_body(target, name("late"))
        if action == "claim"
        else row_body(target, domain_id, "active")
    )
    response = await command(store, target, action, body)
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    assert [(str(row.id), row.state, row.check_seq) for row in await rows_of(store)] == before
    if domain_id:
        assert len(await audits(store, domain_id)) == audited


async def test_a_transfer_away_and_back_during_a_command_is_stale(env):
    store = env.store
    target = await installed(store)

    async def away_and_back() -> None:
        await fixtures.transfer(store, "next")
        await fixtures.transfer(store, "owner")

    once_after(env, commands, "_precheck", away_and_back)
    response = await command(store, target, "claim", claim_body(target, name("epoch")))
    assert (response.status_code, response.json()) == (409, STALE)
    assert await rows_of(store) == []


async def test_a_transfer_away_and_back_before_the_command_is_judged_on_state(env):
    """The defined behaviour: the owner again, the binding unchanged, the command applies."""
    store = env.store
    target = await installed(store)
    await fixtures.transfer(store, "next")
    await fixtures.transfer(store, "owner")
    response = await command(store, target, "claim", claim_body(target, name("back")))
    assert response.status_code == 200, response.text
    assert len(await rows_of(store)) == 1


async def test_a_lost_owner_is_answered_before_a_lost_right(env):
    """Entitlement, the right ended: a refused paid entry still admits the caller first."""
    store = env.store
    enable(env.monkeypatch, "entitlement")
    target = await installed(store)
    await rights(store, 2, domains=True)
    hostname, domain_id = await claimed(env, target)
    await rights(store, 3, domains=False)
    env.dns.publish(hostname, await token_of(store, domain_id))
    body = row_body(target, domain_id, "pending")
    refused = await command(store, target, "verify", body)
    assert (refused.status_code, refused.json()) == (
        403,
        {"error": "public_mcp_domain_not_entitled"},
    )

    async def transfer() -> None:
        await fixtures.transfer(store, "next")

    env.dns.during = transfer
    response = await command(store, target, "verify", body)
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    row = await row_of(store, domain_id)
    assert row is not None and row.state == "pending"
    assert activation_count(env) == 0


# ----------------------------------------------------------------------------
# Selection, installation mismatch and stale commands
# ----------------------------------------------------------------------------

_CHANGED: dict[str, Callable[[Any], Any]] = {
    "application_id": lambda value: value + "-x",
    "resource_id": lambda value: value + "-x",
    "resource_pk": lambda _value: str(uuid4()),
    "context_id": lambda _value: str(uuid4()),
    "endpoint_id": lambda _value: str(uuid4()),
    "public_id": lambda _value: "pmcp_ZzZzZzZzZzZzZzZzZzZzZz",
    "generation": lambda value: value + 1,
}


@pytest.mark.parametrize("field", BINDING)
async def test_each_binding_field_is_compared(env, field):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    changed = {field: _CHANGED[field](target[field])}
    for action, body in (
        ("claim", claim_body(target, name("bind"), **changed)),
        ("verify", row_body(target, domain_id, "pending", **changed)),
        ("remove", row_body(target, domain_id, "pending", **changed)),
    ):
        response = await command(store, target, action, body)
        assert (response.status_code, response.json()) == (409, STALE), (field, action)
    rows = await rows_of(store)
    assert [(str(row.id), row.state, row.check_seq) for row in rows] == [(domain_id, "pending", 0)]
    assert env.counts == {} and env.dns.calls == []


async def test_a_generation_change_makes_earlier_commands_stale(env):
    """Unlink advances the generation; the revoked installation's current binding is admitted."""
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    unlinked = await fixtures.http(
        store, "DELETE", f"sync-installations/{target['installation_id']}"
    )
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json()["state"] == "revoked"
    current = (await observe(store, target)).json()
    assert current["generation"] == target["generation"] + 1

    for action, body in (
        ("claim", claim_body(target, name("old"))),
        ("verify", row_body(target, domain_id, "pending")),
        ("remove", row_body(target, domain_id, "pending")),
    ):
        response = await command(store, target, action, body)
        assert (response.status_code, response.json()) == (409, STALE), action
    assert env.counts == {}

    verified = await command(store, current, "verify", row_body(current, domain_id, "pending"))
    assert verified.json()["domain"] == {"domain_id": domain_id, "state": "pending"}
    removed = await command(store, current, "remove", row_body(current, domain_id, "pending"))
    assert (removed.status_code, removed.json()["domain"]) == (200, None)
    claim = await command(store, current, "claim", claim_body(current, name("new")))
    assert claim.status_code == 200, claim.text


async def test_a_replaced_publication_is_404_and_never_adopted(env):
    store = env.store
    target = await installed(store)
    async with store.factory() as db:
        old = await db.get(PublicMcpEndpoint, UUID(target["endpoint_id"]))
        assert old is not None
        old.revoked_at = utcnow()
        await db.flush()
        replacement = PublicMcpEndpoint(
            workspace_id=old.workspace_id,
            context_id=old.context_id,
            resource_pk=old.resource_pk,
            resource_id=old.resource_id,
            public_name=old.public_name,
            created_by=old.created_by,
        )
        db.add(replacement)
        await db.commit()
        new_id = str(replacement.id)
    counted = await fixtures.targets(store)
    for endpoint_id in (target["endpoint_id"], new_id):
        response = await command(
            store, target, "claim", claim_body(target, name("moved"), endpoint_id=endpoint_id)
        )
        assert (response.status_code, response.content) == (404, NOT_FOUND), endpoint_id
    assert await rows_of(store) == []
    assert await fixtures.targets(store) == counted


async def test_verify_and_remove_need_the_named_row_in_the_named_state(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    slug = await second_publication(store)
    async with store.factory() as db:
        elsewhere = await db.scalar(
            select(PublicMcpEndpoint.id).where(
                PublicMcpEndpoint.workspace_id == store.wid, PublicMcpEndpoint.resource_id == slug
            )
        )
        sibling = PublicMcpDomain(
            hostname=name("sibling"),
            workspace_id=store.wid,
            endpoint_id=elsewhere,
            state="pending",
            verification_token="1" * 64,
        )
        db.add(sibling)
        await db.commit()
    named = [(uuid4(), "pending"), (domain_id, "active"), (sibling.id, "pending")]
    for row_id, state in named:
        for action in ("verify", "remove"):
            response = await command(store, target, action, row_body(target, row_id, state))
            assert (response.status_code, response.json()) == (409, STALE), (row_id, action)
    for row_id in (domain_id, sibling.id):
        row = await row_of(store, row_id)
        assert row is not None and (row.state, row.check_seq) == ("pending", 0)

    # A row of another workspace on this very publication (its endpoint id):
    # only the workspace condition of the row read can refuse it. The
    # publication has no open domain of its own first (one per endpoint).
    removed = await command(store, target, "remove", row_body(target, domain_id, "pending"))
    assert removed.status_code == 200, removed.text
    async with store.factory() as db:
        foreign = PublicMcpDomain(
            hostname=name("foreign"),
            workspace_id=store.other,
            endpoint_id=UUID(target["endpoint_id"]),
            state="pending",
            verification_token="2" * 64,
        )
        db.add(foreign)
        await db.commit()
    for action in ("verify", "remove"):
        response = await command(store, target, action, row_body(target, foreign.id, "pending"))
        assert (response.status_code, response.json()) == (409, STALE), action
    row = await row_of(store, foreign.id)
    assert row is not None and (row.workspace_id, row.state, row.check_seq) == (
        store.other,
        "pending",
        0,
    )
    assert env.counts == {} and env.dns.calls == []


async def test_a_changed_service_registration_is_404(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    await fixtures.change(
        store, SyncService, store.service_id, service_client_id="replacement-" + uuid4().hex
    )
    for action, body in (
        ("claim", claim_body(target, name("service"))),
        ("verify", row_body(target, domain_id, "pending")),
        ("remove", row_body(target, domain_id, "pending")),
    ):
        response = await command(store, target, action, body)
        assert (response.status_code, response.content) == (404, NOT_FOUND), action
    assert [str(row.id) for row in await rows_of(store)] == [domain_id]


@pytest.mark.parametrize("action", ACTIONS)
async def test_an_unlink_after_admission_makes_the_command_stale(env, action):
    store = env.store
    target = await installed(store)
    domain_id = None
    if action != "claim":
        _hostname, domain_id = await claimed(env, target)

    async def unlink() -> None:
        response = await fixtures.http(
            store, "DELETE", f"sync-installations/{target['installation_id']}"
        )
        assert response.status_code == 200, response.text

    once_after(env, commands, "_precheck", unlink)
    body = (
        claim_body(target, name("unlinked"))
        if action == "claim"
        else row_body(target, domain_id, "pending")
    )
    response = await command(store, target, action, body)
    assert (response.status_code, response.json()) == (409, STALE)
    rows = await rows_of(store)
    if action == "claim":
        assert rows == []
    else:
        assert [(str(row.id), row.state, row.check_seq) for row in rows] == [
            (domain_id, "pending", 0)
        ]


@pytest.mark.parametrize("action", ACTIONS)
async def test_every_admission_reads_the_installation_row_again(env, action):
    """The write admission compares the binding with the row, not with an instance in memory.

    The installation the pre-admission loaded stays referenced (and so in the
    session's identity map) while another session unlinks it: the write
    admission must still see the new generation.
    """
    store = env.store
    target = await installed(store)
    domain_id = None
    if action != "claim":
        _hostname, domain_id = await claimed(env, target)
    held: list[Any] = []
    original = commands.bound_installation

    async def keep(*args: Any, **kwargs: Any) -> Any:
        selected = await original(*args, **kwargs)
        held.append(selected)
        return selected

    env.monkeypatch.setattr(commands, "bound_installation", keep)

    async def unlink() -> None:
        response = await fixtures.http(
            store, "DELETE", f"sync-installations/{target['installation_id']}"
        )
        assert response.status_code == 200, response.text

    once_after(env, commands, "_precheck", unlink)
    body = (
        claim_body(target, name("cached"))
        if action == "claim"
        else row_body(target, domain_id, "pending")
    )
    response = await command(store, target, action, body)
    assert (response.status_code, response.json()) == (409, STALE)
    # The pre-admission's selection and the write admission's, the latter at the new generation.
    assert len(held) >= 2 and held[-1][1].generation == target["generation"] + 1
    rows = await rows_of(store)
    if action == "claim":
        assert rows == []
    else:
        assert [(str(row.id), row.state, row.check_seq) for row in rows] == [
            (domain_id, "pending", 0)
        ]


# ----------------------------------------------------------------------------
# Duplicate submission and the retry hazards
# ----------------------------------------------------------------------------


async def test_a_duplicate_claim_keeps_one_row_and_its_token(env):
    store = env.store
    target = await installed(store)
    body = claim_body(target, name("twice"))
    first = await command(store, target, "claim", body)
    assert first.status_code == 200, first.text
    record = (await observe(store, target)).json()["domain"]["verification"]["record_value"]
    again = await command(store, target, "claim", body)
    assert (again.status_code, again.json()) == (409, {"error": "public_mcp_domain_exists"})
    assert len(await rows_of(store)) == 1
    after = (await observe(store, target)).json()["domain"]["verification"]["record_value"]
    assert after == record


async def test_two_identical_claims_at_once_create_one_row(env):
    store = env.store
    target = await installed(store)
    body = claim_body(target, name("race"))
    answers = await asyncio.gather(
        command(store, target, "claim", body), command(store, target, "claim", body)
    )
    assert sorted(answer.status_code for answer in answers) == [200, 409]
    lost = next(answer for answer in answers if answer.status_code == 409)
    assert lost.json()["error"] in {"public_mcp_domain_exists", "RES-002"}
    assert len(await rows_of(store)) == 1


async def test_a_retried_remove_cannot_detach_a_newer_claim(env):
    store = env.store
    target = await installed(store)
    hostname, first = await claimed(env, target)
    remove = row_body(target, first, "pending")
    assert (await command(store, target, "remove", remove)).status_code == 200
    again = await command(store, target, "claim", claim_body(target, hostname))
    assert again.status_code == 200, again.text
    newer = again.json()["domain"]["domain_id"]
    token = await token_of(store, newer)
    retried = await command(store, target, "remove", remove)
    assert (retried.status_code, retried.json()) == (409, STALE)
    row = await row_of(store, newer)
    assert row is not None and (row.state, row.verification_token) == ("pending", token)


async def test_a_retried_remove_of_a_released_domain_leaves_the_newer_active_one(env):
    store = env.store
    target = await installed(store)
    hostname, first = await activated(env, target)
    remove = row_body(target, first, "active")
    assert (await command(store, target, "remove", remove)).status_code == 200
    again = await command(store, target, "claim", claim_body(target, hostname))
    newer = again.json()["domain"]["domain_id"]
    env.dns.publish(hostname, await token_of(store, newer))
    verified = await command(store, target, "verify", row_body(target, newer, "pending"))
    assert verified.json()["domain"] == {"domain_id": newer, "state": "active"}
    retried = await command(store, target, "remove", remove)
    assert (retried.status_code, retried.json()) == (409, STALE)
    row = await row_of(store, newer)
    assert row is not None and row.state == "active"


async def test_a_new_claim_needs_a_new_txt_value(env):
    store = env.store
    target = await installed(store)
    hostname, first = await claimed(env, target)
    first_token = await token_of(store, first)
    assert (
        await command(store, target, "remove", row_body(target, first, "pending"))
    ).status_code == 200
    again = await command(store, target, "claim", claim_body(target, hostname))
    second = again.json()["domain"]["domain_id"]
    second_token = await token_of(store, second)
    assert second_token != first_token
    env.dns.publish(hostname, first_token)
    body = row_body(target, second, "pending")
    assert (await command(store, target, "verify", body)).json()["domain"]["state"] == "pending"
    env.dns.publish(hostname, second_token)
    assert (await command(store, target, "verify", body)).json()["domain"]["state"] == "active"


async def test_a_duplicate_verify_after_activation_is_stale_and_uncounted(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await activated(env, target)
    counted, looked_up = verify_count(env), len(env.dns.calls)
    duplicate = await command(store, target, "verify", row_body(target, domain_id, "pending"))
    assert (duplicate.status_code, duplicate.json()) == (409, STALE)
    assert (verify_count(env), len(env.dns.calls)) == (counted, looked_up)


async def test_a_repeated_verify_of_a_pending_domain_is_a_new_counted_check(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    body = row_body(target, domain_id, "pending")
    for expected in (1, 2):
        response = await command(store, target, "verify", body)
        assert response.json()["domain"] == {"domain_id": domain_id, "state": "pending"}
        assert (verify_count(env), len(env.dns.calls)) == (expected, expected)
    row = await row_of(store, domain_id)
    assert row is not None and row.check_seq == 2


async def test_two_identical_verifies_at_once_apply_one_result(env):
    store = env.store
    target = await installed(store)
    hostname, domain_id = await claimed(env, target)
    env.dns.publish(hostname, await token_of(store, domain_id))
    body = row_body(target, domain_id, "pending")
    answers = await asyncio.gather(
        command(store, target, "verify", body), command(store, target, "verify", body)
    )
    assert sorted(answer.status_code for answer in answers) == [200, 409]
    lost = next(answer for answer in answers if answer.status_code == 409)
    assert lost.json()["error"] in {
        "public_mcp_domain_changed",
        "public_mcp_domain_command_stale",
        "RES-002",
    }
    row = await row_of(store, domain_id)
    assert row is not None and row.state == "active"
    verified = [a for a, _, _ in await audits(store, domain_id) if a.endswith(".verified")]
    assert len(verified) == 1


async def test_two_identical_removes_at_once_detach_once(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await activated(env, target)
    body = row_body(target, domain_id, "active")
    answers = await asyncio.gather(
        command(store, target, "remove", body), command(store, target, "remove", body)
    )
    assert sorted(answer.status_code for answer in answers) == [200, 409]
    lost = next(answer for answer in answers if answer.status_code == 409)
    assert lost.json()["error"] in {"public_mcp_domain_command_stale", "RES-002"}
    detached = [a for a, _, _ in await audits(store, domain_id) if a.endswith(".detached")]
    assert len(detached) == 1
    row = await row_of(store, domain_id)
    assert row is not None and row.state == "released"


async def test_a_claim_resent_after_its_removal_creates_a_new_pending_claim(env):
    store = env.store
    target = await installed(store)
    body = claim_body(target, name("resent"))
    first = (await command(store, target, "claim", body)).json()["domain"]["domain_id"]
    first_token = await token_of(store, first)
    removed = await command(store, target, "remove", row_body(target, first, "pending"))
    assert removed.status_code == 200
    resent = await command(store, target, "claim", body)
    assert resent.status_code == 200, resent.text
    second = resent.json()["domain"]
    assert second["state"] == "pending" and second["domain_id"] != first
    assert await token_of(store, second["domain_id"]) != first_token
    # It holds the publication's one open domain ...
    other = await command(store, target, "claim", claim_body(target, name("another")))
    assert (other.status_code, other.json()) == (409, {"error": "public_mcp_domain_exists"})
    # ... and counts toward the workspace's limit.
    env.monkeypatch.setattr(get_settings(), "mcp_public_domains_max_per_workspace", 1)
    slug = await second_publication(store)
    with pytest.raises(PublicMcpDomainLimitReachedError):
        async with store.factory() as db:
            await lifecycle.attach_domain(db, store.users["owner"], store.wid, slug, name("limit"))
    stale = await command(store, target, "verify", row_body(target, first, "pending"))
    assert (stale.status_code, stale.json()) == (409, STALE)


async def test_a_verify_whose_check_releases_the_domain_answers_null(env):
    store = env.store
    target = await installed(store)
    hostname, domain_id = await activated(env, target)
    async with store.factory() as db:
        await db.execute(
            text(
                "UPDATE public_mcp_domains SET state = 'suspended', "
                "suspended_reason = 'proof_lost', serve_until = NULL, "
                "suspended_at = timezone('UTC', statement_timestamp()) - interval '31 days' "
                "WHERE id = :id"
            ),
            {"id": domain_id},
        )
        await db.commit()
    env.dns.withdraw(hostname)
    body = row_body(target, domain_id, "suspended")
    response = await command(store, target, "verify", body)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "schema_version": "public-domain-command-v1",
        "action": "verify",
        "domain": None,
    }
    row = await row_of(store, domain_id)
    assert row is not None and row.state == "released" and row.cooldown_until is not None
    released = [r for r in await audits(store, domain_id) if r[0] == "public_mcp_domain.released"]
    assert len(released) == 1
    _action, user_id, meta = released[0]
    assert user_id == store.users["owner"]
    assert (meta["client_id"], meta["installation_id"]) == (
        store.client_id,
        target["installation_id"],
    )
    counted = verify_count(env)
    again = await command(store, target, "verify", body)
    assert (again.status_code, again.json()) == (409, STALE)
    released = [r for r in await audits(store, domain_id) if r[0] == "public_mcp_domain.released"]
    assert len(released) == 1 and verify_count(env) == counted


# ----------------------------------------------------------------------------
# Limits and abuse
# ----------------------------------------------------------------------------


async def test_the_workspace_limits_are_shared_with_the_owner_api(env):
    store = env.store
    target = await installed(store)
    env.monkeypatch.setattr(get_settings(), "mcp_public_domains_max_per_workspace", 1)
    slug = await second_publication(store)
    async with store.factory() as db:
        await lifecycle.attach_domain(db, store.users["owner"], store.wid, slug, name("owner-api"))
    limited = await command(store, target, "claim", claim_body(target, name("limited")))
    assert (limited.status_code, limited.json()) == (
        409,
        {"error": "public_mcp_domain_limit_reached"},
    )
    env.monkeypatch.setattr(get_settings(), "mcp_public_domains_max_per_workspace", 3)
    _hostname, domain_id = await claimed(env, target)
    env.counts[f"public_mcp_domain_verify:{store.wid}"] = 20  # the owner API's 20 this hour
    response = await command(store, target, "verify", row_body(target, domain_id, "pending"))
    assert (response.status_code, response.json()) == (429, {"error": "RATE-001"})
    assert response.headers["retry-after"].isdigit()
    assert response.headers["cache-control"] == "no-store"
    assert env.dns.calls == []


async def test_a_refused_caller_consumes_no_budget_and_starts_no_lookup(env):
    store = env.store
    target = await installed(store)
    hostname, domain_id = await claimed(env, target)
    env.dns.publish(hostname, await token_of(store, domain_id))
    for role in ("member", "admin", "outsider"):
        response = await command(
            store, target, "verify", row_body(target, domain_id, "pending"), role=role
        )
        assert (response.status_code, response.content) == (404, NOT_FOUND)
    assert env.counts == {} and env.dns.calls == []
    row = await row_of(store, domain_id)
    assert row is not None and (row.state, row.check_seq) == ("pending", 0)


async def test_an_indeterminate_lookup_writes_nothing(env):
    store = env.store
    target = await installed(store)
    hostname, domain_id = await claimed(env, target)
    env.dns.unavailable.add(hostname)
    response = await command(store, target, "verify", row_body(target, domain_id, "pending"))
    assert (response.status_code, response.json()) == (
        503,
        {"error": "public_mcp_domain_verification_unavailable"},
    )
    row = await row_of(store, domain_id)
    assert row is not None and (row.state, row.check_seq, row.last_checked_at) == (
        "pending",
        0,
        None,
    )


async def test_a_token_that_expires_during_the_lookup_writes_nothing(env):
    store = env.store
    target = await installed(store)
    hostname, domain_id = await claimed(env, target)
    env.dns.publish(hostname, await token_of(store, domain_id))

    async def expire() -> None:
        store.clock.now = store.clock.now + timedelta(hours=2)

    env.dns.during = expire
    response = await command(store, target, "verify", row_body(target, domain_id, "pending"))
    assert (response.status_code, response.json()) == (401, {"error": "invalid_token"})
    row = await row_of(store, domain_id)
    assert row is not None and (row.state, row.check_seq) == ("pending", 0)
    assert activation_count(env) == 0


async def test_a_busy_workspace_at_activation_counts_nothing(env):
    """The activation is counted after the write transaction's admission: a busy row counts nothing."""
    store = env.store
    target = await installed(store)
    hostname, domain_id = await claimed(env, target)
    env.dns.publish(hostname, await token_of(store, domain_id))

    async def hold_the_workspace() -> None:
        holder = store.factory()
        env.holders.append(holder)
        await holder.execute(select(Workspace).where(Workspace.id == store.wid).with_for_update())

    env.dns.during = hold_the_workspace
    response = await command(store, target, "verify", row_body(target, domain_id, "pending"))
    assert (response.status_code, response.json()) == (409, {"error": "RES-002"})
    assert activation_count(env) == 0 and verify_count(env) == 1
    row = await row_of(store, domain_id)
    assert row is not None and (row.state, row.check_seq) == ("pending", 0)


# ----------------------------------------------------------------------------
# The lifecycle's own answers through the commands
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("authority", ["operator", "entitlement"])
async def test_a_counter_failure_inside_the_activation_transaction_writes_nothing(env, authority):
    """The activation is counted inside the guarded write: a counter failure rolls it back (503)."""
    store = env.store
    enable(env.monkeypatch, authority)
    target = await installed(store)
    if authority == "entitlement":
        await rights(store, 2, domains=True)
    hostname, domain_id = await claimed(env, target)
    env.dns.publish(hostname, await token_of(store, domain_id))
    counting = lifecycle.incrby_counter

    async def failing(key: str, amount: int, ttl: int | None = None) -> int:
        if key.startswith("public_mcp_domain_activate:"):
            raise ConnectionError("counter store unavailable")
        return await counting(key, amount, ttl=ttl)

    env.monkeypatch.setattr(lifecycle, "incrby_counter", failing)
    body = row_body(target, domain_id, "pending")
    response = await command(store, target, "verify", body)
    assert (response.status_code, response.json()) == (
        503,
        {"error": "public_mcp_domain_verification_unavailable"},
    )
    row = await row_of(store, domain_id)
    assert row is not None and (row.state, row.check_seq) == ("pending", 0)
    assert [action for action, _, _ in await audits(store, domain_id)] == [
        "public_mcp_domain.attached"
    ]
    # The next command, a new request, is answered normally.
    env.monkeypatch.setattr(lifecycle, "incrby_counter", counting)
    again = await command(store, target, "verify", body)
    assert again.status_code == 200, again.text
    assert again.json()["domain"] == {"domain_id": domain_id, "state": "active"}


@pytest.mark.parametrize("action", ["claim", "verify"])
async def test_an_owner_lost_before_the_lifecycle_reads_it_answers_the_uniform_404(env, action):
    """A transfer right after the pre-admission meets the lifecycle's own owner read first.

    Its refusal (owner required) is answered as every caller who is not admitted
    is: one 404 body, nothing counted, looked up or written.
    """
    store = env.store
    target = await installed(store)
    domain_id = None
    if action == "verify":
        hostname, domain_id = await claimed(env, target)
        env.dns.publish(hostname, await token_of(store, domain_id))
    before = [(str(row.id), row.state, row.check_seq) for row in await rows_of(store)]

    async def transfer() -> None:
        await fixtures.transfer(store, "next")

    once_after(env, commands, "_precheck", transfer)
    body = (
        claim_body(target, name("owner-lost"))
        if action == "claim"
        else row_body(target, domain_id, "pending")
    )
    response = await command(store, target, action, body)
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    assert [(str(row.id), row.state, row.check_seq) for row in await rows_of(store)] == before
    assert env.counts == {} and env.dns.calls == []


async def test_a_reserved_or_malformed_name_is_refused_after_the_admission(env):
    store = env.store
    target = await installed(store)
    for hostname in ("edge.example.com", "localhost", "has space.example.org"):
        body = claim_body(target, hostname)
        refused = await command(store, target, "claim", body)
        assert (refused.status_code, refused.json()) == (
            422,
            {"error": "public_mcp_domain_hostname_not_allowed"},
        ), hostname
        outsider = await command(store, target, "claim", body, role="member")
        assert (outsider.status_code, outsider.content) == (404, NOT_FOUND), hostname
    assert await rows_of(store) == []


async def test_verify_answers_held_and_cooldown_only_after_the_own_proof(env):
    store = env.store
    target = await installed(store)
    hostname = name("taken")
    holder = uuid4()
    now = utcnow()
    async with store.factory() as db:
        db.add(
            PublicMcpDomain(
                id=holder,
                hostname=hostname,
                workspace_id=store.other,
                endpoint_id=uuid4(),
                state="active",
                verification_token="3" * 64,
                verified_at=now,
                proof_ok_at=now,
                serve_until=now + timedelta(hours=1),
            )
        )
        await db.commit()
    claim = await command(store, target, "claim", claim_body(target, hostname))
    assert claim.status_code == 200, claim.text
    domain_id = claim.json()["domain"]["domain_id"]
    body = row_body(target, domain_id, "pending")
    silent = await command(store, target, "verify", body)  # no own proof: nothing about the holder
    assert silent.json()["domain"] == {"domain_id": domain_id, "state": "pending"}
    env.dns.publish(hostname, await token_of(store, domain_id))
    held = await command(store, target, "verify", body)
    assert (held.status_code, held.json()) == (409, {"error": "public_mcp_domain_held"})

    async with store.factory() as db:
        await db.execute(
            text(
                "UPDATE public_mcp_domains SET state = 'released', serve_until = NULL, "
                "released_at = timezone('UTC', statement_timestamp()), "
                "cooldown_until = timezone('UTC', statement_timestamp()) + interval '1 day' "
                "WHERE id = :id"
            ),
            {"id": holder},
        )
        await db.commit()
    cooling = await command(store, target, "verify", body)
    assert (cooling.status_code, cooling.json()) == (
        409,
        {"error": "public_mcp_domain_in_cooldown"},
    )
    row = await row_of(store, domain_id)
    assert row is not None and row.state == "pending"


async def test_a_domain_changed_during_the_lookup_answers_changed(env):
    """The fence (I2) through the commands: a pending record and a held check, both refused."""
    store = env.store
    target = await installed(store)
    _hostname, pending = await claimed(env, target, "fenced")

    def bump(domain_id: str) -> Callable[[], Awaitable[None]]:
        async def hook() -> None:
            async with store.factory() as db:
                await db.execute(
                    text("UPDATE public_mcp_domains SET check_seq = check_seq + 1 WHERE id = :id"),
                    {"id": domain_id},
                )
                await db.commit()

        return hook

    env.dns.during = bump(pending)
    response = await command(store, target, "verify", row_body(target, pending, "pending"))
    assert (response.status_code, response.json()) == (409, {"error": "public_mcp_domain_changed"})
    row = await row_of(store, pending)
    assert row is not None and (row.state, row.check_seq, row.last_checked_at) == (
        "pending",
        1,
        None,
    )
    removed = await command(store, target, "remove", row_body(target, pending, "pending"))
    assert removed.status_code == 200, removed.text

    _hostname, active = await activated(env, target, "fenced-held")
    before = await row_of(store, active)
    assert before is not None
    env.dns.during = bump(active)
    response = await command(store, target, "verify", row_body(target, active, "active"))
    assert (response.status_code, response.json()) == (409, {"error": "public_mcp_domain_changed"})
    row = await row_of(store, active)
    assert row is not None and (row.state, row.check_seq, row.last_checked_at) == (
        "active",
        before.check_seq + 1,
        before.last_checked_at,
    )


# ----------------------------------------------------------------------------
# Suspension and compatibility
# ----------------------------------------------------------------------------

ROLES = ("owner", "member", "admin", "viewer", "outsider")


async def test_commands_refuse_the_owner_of_a_suspended_workspace_only(env):
    store = env.store
    target = await installed(store)
    _hostname, domain_id = await claimed(env, target)
    bodies = {
        "claim": claim_body(target, name("suspended")),
        "verify": row_body(target, domain_id, "pending"),
        "remove": row_body(target, domain_id, "pending"),
    }

    async def answers(roles: tuple[str, ...]) -> dict:
        return {
            (role, action): await command(store, target, action, body, role=role)
            for role in roles
            for action, body in bodies.items()
        }

    with patch("services.workspace_slots.workspace_slot_mode", return_value="enforce"):
        before = await answers(ROLES[1:])
        async with store.factory() as db:
            db.add(
                WorkspaceSlotSuspension(
                    workspace_id=store.wid,
                    owner_user_id=store.users["owner"],
                    cause="billing_snapshot",
                    suspended_at=utcnow(),
                )
            )
            await db.commit()
        try:
            suspended = await answers(ROLES)
        finally:
            async with store.factory() as db:
                await db.execute(
                    delete(WorkspaceSlotSuspension).where(
                        WorkspaceSlotSuspension.workspace_id == store.wid
                    )
                )
                await db.commit()
    for action in ACTIONS:
        owner = suspended[("owner", action)]
        assert (owner.status_code, owner.json()) == (403, {"error": "SLOT-001"}), action
        for role in ROLES[1:]:
            answer, earlier = suspended[(role, action)], before[(role, action)]
            assert (answer.status_code, answer.content) == (404, NOT_FOUND), (role, action)
            assert answer.content == earlier.content
    rows = await rows_of(store)
    assert [(str(row.id), row.state, row.check_seq) for row in rows] == [(domain_id, "pending", 0)]


async def test_existing_responses_keep_their_shape(env):
    store = env.store
    target = await installed(store)
    await activated(env, target)
    installation = await fixtures.http(
        store, "GET", f"sync-installations/{target['installation_id']}"
    )
    assert installation.json()["operations"] == ["schema:read", "records:write", "results:read"]
    observation = (await observe(store, target)).json()
    assert set(observation) == {
        "schema_version",
        "subject",
        "workspace_id",
        "installation_id",
        "application_id",
        "resource_id",
        "resource_pk",
        "context_id",
        "endpoint_id",
        "public_id",
        "generation",
        "observed_at",
        "valid_until",
        "domain",
    }
    assert set(observation["domain"]) == {
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
        "tls",
    }
