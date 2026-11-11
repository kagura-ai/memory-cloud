"""Real PostgreSQL isolation, transaction and entitlement checks (#2739).

Uses a random private schema in TEST_DATABASE_URL, never application tables.
REQUIRE_TEST_DATABASE=1 makes missing infrastructure fail rather than skip.
"""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

import models.llm_pricing  # noqa: F401
from api.routes.operation_history import get_operation_history
from db.base import Base
from models.auth import APIKey, Context, IdentityLink, User, Workspace, WorkspaceMember
from models.operation_history import OperationHistory as Event
from models.resource import (
    Resource,
    ResourceToken,
    WorkspaceConnector,
    WorkspaceConnectorChannelRoute,
)
from services.operation_history import read_history
from services.operation_history_erasure import erase_operation_history
from services.operation_history_writer import record_operation
from utils.exceptions import AuthorizationError, FeatureNotAvailableError

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def history_db():
    schema = "history_test_" + uuid4().hex
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
    )
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {"search_path": schema, "timezone": "UTC"},
            "timeout": 5,
        },
    )
    tables = {
        m.__table__
        for m in (
            User,
            Workspace,
            WorkspaceMember,
            Context,
            IdentityLink,
            Event,
            Resource,
            ResourceToken,
            WorkspaceConnector,
            # #1759: the connector settings path reads its channel routes.
            WorkspaceConnectorChannelRoute,
            APIKey,
        )
    }
    tables.add(APIKey.__table__)
    tables.add(Base.metadata.tables["context_search_configs"])
    while True:
        dependencies = {fk.column.table for table in tables for fk in table.foreign_keys}
        if dependencies <= tables:
            break
        tables.update(dependencies)
    try:
        async with engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    except (OSError, ConnectionError, TimeoutError) as exc:
        await engine.dispose()
        if os.environ.get("REQUIRE_TEST_DATABASE") == "1":
            pytest.fail(f"PostgreSQL required: {type(exc).__name__}")
        pytest.skip("PostgreSQL test server unavailable")
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        async with engine.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


