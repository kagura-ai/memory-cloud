"""Operation history for personal OAuth clients and management grants (#2739).

Real PostgreSQL: the OAuth client routes write through the synchronous OAuth2
session, the grant routes through the async one, and the history INSERT is
conditional on the workspace's entitlement in both.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import oauth as oauth_routes
from auth.history_principal import set_history_principal
from models.auth import Context, OAuth2Client, User, Workspace, WorkspaceMember
from models.operation_history import OperationHistory as Event

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _request(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.9"),
        headers={"user-agent": "pytest-ua"},
        cookies={},
        query_params={},
        state=SimpleNamespace(user={"user_id": user_id}),
    )


@pytest.fixture
def sync_session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from config.database import to_sync_database_url
    from tests.conftest import TEST_DATABASE_URL

    engine = create_engine(to_sync_database_url(TEST_DATABASE_URL))
    try:
        with engine.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"Test database not available (sync): {exc}")
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    suffix = uuid4().hex[:10]
    owner = f"hist-owner-{suffix}"
    db_session.add(User(user_id=owner, email=f"{owner}@example.test", name="History Owner"))
    await db_session.flush()
    workspaces = {plan: uuid4() for plan in ("pro", "basic")}
    for plan, workspace_id in workspaces.items():
        db_session.add(
            Workspace(id=workspace_id, name=f"h-{plan}", owner_user_id=owner, plan_name=plan)
        )
    await db_session.flush()
    for workspace_id in workspaces.values():
        db_session.add(WorkspaceMember(workspace_id=workspace_id, user_id=owner, role="owner"))
    await db_session.commit()
    set_history_principal("session")
    yield SimpleNamespace(owner=owner, pro=workspaces["pro"], basic=workspaces["basic"])
    set_history_principal()
    await db_session.rollback()
    for workspace_id in workspaces.values():
        await db_session.execute(delete(Event).where(Event.workspace_id == workspace_id))
        await db_session.execute(
            delete(OAuth2Client).where(OAuth2Client.workspace_id == workspace_id)
        )
    await db_session.commit()


async def _events(db: AsyncSession, workspace_id) -> list[Event]:
    db.expire_all()
    return list(
        (
            await db.scalars(
                select(Event).where(Event.workspace_id == workspace_id).order_by(Event.occurred_at)
            )
        ).all()
    )


async def _client_lifecycle(world, workspace_id, monkeypatch, encryptor) -> str:
    request = _request(world.owner)
    user = {"user_id": world.owner, "current_workspace_id": workspace_id}
    created = await oauth_routes.create_oauth2_client(
        request,
        BackgroundTasks(),
        oauth_routes.OAuth2ClientCreateRequest(
            client_name="History App", redirect_uris=["https://app.example/cb"]
        ),
        user,
    )
    client_id = created.client_id
    # A no-op edit records nothing; a real one records the field names only.
    await oauth_routes.update_oauth2_client(
        request,
        client_id,
        oauth_routes.OAuth2ClientUpdateRequest(client_name="History App"),
        user,
    )
    await oauth_routes.update_oauth2_client(
        request,
        client_id,
        oauth_routes.OAuth2ClientUpdateRequest(
            client_name="Renamed App", redirect_uris=["https://app.example/other"]
        ),
        user,
    )
    await oauth_routes.regenerate_oauth2_client_secret(request, BackgroundTasks(), client_id, user)
    await oauth_routes.delete_oauth2_client(request, client_id, user)
    return client_id


@pytest.fixture
def encryptor(monkeypatch) -> None:
    import utils.encryption

    monkeypatch.setattr(
        utils.encryption, "get_encryptor", lambda: SimpleNamespace(encrypt=lambda value: "enc")
    )


async def test_workspace_oauth_client_lifecycle_is_recorded(
    db_session, world, sync_session_factory, monkeypatch, encryptor
):
    monkeypatch.setattr(oauth_routes, "get_sync_session", sync_session_factory)
    client_id = await _client_lifecycle(world, world.pro, monkeypatch, encryptor)
    events = await _events(db_session, world.pro)
    assert [e.operation for e in events] == [
        "credential.issued",
        "credential.updated",
        "credential.revoked",
        "credential.issued",
        "credential.revoked",
    ]
    assert {e.target_id for e in events} == {client_id}
    assert {e.target_type for e in events} == {"credential"}
    assert all(e.actor_user_id == world.owner == e.subject_user_id for e in events)
    assert all(e.surface == "web" for e in events)
    assert events[1].changed_fields == ["name", "redirect_uris"]
    assert "Renamed App" not in str([e.changed_fields for e in events])


async def test_oauth_client_of_a_workspace_without_history_records_nothing(
    db_session, world, sync_session_factory, monkeypatch, encryptor
):
    monkeypatch.setattr(oauth_routes, "get_sync_session", sync_session_factory)
    await _client_lifecycle(world, world.basic, monkeypatch, encryptor)
    assert await _events(db_session, world.basic) == []


async def test_management_grant_events_carry_the_grant_and_its_context(db_session, world):
    from api.routes import management_grants

    context_id = uuid4()
    context = Context(
        id=context_id, workspace_id=world.pro, name="grant-ctx", created_by=world.owner
    )
    db_session.add(context)
    await db_session.commit()
    grant = SimpleNamespace(id=uuid4(), workspace_id=world.pro, context_id=context_id)
    for operation in ("credential.issued", "credential.revoked", "credential.revoked"):
        management_grants.record_grant(db_session, grant, world.owner, operation)
        await db_session.commit()
    events = await _events(db_session, world.pro)
    assert [(e.operation, e.target_id, e.context_id) for e in events] == [
        ("credential.issued", str(grant.id), context_id),
        ("credential.revoked", str(grant.id), context_id),
    ]
    assert all(e.target_type == "credential" and e.actor_user_id == world.owner for e in events)
    await db_session.execute(delete(Event).where(Event.workspace_id == world.pro))
    await db_session.delete(context)
    await db_session.commit()


def _grant_db(*results):
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[MagicMock(scalar_one_or_none=MagicMock(return_value=r)) for r in results]
    )
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


async def test_management_grant_routes_record_issue_and_one_revocation(monkeypatch):
    from api.routes import management_grants
    from models.management_schemas import GrantCreate

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        management_grants,
        "record_grant",
        lambda _db, grant, actor, operation: calls.append((str(grant.id), operation)),
    )
    monkeypatch.setattr(management_grants, "current_binding", AsyncMock())
    workspace_id = uuid4()
    resource = SimpleNamespace(id=uuid4())
    schema = SimpleNamespace(schema_version=1, field_definitions=[])
    body = GrantCreate(
        resource_pk=resource.id,
        context_id=uuid4(),
        endpoint_id=uuid4(),
        operations=["schema:read"],
        approved_fields=["title"],
    )
    created = await management_grants.create_grant(
        body, ("owner", workspace_id), Response(), _grant_db(None, resource, schema)
    )
    assert calls == [(created["id"], "credential.issued")]
    grant = SimpleNamespace(id=UUID(created["id"]), revoked_at=None)
    for _ in range(2):
        await management_grants.revoke_grant(
            grant.id, ("owner", workspace_id), _grant_db(None, grant)
        )
    assert calls[1:] == [(created["id"], "credential.revoked")]
