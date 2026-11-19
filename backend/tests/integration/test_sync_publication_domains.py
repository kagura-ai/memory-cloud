"""Installation-bound domain receipts through real OIDC and PostgreSQL."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.routes.internal_public_mcp_domains import router as internal_domains
from config.settings import get_settings
from models.public_mcp_domain import PublicMcpDomain
from models.sync_installation import SyncService
from tests.integration import sync_installation_fixtures as fixtures
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store


async def test_owner_observes_only_current_binding_and_lease(store, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_public_usage_budget_enabled", True)
    monkeypatch.setattr(settings, "mcp_public_domains_routing_target", "edge.kagura-ai.com")
    monkeypatch.setattr(settings, "mcp_public_domains_enabled", True)
    await fixtures.paid(store)
    approval = await fixtures.approve(store, fixtures.approval_body(application_id=str(uuid4())))
    response = await fixtures.http(
        store, "POST", "sync-installations", body=fixtures.activation(approval)
    )
    assert response.status_code == 201, response.text
    target = response.json()
    path = f"sync-installations/{target['installation_id']}/publication-domains"

    async def read(**kwargs):
        return await fixtures.http(store, "GET", path, **kwargs)

    initial = await read()
    assert initial.status_code == 200, initial.text
    receipt = initial.json()
    assert initial.headers["cache-control"] == "no-store"
    assert receipt["domain"] is None and receipt["subject"] == store.users["owner"]
    for key in (
        "installation_id",
        "workspace_id",
        "application_id",
        "resource_id",
        "resource_pk",
        "context_id",
        "endpoint_id",
        "public_id",
        "generation",
    ):
        assert receipt[key] == target[key]
    start, end = (datetime.fromisoformat(receipt[k]) for k in ("observed_at", "valid_until"))
    assert timedelta(0) < end - start <= timedelta(seconds=30)
    for role in ("member", "admin", "viewer", "outsider"):
        assert (await read(role=role)).status_code == 404
    assert (await read(workspace=store.other)).status_code == 404

    now = utcnow()
    name = f"{uuid4().hex}.kagura-ai.com"
    rid = uuid4()
    async with store.factory() as db:
        db.add(
            PublicMcpDomain(
                id=rid,
                workspace_id=store.wid,
                endpoint_id=UUID(target["endpoint_id"]),
                hostname=name,
                state="active",
                verification_token="a" * 64,
                created_at=now,
                verified_at=now,
                proof_ok_at=now,
                last_checked_at=now,
                last_proof_result="ok",
                routing_status="mismatch",
                serve_until=now + timedelta(seconds=20),
            )
        )
        await db.commit()
    response = await read()
    assert response.status_code == 200, response.text
    facts = response.json()["domain"]
    assert facts["serving"] and facts["tls"] == {"status": "unreported"}
    assert facts["last_check"]["result"] == "ok" and facts["routing"]["status"] == "mismatch"
    assert response.json()["valid_until"] == facts["serve_until"]
    # A dedicated edge reporter writes to the same binding read by the owner.
    store.app.include_router(internal_domains)
    token = uuid4().hex
    report = {
        "domain_id": str(rid),
        "hostname": name,
        "endpoint_id": target["endpoint_id"],
        "tls": {
            "status": "valid",
            "observed_at": now.isoformat() + "Z",
            "valid_until": (now + timedelta(minutes=3)).isoformat() + "Z",
            "certificate_not_after": (now + timedelta(days=1)).isoformat() + "Z",
        },
    }
    async with AsyncClient(transport=ASGITransport(app=store.app), base_url="https://test") as edge:
        route = "/internal/public-mcp-domains/tls-observation"
        auth = {"Authorization": f"Bearer {token}"}
        assert (await edge.put(route, json=report, headers=auth)).status_code == 404
        monkeypatch.setattr(settings, "mcp_public_domains_tls_reporter_token", token)
        assert (await edge.put(route, json=report)).status_code == 401
        for mismatch in (
            {"hostname": "other.example.org"},
            {"endpoint_id": str(uuid4())},
            {"domain_id": str(uuid4())},
        ):
            assert (await edge.put(route, json=report | mismatch, headers=auth)).status_code == 404
        for _ in range(2):
            assert (await edge.put(route, json=report, headers=auth)).status_code == 204
        changed = report | {"tls": report["tls"] | {"status": "renewal_failed"}}
        assert (await edge.put(route, json=changed, headers=auth)).status_code == 409
        assert (await read()).json()["domain"]["tls"] == report["tls"]
        assert (await read(role="member")).status_code == 404
        changed["tls"]["observed_at"] = utcnow().isoformat() + "Z"
        assert (await edge.put(route, json=changed, headers=auth)).status_code == 204
        assert (await edge.put(route, json=report, headers=auth)).status_code == 409
        assert (await read()).json()["domain"]["tls"]["status"] == "renewal_failed"
    await fixtures.change(store, PublicMcpDomain, rid, serve_until=now)
    assert (await read()).json()["domain"]["serving"] is False
    await fixtures.change(
        store,
        PublicMcpDomain,
        rid,
        state="released",
        released_at=now,
        cooldown_until=now + timedelta(hours=1),
    )
    released = (await read()).json()["domain"]
    async with AsyncClient(transport=ASGITransport(app=store.app), base_url="https://test") as edge:
        assert (await edge.put(route, json=report, headers=auth)).status_code == 404
    assert set(released) == {"state", "domain_id", "hostname", "released_at", "cooldown_until"}
    assert released["state"] == "released"
    # A same-workspace reassignment is possible even during cooldown.
    # A new holder of the same hostname cannot supply this endpoint's facts.
    async with store.factory() as db:
        other_id = uuid4()
        db.add(
            PublicMcpDomain(
                id=other_id,
                workspace_id=store.wid,
                endpoint_id=uuid4(),
                hostname=name,
                state="active",
                verification_token="b" * 64,
                verified_at=now,
                proof_ok_at=now,
                serve_until=now + timedelta(hours=1),
            )
        )
        await db.commit()
    assert (await read()).json()["domain"] is None
    await fixtures.change(store, PublicMcpDomain, rid, cooldown_until=now)
    await fixtures.change(store, PublicMcpDomain, other_id, workspace_id=store.other)
    assert (await read()).json()["domain"] is None
    async with store.factory() as db:
        for key in (rid, other_id):
            await db.delete(await db.get(PublicMcpDomain, key))
        await db.execute(
            text("DELETE FROM public_mcp_domain_tls_reports WHERE domain_id = :rid"), {"rid": rid}
        )
        await db.commit()
    await fixtures.change(
        store, SyncService, store.service_id, service_client_id="replacement-" + uuid4().hex
    )
    assert (await read()).status_code == 404
    monkeypatch.setattr(settings, "mcp_public_domains_enabled", False)
    assert (await read()).status_code == 404