async def seed(db):
    owner, other = "owner-" + uuid4().hex, "other-" + uuid4().hex
    db.add_all(
        [
            User(user_id=owner, email=owner + "@example.test", name="Visible Owner"),
            User(user_id=other, email=other + "@example.test", name="Private Owner"),
        ]
    )
    await db.flush()
    workspace, elsewhere = uuid4(), uuid4()
    db.add_all(
        [
            Workspace(id=workspace, name="History", owner_user_id=owner, plan_name="promax"),
            Workspace(id=elsewhere, name="Elsewhere", owner_user_id=owner, plan_name="promax"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            WorkspaceMember(workspace_id=workspace, user_id=owner, role="owner"),
            WorkspaceMember(workspace_id=workspace, user_id=other, role="admin"),
            WorkspaceMember(workspace_id=elsewhere, user_id=owner, role="owner"),
        ]
    )
    shared, private, foreign = uuid4(), uuid4(), uuid4()
    db.add_all(
        [
            Context(
                id=shared, workspace_id=workspace, name="shared", created_by=owner, is_private=False
            ),
            Context(
                id=private,
                workspace_id=workspace,
                name="private",
                created_by=other,
                is_private=True,
            ),
            Context(
                id=foreign,
                workspace_id=elsewhere,
                name="foreign",
                created_by=owner,
                is_private=False,
            ),
        ]
    )
    await db.commit()
    return SimpleNamespace(
        owner=owner,
        other=other,
        workspace=workspace,
        elsewhere=elsewhere,
        shared=shared,
        private=private,
        foreign=foreign,
    )


async def page(db, s, **filters):
    return await read_history(
        db, workspace_id=s.workspace, user_id=s.owner, limit=30, retention_days=90, **filters
    )


async def route(db, s):
    response = Response()
    result = await get_operation_history(
        s.workspace,
        {"user_id": s.owner, "current_workspace_id": s.elsewhere},
        response,
        db,
        query="",
        limit=30,
        cursor=None,
        context_id=None,
        actor=None,
        agent_id=None,
        operation=None,
        target_type=None,
        outcome=None,
        since=None,
        until=None,
    )
    assert response.headers["Cache-Control"] == "no-store"
    return result


async def test_private_and_cross_workspace_rows_counts_search_are_scoped(history_db):
    db = history_db
    s = await seed(db)
    for context, target in [
        (s.shared, "visible"),
        (s.private, "private-secret-id"),
        (s.foreign, "foreign-id"),
    ]:
        record_operation(
            db,
            operation="memory.updated",
            actor_user_id=s.other,
            context_id=context,
            target_id=target,
        )
    await db.commit()
    assert (await page(db, s))["total"] == 1
    for query in ("private-secret-id", "foreign-id"):
        assert (await page(db, s, query=query))["total"] == 0
    assert (await page(db, s, context_id=s.private))["items"] == []
    assert (await page(db, s, context_id=s.foreign))["items"] == []
    own = await read_history(
        db, workspace_id=s.workspace, user_id=s.other, limit=30, retention_days=90
    )
    assert own["total"] == 2


async def test_snapshot_and_current_privacy_both_apply_and_deleted_target_survives(history_db):
    db = history_db
    s = await seed(db)
    await db.execute(update(Context).where(Context.id == s.shared).values(created_by=s.other))
    await db.commit()
    record_operation(
        db,
        operation="memory.deleted",
        actor_user_id=s.owner,
        context_id=s.shared,
        target_id=uuid4(),
    )
    record_operation(
        db,
        operation="memory.created",
        actor_user_id=s.other,
        context_id=s.private,
        target_id=uuid4(),
    )
    await db.commit()
    # Opening a formerly private context does not reveal its old private activity.
    await db.execute(update(Context).where(Context.id == s.private).values(is_private=False))
    # Closing a shared context immediately hides its older activity from other admins.
    await db.execute(
        update(Context).where(Context.id == s.shared).values(is_private=True, created_by=s.other)
    )
    await db.commit()
    assert (await page(db, s))["total"] == 0
    await db.execute(delete(Context).where(Context.id.in_([s.shared, s.private])))
    await db.commit()
    # Purging never reopens a boundary that is no longer verifiable.
    assert (await page(db, s))["total"] == 0
    assert (
        await read_history(
            db, workspace_id=s.workspace, user_id=s.other, limit=30, retention_days=90
        )
    )["total"] == 2


async def test_rollback_retry_and_changed_field_allowlist(history_db):
    db = history_db
    s = await seed(db)
    key = "source:" + uuid4().hex
    kwargs = {
        "operation": "memory.updated",
        "actor_user_id": s.owner,
        "context_id": s.shared,
        "event_key": key,
        "changed_fields": ["content", "password", "raw_query=secret"],
        "target_id": uuid4(),
    }
    record_operation(db, **kwargs)
    await db.flush()
    await db.rollback()
    assert (await page(db, s))["total"] == 0
    record_operation(db, **kwargs)
    record_operation(db, **kwargs)
    await db.commit()
    result = await page(db, s)
    assert result["total"] == 1
    assert result["items"][0]["changed_fields"] == ["content"]
    # Replaying a committed source (and an autoflushed source) is also harmless.
    record_operation(db, **kwargs)
    await db.flush()
    record_operation(db, **kwargs)
    await db.commit()
    assert (await page(db, s))["total"] == 1


async def test_combined_filters_stable_pagination_literal_search_and_actor_names(history_db):
    db = history_db
    s = await seed(db)
    at = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    agent = uuid4()
    for index in range(5):
        record_operation(
            db,
            operation="memory.updated",
            actor_user_id=s.owner,
            context_id=s.shared,
            target_id=f"id%_{index}",
            agent_id=agent,
            surface="mcp",
            at=at,
        )
    record_operation(
        db,
        operation="sleep.failed",
        actor_user_id=None,
        context_id=s.shared,
        surface="system",
        outcome="failure",
        at=at,
    )
    await db.commit()
    filters = {
        "workspace_id": s.workspace,
        "user_id": s.owner,
        "query": "id%_",
        "context_id": s.shared,
        "actor": s.owner,
        "agent_id": agent,
        "operation": "memory.updated",
        "target_type": "memory",
        "outcome": "success",
        "since": at - timedelta(seconds=1),
        "until": at + timedelta(seconds=1),
        "limit": 2,
        "retention_days": 90,
    }
    ids = []
    cursor = None
    while True:
        result = await read_history(db, **filters, cursor=cursor)
        assert result["total"] == 5
        ids.extend(item["id"] for item in result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert len(ids) == len(set(ids)) == 5
    assert (await page(db, s, query="Visible Owner"))["total"] == 5
    assert (await page(db, s, outcome="failure"))["items"][0]["actor_user_id"] is None


async def test_workspace_entitlement_and_role_rechecked_after_changes(history_db):
    db = history_db
    s = await seed(db)
    record_operation(
        db,
        operation="member.added",
        actor_user_id=s.owner,
        workspace_id=s.workspace,
        subject_user_id=s.other,
    )
    await db.commit()
    assert (await route(db, s)).total == 1
    await db.execute(update(Workspace).where(Workspace.id == s.workspace).values(plan_name="pro"))
    await db.commit()
    assert (await route(db, s)).total == 1
    for plan in ("basic", "free"):
        await db.execute(
            update(Workspace).where(Workspace.id == s.workspace).values(plan_name=plan)
        )
        await db.commit()
        with pytest.raises(FeatureNotAvailableError):
            await route(db, s)
    # Another entitled workspace never unlocked this one; upgrading this one does.
    await db.execute(
        update(Workspace).where(Workspace.id == s.workspace).values(plan_name="promax")
    )
    await db.commit()
    assert (await route(db, s)).total == 1
    await db.execute(
        update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == s.workspace, WorkspaceMember.user_id == s.owner)
        .values(role="member")
    )
    await db.commit()
    with pytest.raises(AuthorizationError):
        await route(db, s)


async def test_erasure_scrubs_actor_subject_owner_and_live_name(history_db):
    db = history_db
    s = await seed(db)
    record_operation(
        db,
        operation="member.role_changed",
        actor_user_id=s.owner,
        workspace_id=s.workspace,
        subject_user_id=s.other,
    )
    record_operation(
        db,
        operation="memory.created",
        actor_user_id=s.other,
        context_id=s.private,
        target_id=uuid4(),
    )
    await db.commit()
    await erase_operation_history(db, s.other, "erased-subject")
    await db.commit()
    events = (await db.scalars(select(Event))).all()
    for event in events:
        assert s.other not in (event.actor_user_id, event.subject_user_id, event.context_owner_id)
    assert (await page(db, s, actor=s.other))["total"] == 0


async def test_migration_roundtrip_matches_model_and_keeps_existing_tables(history_db):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path(__file__).parents[2] / "alembic/versions/f33_2739_operation_history.py"
    spec = importlib.util.spec_from_file_location("history_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def roundtrip(session):
        connection = session.connection()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert inspect(connection).has_table("contexts")
            migration.upgrade()
            inspector = inspect(connection)
            assert {c["name"] for c in inspector.get_columns(Event.__tablename__)} == set(
                Event.__table__.columns.keys()
            )
            assert {i["name"] for i in inspector.get_indexes(Event.__tablename__)} == {
                i.name for i in Event.__table__.indexes
            }

    await history_db.run_sync(roundtrip)
    await history_db.commit()


async def test_concurrent_replay_and_failed_source_transaction(history_db):
    import asyncio

    from sqlalchemy.exc import IntegrityError

    db = history_db
    s = await seed(db)
    key = "concurrent:" + uuid4().hex

    async def write():
        async with AsyncSession(db.bind, expire_on_commit=False) as session:
            record_operation(
                session,
                operation="member.added",
                actor_user_id=s.owner,
                workspace_id=s.workspace,
                event_key=key,
            )
            await session.commit()

    await asyncio.gather(write(), write())
    assert (await page(db, s))["total"] == 1
    record_operation(
        db, operation="member.removed", actor_user_id=s.owner, workspace_id=s.workspace
    )
    db.add(User(user_id=s.owner, email="duplicate@example.test"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    assert (await page(db, s))["total"] == 1


async def test_resource_credentials_follow_canonical_workspace_and_private_boundary(history_db):
    from auth.resource_tokens import ResourceTokenManager

    db = history_db
    s = await seed(db)
    resource = Resource(id=uuid4(), workspace_id=s.workspace, resource_id="private-resource")
    db.add(resource)
    await db.execute(
        update(Context).where(Context.id == s.private).values(resource_id=resource.resource_id)
    )
    await db.commit()
    manager = ResourceTokenManager(db)
    _, token = await manager.create_token(
        resource.resource_id,
        resource_pk=resource.id,
        workspace_id=s.workspace,
        created_by=s.owner,
        actor_user_id=s.other,
    )
    await db.commit()
    # Deliberately drift the non-authoritative shadow column.
    await db.execute(
        update(ResourceToken).where(ResourceToken.id == token.id).values(workspace_id=s.elsewhere)
    )
    await db.commit()
    await db.refresh(token)
    await manager.revoke_token(token.id, actor_user_id=s.other)
    await db.commit()
    events = (await db.scalars(select(Event).order_by(Event.operation))).all()
    assert {event.workspace_id for event in events} == {s.workspace}
    assert {event.context_id for event in events} == {s.private}
    assert {event.actor_user_id for event in events} == {s.other}
    assert {event.operation for event in events} == {"credential.issued", "credential.revoked"}
    assert (await page(db, s))["total"] == 0
    assert (
        await read_history(
            db, workspace_id=s.workspace, user_id=s.other, limit=30, retention_days=90
        )
    )["total"] == 2
    await manager.revoke_token(token.id, actor_user_id=s.other)
    await db.commit()
    assert len((await db.scalars(select(Event))).all()) == 2


async def test_member_changes_distinguish_actor_and_subject_and_ignore_replays(history_db):
    from services.workspace_service import WorkspaceService

    db = history_db
    s = await seed(db)
    service = WorkspaceService(db)
    for _ in range(2):
        await service.update_member_role(s.workspace, s.other, "member", actor_user_id=s.owner)
        await service.update_member_context_access(s.workspace, s.other, [], actor_user_id=s.owner)
    events = (await db.scalars(select(Event))).all()
    assert {e.operation for e in events} == {"member.role_changed", "member.access_changed"}
    assert len(events) == 2
    assert all(e.actor_user_id == s.owner and e.subject_user_id == s.other for e in events)


async def test_connector_rebinding_keeps_private_history_boundary(history_db):
    from services.connector_provisioning import ConnectorProvisioningService

    db = history_db
    s = await seed(db)
    resource = Resource(id=uuid4(), workspace_id=s.workspace, resource_id="connector-history")
    db.add(resource)
    await db.flush()
    connector = WorkspaceConnector(
        workspace_id=s.workspace,
        resource_pk=resource.id,
        connector_type="slack",
        context_id=s.private,
        kmc_api_key_encrypted="placeholder",
        created_by=s.owner,
    )
    db.add(connector)
    await db.commit()
    service = ConnectorProvisioningService(db)
    # Private -> shared -> private -> unbound: each transition touches private data.
    for target in (s.shared, s.private, None):
        await service.update_connector_settings(
            workspace_id=s.workspace, connector_id=connector.id, user_id=s.owner, context_id=target
        )
        await db.commit()
    events = (await db.scalars(select(Event))).all()
    assert len(events) == 3
    assert all(e.context_id == s.private and e.context_private for e in events)
    assert (await page(db, s))["total"] == 0


async def test_personal_api_key_routes_record_workspace_revocation_once(history_db):
    from api.routes.api_keys import delete_api_key, revoke_api_key
    from auth.history_principal import set_history_principal

    db = history_db
    s = await seed(db)
    key = APIKey(
        user_id=s.owner,
        workspace_id=s.workspace,
        key_hash=uuid4().hex,
        key_prefix="test",
        name="test",
    )
    db.add(key)
    await db.commit()
    set_history_principal("session")
    for _ in range(2):
        await revoke_api_key(key.public_id, {"user_id": s.owner}, manager=None, db=db)
    await delete_api_key(key.public_id, {"user_id": s.owner}, manager=None, db=db)
    events = (await db.scalars(select(Event))).all()
    assert len(events) == 1
    assert events[0].operation == "credential.revoked"
    assert events[0].actor_user_id == s.owner
    assert events[0].workspace_id == s.workspace
    assert events[0].surface == "web"
    set_history_principal()


async def test_context_creation_keeps_authenticated_actor_separate_from_owner(history_db):
    from services.context_service import ContextService

    db = history_db
    s = await seed(db)
    context = await ContextService(db).create_context(
        workspace_id=s.workspace,
        name="connector-auto",
        created_by=s.owner,
        actor_user_id=s.other,
        create_collection=False,
    )
    event = (await db.scalars(select(Event).where(Event.target_id == str(context.id)))).one()
    assert event.actor_user_id == s.other
    assert event.context_owner_id == s.owner
    assert event.context_private


async def test_history_write_failure_rolls_back_business_change(history_db):
    from sqlalchemy.exc import IntegrityError

    db = history_db
    s = await seed(db)
    original_name = await db.scalar(select(Workspace.name).where(Workspace.id == s.workspace))
    await db.execute(
        update(Workspace).where(Workspace.id == s.workspace).values(name="uncommitted")
    )
    row = record_operation(
        db,
        operation="context.updated",
        actor_user_id=s.owner,
        workspace_id=s.workspace,
    )
    row.count = 0  # Inject a real database failure in the evidence INSERT.
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    assert (
        await db.scalar(select(Workspace.name).where(Workspace.id == s.workspace)) == original_name
    )
    assert (await page(db, s))["items"] == []


async def test_timestamp_is_utc_under_non_utc_database_timezone(history_db):
    from sqlalchemy import text

    db = history_db
    s = await seed(db)
    await db.execute(text("SET TIME ZONE 'Asia/Tokyo'"))
    naive_utc = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(hours=1)
    record_operation(
        db,
        operation="context.updated",
        actor_user_id=s.owner,
        workspace_id=s.workspace,
        at=naive_utc,
    )
    await db.commit()
    assert (await page(db, s))["items"][0]["occurred_at"] == naive_utc.replace(tzinfo=UTC)


async def test_linked_private_owner_reads_only_current_link_set(history_db):
    db = history_db
    s = await seed(db)
    record_operation(db, operation="memory.created", actor_user_id=s.other, context_id=s.private)
    await db.commit()
    assert (await page(db, s))["total"] == 0
    group = uuid4()
    db.add_all([IdentityLink(group_id=group, user_id=user) for user in (s.owner, s.other)])
    await db.commit()
    assert (await page(db, s))["total"] == 1
    await db.execute(delete(IdentityLink).where(IdentityLink.user_id == s.owner))
    await db.commit()
    assert (await page(db, s))["total"] == 0


async def test_count_cap_has_same_privacy_filters_and_keeps_pagination(history_db, monkeypatch):
    monkeypatch.setattr("services.operation_history.COUNT_LIMIT", 2)
    db = history_db
    s = await seed(db)
    for _ in range(4):
        record_operation(db, operation="memory.created", actor_user_id=s.other, context_id=s.shared)
        record_operation(
            db, operation="memory.created", actor_user_id=s.other, context_id=s.private
        )
    await db.commit()
    first = await read_history(
        db, workspace_id=s.workspace, user_id=s.owner, limit=2, retention_days=90
    )
    assert first["total"] == 2 and first["total_capped"] is True
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = await read_history(
        db,
        workspace_id=s.workspace,
        user_id=s.owner,
        limit=2,
        retention_days=90,
        cursor=first["next_cursor"],
    )
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    hidden = await page(db, s, context_id=s.private)
    assert hidden["total"] == 0 and hidden["total_capped"] is False


@pytest_asyncio.fixture
async def history_http(history_db):
    """Exercise FastAPI parsing and the actual SessionUser dependency over HTTP."""
    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from api.main import memory_cloud_exception_handler
    from api.routes.operation_history import router
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)
    s = await seed(history_db)
    app.state.user_id = s.owner

    @app.middleware("http")
    async def browser_session(request: Request, call_next):
        request.state.user = {"user_id": app.state.user_id} if app.state.user_id else None
        return await call_next(request)

    async def database():
        yield history_db

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, app, s, f"/api/v1/workspaces/{s.workspace}/operation-history"


async def test_http_rejects_all_bearer_credentials_and_expired_session(history_http):
    client, app, _, url = history_http
    assert (await client.get(url)).status_code == 200
    for token in ("kagura_test", "oauth-test", "kagura_resource_test"):
        result = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        assert result.status_code == 403
        assert "items" not in result.json()
    app.state.user_id = None
    assert (await client.get(url)).status_code == 401


async def test_http_rechecks_removed_role_and_selected_workspace_entitlement(
    history_http, history_db
):
    client, _, s, url = history_http
    record_operation(
        history_db, operation="member.added", actor_user_id=s.owner, workspace_id=s.workspace
    )
    await history_db.commit()
    response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(response.json()["items"]) == 1
    await history_db.execute(
        update(Workspace).where(Workspace.id == s.workspace).values(plan_name="free")
    )
    await history_db.commit()
    denied = await client.get(url)
    assert denied.status_code == 403 and denied.json()["error"] == "FEAT-001"
    await history_db.execute(
        update(Workspace).where(Workspace.id == s.workspace).values(plan_name="promax")
    )
    await history_db.commit()
    assert (await client.get(url)).status_code == 200
    await history_db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == s.workspace,
            WorkspaceMember.user_id == s.owner,
        )
    )
    await history_db.commit()
    assert (await client.get(url)).status_code == 403


@pytest.mark.parametrize(
    "params",
    [
        {"query": "x" * 121},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "x" * 513},
        {"context_id": "invalid"},
        {"agent_id": "invalid"},
        {"outcome": "invalid"},
    ],
)
async def test_http_validates_query_boundaries(history_http, params):
    client, _, _, url = history_http
    assert (await client.get(url, params=params)).status_code == 422


