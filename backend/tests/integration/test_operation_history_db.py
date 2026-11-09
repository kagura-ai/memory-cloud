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
from models.resource import Resource, ResourceToken, WorkspaceConnector
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
    return await read_history(db, workspace_id=s.workspace, user_id=s.owner, limit=30, **filters)


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
    own = await read_history(db, workspace_id=s.workspace, user_id=s.other, limit=30)
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
    assert (await read_history(db, workspace_id=s.workspace, user_id=s.other, limit=30))[
        "total"
    ] == 2


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
    at = datetime(2026, 11, 9, tzinfo=UTC)
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
    for plan in ("pro", "basic", "free"):
        await db.execute(
            update(Workspace).where(Workspace.id == s.workspace).values(plan_name=plan)
        )
        await db.commit()
        with pytest.raises(FeatureNotAvailableError):
            await route(db, s)
    # Another Max workspace never unlocked this one; upgrading this one does.
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
    assert (await read_history(db, workspace_id=s.workspace, user_id=s.other, limit=30))[
        "total"
    ] == 2
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
    for _ in range(2):
        await revoke_api_key(key.public_id, {"user_id": s.owner}, manager=None, db=db)
    await delete_api_key(key.public_id, {"user_id": s.owner}, manager=None, db=db)
    events = (await db.scalars(select(Event))).all()
    assert len(events) == 1
    assert events[0].operation == "credential.revoked"
    assert events[0].actor_user_id == s.owner
    assert events[0].workspace_id == s.workspace
    assert events[0].surface == "web"


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
