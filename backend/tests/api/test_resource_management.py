"""Real database checks of the independent management authorization boundary."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Context, Workspace, WorkspaceMember
from models.management_grant import ManagementGrant, ManagementOperation
from models.public_mcp import PublicMcpEndpoint
from models.resource import ResourceEvent, ResourceSchema
from tests.observability_helpers import app_client, issue_credential, owner_client, world_rows
from utils.datetime import utcnow
from utils.exceptions import RedisError

pytestmark = pytest.mark.asyncio(loop_scope="session")
BASE = "/api/v1/resource-management"
GRANTS = BASE + "-grants"
OPS = [
    "schema:read",
    "events:upsert",
    "events:delete",
    "tokens:read",
    "tokens:create",
    "tokens:update",
    "tokens:revoke",
]


@pytest_asyncio.fixture(loop_scope="session")
async def scenario(async_engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    monkeypatch.setattr(
        "api.routes.resource_management._schedule_indexer_for_resource", AsyncMock()
    )
    async with world_rows(async_engine) as w:
        async with w.factory() as s:
            await s.execute(
                update(Workspace)
                .where(Workspace.id.in_([w.ws, w.other_ws]))
                .values(plan_name="promax")
            )
            endpoints = {}
            for key in ("a1", "a2", "b1"):
                endpoint = PublicMcpEndpoint(
                    id=uuid4(),
                    workspace_id=w.other_ws if key == "b1" else w.ws,
                    resource_pk=w.resource_pk[key],
                    context_id=w.ctx[key],
                    resource_id=w.slugs[key],
                    public_name="Synthetic public resource",
                    created_by=w.other_owner if key == "b1" else w.owner,
                )
                s.add(endpoint)
                s.add(
                    ResourceSchema(
                        resource_pk=w.resource_pk[key],
                        resource_id=w.slugs[key],
                        schema_version=1,
                        field_definitions=[
                            {
                                "name": "name",
                                "type": "text",
                                "classification": "public",
                                "required": True,
                            },
                            {"name": "private_note", "type": "text", "classification": "pii"},
                        ],
                    )
                )
                endpoints[key] = endpoint.id
            await s.commit()
        yield w, endpoints


def body(w, endpoints, key="a1", **changes):
    return {
        "resource_pk": str(w.resource_pk[key]),
        "context_id": str(w.ctx[key]),
        "endpoint_id": str(endpoints[key]),
        "operations": OPS,
        "approved_fields": ["name"],
        "ttl_days": 1,
        "quota_events_per_hour": 100,
        "max_active_tokens": 2,
        "acknowledge_full_resource_token_authority": True,
        **changes,
    }


async def issued(w, endpoints, **changes):
    async with owner_client(w, w.owner) as c:
        result = await c.post(GRANTS, json=body(w, endpoints, **changes))
        assert result.status_code == 201
        assert result.headers["cache-control"] == "no-store"
        return result.json()


def headers(grant):
    return {"authorization": "Bearer " + grant["secret"], "x-management-actor": "a" * 64}


async def test_owner_provisioning_and_credential_isolation(scenario):
    w, endpoints = scenario
    async with owner_client(w, w.member) as c:
        assert (await c.post(GRANTS, json=body(w, endpoints))).status_code == 403
    async with owner_client(w, w.owner) as c:
        assert (await c.post(GRANTS, json=body(w, endpoints, "b1"))).status_code == 404
        assert (
            await c.post(
                GRANTS, json=body(w, endpoints, acknowledge_full_resource_token_authority=False)
            )
        ).status_code == 422
        assert (
            await c.post(GRANTS, json=body(w, endpoints, approved_fields=["private_note"]))
        ).status_code == 409
    grant = await issued(w, endpoints)
    async with w.factory() as s:
        row = await s.get(ManagementGrant, UUID(grant["id"]))
        assert row.key_hash != grant["secret"] and len(row.key_hash) == 64
    _, observation = await issue_credential(w, resource="a1")
    async with app_client(w) as c:
        assert (
            await c.get(BASE + "/schema", headers={"authorization": "Bearer " + observation})
        ).status_code == 401
        assert (await c.get("/api/v1/resource-tokens", headers=headers(grant))).status_code == 401
        schema = await c.get(BASE + "/schema", headers=headers(grant))
        assert schema.status_code == 200
        assert schema.json()["fields"] == [{"name": "name", "type": "text", "required": True}]
        assert grant["secret"] not in schema.text
        assert (
            await c.post(
                BASE + "/tokens",
                headers={"authorization": "Bearer " + grant["secret"]},
                json={"operation_id": str(uuid4())},
            )
        ).status_code == 400


async def test_token_lifecycle_one_time_and_cross_resource(scenario):
    w, endpoints = scenario
    grant = await issued(w, endpoints)
    foreign = await issued(
        w,
        endpoints,
        resource_pk=str(w.resource_pk["a2"]),
        context_id=str(w.ctx["a2"]),
        endpoint_id=str(endpoints["a2"]),
    )
    async with app_client(w) as c:
        request = {
            "operation_id": str(uuid4()),
            "description": "External integration",
            "quota_events_per_hour": 50,
        }
        result = await c.post(BASE + "/tokens", headers=headers(grant), json=request)
        assert result.status_code == 201
        created = result.json()
        token_id = created["token_metadata"]["id"]
        assert created["token"].startswith("kagura_resource_")
        assert result.headers["cache-control"] == "no-store"
        replay = await c.post(BASE + "/tokens", headers=headers(grant), json=request)
        assert replay.status_code == 201 and "token" not in replay.json()
        assert replay.json()["replayed"] is True
        listed = await c.get(BASE + "/tokens", headers=headers(grant))
        assert listed.json()["total"] == 1 and created["token"] not in listed.text
        assert (
            await c.patch(
                BASE + "/tokens/" + token_id,
                headers=headers(foreign),
                json={**request, "operation_id": str(uuid4())},
            )
        ).status_code == 404
        assert (
            await c.patch(
                BASE + "/tokens/" + token_id,
                headers=headers(grant),
                json={**request, "operation_id": str(uuid4()), "quota_events_per_hour": 101},
            )
        ).status_code == 403
        updated = await c.patch(
            BASE + "/tokens/" + token_id,
            headers=headers(grant),
            json={**request, "operation_id": str(uuid4()), "description": "Updated"},
        )
        assert (
            updated.status_code == 200
            and updated.json()["token_metadata"]["description"] == "Updated"
        )
        assert (
            await c.request(
                "DELETE",
                BASE + "/tokens/" + token_id,
                headers=headers(grant),
                json={"operation_id": str(uuid4())},
            )
        ).status_code == 200
        assert (await c.get(BASE + "/tokens", headers=headers(grant))).json()["tokens"][0][
            "status"
        ] == "revoked"


async def test_ingest_replay_fields_versions_partial_and_no_payload_audit(scenario):
    w, endpoints = scenario
    grant = await issued(w, endpoints)
    event = {
        "op": "upsert",
        "doc_id": "synthetic",
        "version": 1,
        "idempotency_key": str(uuid4()),
        "payload": {"name": "Synthetic public text"},
    }
    request = {"operation_id": str(uuid4()), "schema_hash": grant["schema_hash"], "events": [event]}
    async with app_client(w) as c:
        response = await c.post(BASE + "/events", headers=headers(grant), json=request)
        assert response.status_code == 200 and response.json()["accepted"] == 1
        assert response.json()["searchable"] == "unknown"
        replay = await c.post(BASE + "/events", headers=headers(grant), json=request)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        changed = {**request, "events": [{**event, "payload": {"name": "Changed"}}]}
        assert (
            await c.post(BASE + "/events", headers=headers(grant), json=changed)
        ).status_code == 409
        private = {
            **request,
            "operation_id": str(uuid4()),
            "events": [{**event, "payload": {"name": "Safe", "private_note": "SECRET-SYNTHETIC"}}],
        }
        invalid = await c.post(BASE + "/events", headers=headers(grant), json=private)
        assert invalid.status_code == 422 and "SECRET-SYNTHETIC" not in invalid.text
        for version in (True, 1.5, "1"):
            invalid = await c.post(
                BASE + "/events",
                headers=headers(grant),
                json={**request, "events": [{**event, "version": version}]},
            )
            assert invalid.status_code == 422
        partial = {
            **request,
            "operation_id": str(uuid4()),
            "events": [
                {**event, "idempotency_key": str(uuid4())},
                {**event, "doc_id": "second", "idempotency_key": str(uuid4())},
            ],
        }
        response = await c.post(BASE + "/events", headers=headers(grant), json=partial)
        assert (
            response.status_code == 200
            and response.json()["state"] == "partial"
            and response.json()["accepted"] == 1
        )
        assert response.json()["errors"][0]["index"] == 0
    async with w.factory() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(ResourceEvent)
            .where(ResourceEvent.resource_pk == w.resource_pk["a1"])
        )
        assert count == 2
        audit = (
            (
                await s.execute(
                    select(ManagementOperation).where(
                        ManagementOperation.grant_id == UUID(grant["id"])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 2
        assert all("Synthetic public text" not in str(row.outcome) for row in audit)


@pytest.mark.parametrize(
    "change", ["owner", "plan", "private", "endpoint", "context", "expiry", "schema"]
)
async def test_current_authority_changes_fail_closed(scenario, change):
    w, endpoints = scenario
    grant = await issued(w, endpoints)
    async with w.factory() as s:
        if change == "owner":
            await s.execute(
                update(WorkspaceMember)
                .where(WorkspaceMember.workspace_id == w.ws, WorkspaceMember.user_id == w.owner)
                .values(role=WorkspaceRole.MEMBER)
            )
        if change == "plan":
            await s.execute(update(Workspace).where(Workspace.id == w.ws).values(plan_name="free"))
        if change == "private":
            await s.execute(
                update(Context).where(Context.id == w.ctx["a1"]).values(is_private=True)
            )
        if change == "endpoint":
            await s.execute(
                update(PublicMcpEndpoint)
                .where(PublicMcpEndpoint.id == endpoints["a1"])
                .values(revoked_at=utcnow())
            )
        if change == "context":
            await s.execute(
                update(Context).where(Context.id == w.ctx["a1"]).values(resource_id=None)
            )
        if change == "expiry":
            await s.execute(
                update(ManagementGrant)
                .where(ManagementGrant.id == UUID(grant["id"]))
                .values(expires_at=utcnow() - timedelta(seconds=1))
            )
        if change == "schema":
            s.add(
                ResourceSchema(
                    resource_pk=w.resource_pk["a1"],
                    resource_id=w.slugs["a1"],
                    schema_version=2,
                    field_definitions=[],
                )
            )
        await s.commit()
    async with app_client(w) as c:
        assert (await c.get(BASE + "/capabilities", headers=headers(grant))).status_code in {
            401,
            403,
            409,
        }


async def test_revocation_and_operation_allowlist(scenario):
    w, endpoints = scenario
    grant = await issued(w, endpoints, operations=["schema:read"])
    async with app_client(w) as c:
        assert (await c.get(BASE + "/tokens", headers=headers(grant))).status_code == 403
        assert (
            await c.post(
                BASE + "/tokens", headers=headers(grant), json={"operation_id": str(uuid4())}
            )
        ).status_code == 403
    async with owner_client(w, w.owner) as c:
        assert (await c.delete(GRANTS + "/" + grant["id"])).status_code == 204
    async with app_client(w) as c:
        assert (await c.get(BASE + "/schema", headers=headers(grant))).status_code == 401


async def test_concurrent_mint_respects_cap_and_oversize_request(scenario):
    w, endpoints = scenario
    grant = await issued(w, endpoints, max_active_tokens=1)
    async with app_client(w) as c:
        replies = await asyncio.gather(
            *(
                c.post(
                    BASE + "/tokens",
                    headers=headers(grant),
                    json={"operation_id": str(uuid4()), "quota_events_per_hour": 10},
                )
                for _ in range(2)
            )
        )
        assert sorted(r.status_code for r in replies) == [201, 403]
        assert (await c.get(BASE + "/tokens", headers=headers(grant))).json()["total"] == 1
        oversized = await c.post(
            BASE + "/events",
            headers={**headers(grant), "content-type": "application/json"},
            content=b" " * 1048577,
        )
        assert oversized.status_code == 413


async def test_quota_backend_failure_does_not_persist(scenario, monkeypatch):
    w, endpoints = scenario
    grant = await issued(w, endpoints)
    monkeypatch.setattr(
        "services.resource_quota_service.get_cache",
        AsyncMock(side_effect=RedisError("Synthetic outage")),
    )
    request = {
        "operation_id": str(uuid4()),
        "schema_hash": grant["schema_hash"],
        "events": [
            {
                "op": "upsert",
                "doc_id": "quota-test",
                "version": 1,
                "payload": {"name": "Safe"},
                "idempotency_key": str(uuid4()),
            }
        ],
    }
    async with app_client(w) as c:
        assert (
            await c.post(BASE + "/events", headers=headers(grant), json=request)
        ).status_code == 503
    async with w.factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(ResourceEvent)
                .where(ResourceEvent.resource_pk == w.resource_pk["a1"])
            )
            == 0
        )
        assert (
            await s.get(ManagementOperation, (UUID(grant["id"]), UUID(request["operation_id"])))
            is None
        )


async def test_scheduler_failure_replay_reports_acceptance_only(scenario, monkeypatch):
    w, endpoints = scenario
    grant = await issued(w, endpoints)
    monkeypatch.setattr(
        "api.routes.resource_management._schedule_indexer_for_resource",
        AsyncMock(side_effect=RuntimeError("Synthetic scheduling failure")),
    )
    request = {
        "operation_id": str(uuid4()),
        "schema_hash": grant["schema_hash"],
        "events": [
            {
                "op": "upsert",
                "doc_id": "schedule-test",
                "version": 1,
                "payload": {"name": "Safe"},
                "idempotency_key": str(uuid4()),
            }
        ],
    }
    async with app_client(w) as c:
        # The ASGI harness propagates the post-commit server exception.
        with pytest.raises(RuntimeError, match="Synthetic scheduling failure"):
            await c.post(BASE + "/events", headers=headers(grant), json=request)
        replay = await c.post(BASE + "/events", headers=headers(grant), json=request)
        assert replay.status_code == 200
        assert replay.json()["state"] == "accepted" and replay.json()["searchable"] == "unknown"
        assert replay.json()["replayed"] is True


async def test_numeric_boundaries_are_rejected_before_persistence_and_replay(scenario):
    w, endpoints = scenario
    async with w.factory() as s:
        await s.execute(
            update(ResourceSchema)
            .where(ResourceSchema.resource_pk == w.resource_pk["a1"])
            .values(
                field_definitions=[
                    {"name": "name", "type": "number", "classification": "public", "required": True}
                ]
            )
        )
        await s.commit()
    grant = await issued(w, endpoints)
    event = {
        "op": "upsert",
        "doc_id": "numeric",
        "version": 1,
        "payload": {"name": 5},
        "idempotency_key": str(uuid4()),
    }
    request = {"operation_id": str(uuid4()), "schema_hash": grant["schema_hash"], "events": [event]}
    async with app_client(w) as c:
        assert (
            await c.post(BASE + "/events", headers=headers(grant), json=request)
        ).status_code == 200
        # Python's JSON parser accepts exponent overflow. The API must reject it,
        # including when the request reuses a previously accepted operation ID.
        for operation_id in (request["operation_id"], str(uuid4())):
            for number in ("1e400", "9" * 400):
                wire = json.dumps({**request, "operation_id": operation_id}).replace(
                    '"name": 5', '"name": ' + number
                )
                response = await c.post(
                    BASE + "/events",
                    headers={**headers(grant), "content-type": "application/json"},
                    content=wire,
                )
                assert response.status_code == 422
        oversized_version = {
            **request,
            "operation_id": str(uuid4()),
            "events": [{**event, "version": 2147483648}],
        }
        assert (
            await c.post(BASE + "/events", headers=headers(grant), json=oversized_version)
        ).status_code == 422
    async with w.factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(ResourceEvent)
                .where(ResourceEvent.resource_pk == w.resource_pk["a1"])
            )
            == 1
        )