async def test_http_validates_timezone_and_treats_blank_actor_as_unset(history_http):
    client, _, _, url = history_http
    naive = await client.get(url, params={"until": "2026-11-09T12:00:00"})
    assert naive.status_code == 422
    assert naive.json()["details"]["field"] == "until"
    assert (await client.get(url, params={"actor": "  "})).status_code == 200


async def _set_plan(db, workspace, plan):
    await db.execute(update(Workspace).where(Workspace.id == workspace).values(plan_name=plan))
    await db.commit()


async def _stored(db, workspace):
    return (await db.scalars(select(Event).where(Event.workspace_id == workspace))).all()


@pytest.mark.parametrize("plan,recorded", [("free", 0), ("basic", 0), ("pro", 1), ("promax", 1)])
async def test_only_entitled_workspaces_record(history_db, plan, recorded):
    db = history_db
    s = await seed(db)
    await _set_plan(db, s.workspace, plan)
    record_operation(db, operation="member.added", actor_user_id=s.owner, workspace_id=s.workspace)
    # A context-only event resolves its workspace (and so its entitlement) in SQL.
    record_operation(
        db, operation="memory.updated", actor_user_id=s.owner, context_id=s.shared, target_id="m"
    )
    await db.commit()
    assert len(await _stored(db, s.workspace)) == 2 * recorded
    # The other workspace stays entitled and keeps recording.
    record_operation(db, operation="member.added", actor_user_id=s.owner, workspace_id=s.elsewhere)
    await db.commit()
    assert len(await _stored(db, s.elsewhere)) == 1


async def test_downgrade_keeps_rows_refuses_reads_and_upgrade_shows_them_again(history_db):
    db = history_db
    s = await seed(db)
    await _set_plan(db, s.workspace, "pro")
    record_operation(db, operation="member.added", actor_user_id=s.owner, workspace_id=s.workspace)
    await db.commit()
    assert (await route(db, s)).total == 1
    await _set_plan(db, s.workspace, "basic")
    with pytest.raises(FeatureNotAvailableError):
        await route(db, s)
    # Nothing new is recorded while the workspace is not entitled ...
    record_operation(
        db, operation="member.removed", actor_user_id=s.owner, workspace_id=s.workspace
    )
    await db.commit()
    # ... and the downgrade deleted nothing.
    assert [e.operation for e in await _stored(db, s.workspace)] == ["member.added"]
    await _set_plan(db, s.workspace, "pro")
    result = await route(db, s)
    assert [item.operation for item in result.items] == ["member.added"]


async def test_tier_override_that_grants_the_feature_records(history_db, monkeypatch):
    from dataclasses import replace

    from config.plan_tiers import PLAN_TIERS

    db = history_db
    s = await seed(db)
    await _set_plan(db, s.workspace, "basic")
    basic = PLAN_TIERS["basic"]
    # The registry after a PLAN_BASIC_FEATURES override that adds the feature.
    monkeypatch.setitem(
        PLAN_TIERS, "basic", replace(basic, features=basic.features | {"operation_history"})
    )
    record_operation(db, operation="member.added", actor_user_id=s.owner, workspace_id=s.workspace)
    await db.commit()
    assert len(await _stored(db, s.workspace)) == 1
    assert (await route(db, s)).total == 1


async def test_savepoint_rollback_discards_the_queued_event_and_a_retry_records(history_db):
    db = history_db
    s = await seed(db)
    key = "savepoint:" + uuid4().hex
    kwargs = {
        "operation": "member.added",
        "actor_user_id": s.owner,
        "workspace_id": s.workspace,
        "event_key": key,
    }
    savepoint = await db.begin_nested()
    first = record_operation(db, **kwargs)
    await savepoint.rollback()
    retried = record_operation(db, **kwargs)
    assert retried is not first
    await db.commit()
    assert len(await _stored(db, s.workspace)) == 1


def _days_ago(days, minutes=0):
    return datetime.now(UTC) - timedelta(days=days, minutes=minutes)


async def test_reads_only_show_rows_inside_the_effective_retention(history_db):
    db = history_db
    s = await seed(db)
    for days, target in ((89, "kept"), (91, "expired"), (200, "ancient")):
        record_operation(
            db,
            operation="memory.updated",
            actor_user_id=s.owner,
            context_id=s.shared,
            target_id=target,
            at=_days_ago(days),
        )
    await db.commit()
    result = await route(db, s)
    assert result.retention_days == 90
    assert [item.target_id for item in result.items] == ["kept"]
    assert result.total == 1
    # Search, filters and pagination share the window: expired rows are gone
    # immediately, before the daily purge deletes them.
    assert (await page(db, s, query="expired"))["total"] == 0
    first = await read_history(
        db, workspace_id=s.workspace, user_id=s.owner, limit=1, retention_days=90
    )
    assert first["next_cursor"] is None
    # The add-on extends the window for this workspace only.
    await db.execute(
        update(Workspace)
        .where(Workspace.id == s.workspace)
        .values(addon_history_retention_days_bonus=110)
    )
    await db.commit()
    result = await route(db, s)
    assert result.retention_days == 200
    assert [item.target_id for item in result.items] == ["kept", "expired"]


async def test_purge_deletes_expired_rows_in_batches(history_db):
    from tasks.operation_history_tasks import purge_operation_history

    db = history_db
    s = await seed(db)
    now = datetime.now(UTC)
    gone = uuid4()  # a workspace whose row no longer exists keeps the base
    await db.execute(
        update(Workspace)
        .where(Workspace.id == s.elsewhere)
        .values(addon_history_retention_days_bonus=30)
    )
    rows = [
        (s.workspace, 91, "expired-1"),
        (s.workspace, 95, "expired-2"),
        (s.workspace, 120, "expired-3"),
        (s.workspace, 89, "kept"),
        (s.elsewhere, 100, "bonus-kept"),
        (s.elsewhere, 121, "bonus-expired"),
    ]
    for workspace, days, target in rows:
        record_operation(
            db,
            operation="member.added",
            actor_user_id=s.owner,
            workspace_id=workspace,
            target_id=target,
            at=now - timedelta(days=days),
        )
    await db.commit()
    # Orphans were recorded while their workspace existed; insert them directly
    # (the writer would refuse a workspace that does not exist).
    from sqlalchemy import insert

    await db.execute(
        insert(Event.__table__),
        [
            {
                "id": uuid4(),
                "workspace_id": gone,
                "context_private": False,
                "occurred_at": now - timedelta(days=days),
                "principal_kind": "system",
                "surface": "system",
                "operation": "member.added",
                "target_type": "member",
                "target_id": target,
                "outcome": "success",
                "count": 1,
                "changed_fields": [],
            }
            for days, target in ((91, "orphan-expired"), (60, "orphan-kept"))
        ],
    )
    await db.commit()
    counts = await purge_operation_history(db, now=now, batch_size=2)
    assert counts == {"deleted": 5, "batches": 3, "budget_exhausted": 0}
    remaining = {e.target_id for e in (await db.scalars(select(Event))).all()}
    assert remaining == {"kept", "bonus-kept", "orphan-kept"}
    # A rerun finds nothing left to delete.
    again = await purge_operation_history(db, now=now, batch_size=2)
    assert again == {"deleted": 0, "batches": 0, "budget_exhausted": 0}


async def test_purge_stops_at_its_time_budget(history_db):
    from tasks.operation_history_tasks import purge_operation_history

    db = history_db
    s = await seed(db)
    for index in range(3):
        record_operation(
            db,
            operation="member.added",
            actor_user_id=s.owner,
            workspace_id=s.workspace,
            target_id=f"old-{index}",
            at=_days_ago(100 + index),
        )
    await db.commit()
    counts = await purge_operation_history(db, batch_size=1, time_budget_seconds=0)
    assert counts == {"deleted": 1, "batches": 1, "budget_exhausted": 1}
    assert len(await _stored(db, s.workspace)) == 2
