"""Operation-history evidence through the mutation entry paths (#2739)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from starlette.requests import Request

from models.auth import Context
from models.operation_history import OperationHistory
from services.connector_provisioning import ConnectorProvisioningService
from services.context_service import CAPACITY_PURGE_ACTOR, ContextService


def _db():
    db = MagicMock()
    db.new = []
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    return db


def _context():
    return Context(
        id=uuid4(),
        workspace_id=uuid4(),
        name="history-test",
        created_by="creator",
        is_private=False,
        is_public=False,
        is_locked=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _rows(db):
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], OperationHistory)]


@pytest.mark.asyncio
async def test_mcp_context_update_records_real_changes_only():
    from mcp_server.tools.context import handle_update_context

    db, context = _db(), _context()

    async def database():
        yield db

    with (
        patch("db.base.get_db", database),
        patch(
            "services.permission_service.PermissionService.check_context_owner",
            AsyncMock(return_value=context),
        ),
        patch("mcp_server.tools.context._log_tool_usage", AsyncMock()),
    ):
        await handle_update_context(
            {"context_id": str(context.id), "is_locked": True}, "admin", context.workspace_id
        )
        await handle_update_context(
            {"context_id": str(context.id), "is_locked": True}, "admin", context.workspace_id
        )

    (event,) = _rows(db)
    assert (event.operation, event.actor_user_id, event.surface) == (
        "context.updated",
        "admin",
        "mcp",
    )
    assert event.context_id == context.id
    assert event.changed_fields == ["is_locked"]
    assert db.commit.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add", "extend", "role", "remove", "same_role"])
async def test_context_members_record_actor_subject_and_access_changes(action):
    from api.routes.contexts import (
        AddContextMemberRequest,
        UpdateContextMemberRoleRequest,
        add_context_member,
        remove_context_member,
        update_context_member_role,
    )

    db, context = _db(), _context()
    member = SimpleNamespace(user_id="affected", role="viewer", created_at=datetime.now(UTC))
    result = MagicMock()
    result.scalar_one_or_none.return_value = None if action == "add" else member
    db.execute.return_value = result
    workspace_member = SimpleNamespace(
        role="member", allowed_context_ids=[] if action == "extend" else [context.id]
    )
    request = Request({"type": "http"})
    with (
        patch("api.routes.contexts.get_current_user", AsyncMock(return_value={"user_id": "admin"})),
        patch(
            "services.permission_service.PermissionService.check_context_owner",
            AsyncMock(return_value=context),
        ),
        patch(
            "services.permission_service.PermissionService.is_workspace_member",
            AsyncMock(return_value=True),
        ),
        patch("services.permission_service.PermissionService.check_workspace_admin", AsyncMock()),
        patch(
            "services.workspace_service.WorkspaceService.get_member",
            AsyncMock(return_value=workspace_member),
        ),
    ):
        if action in {"add", "extend"}:
            await add_context_member(
                context.id,
                AddContextMemberRequest(
                    user_id="affected", role="editor", extend_access_list=action == "extend"
                ),
                request,
                db,
            )
        elif action == "remove":
            await remove_context_member(context.id, "affected", request, db)
        else:
            await update_context_member_role(
                context.id,
                "affected",
                UpdateContextMemberRoleRequest(
                    role="viewer" if action == "same_role" else "editor"
                ),
                request,
                db,
            )

    if action == "same_role":
        assert _rows(db) == []
        return
    (event,) = _rows(db)
    assert (
        event.operation
        == {
            "add": "member.added",
            "extend": "member.access_changed",
            "role": "member.role_changed",
            "remove": "member.removed",
        }[action]
    )
    assert event.actor_user_id == "admin"
    assert event.subject_user_id == "affected"
    assert event.workspace_id == context.workspace_id
    assert event.context_id == context.id
    if action == "extend":
        assert "allowed_context_ids" in event.changed_fields
        assert workspace_member.allowed_context_ids == [context.id]


@pytest.mark.asyncio
async def test_slack_token_refresh_is_attributed_and_unchanged_token_is_silent():
    db = _db()
    conn = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), context_id=uuid4(), config_version=1)
    bundle = {"bot_token": "fixture-old-token"}
    conn.get_oauth_tokens = lambda: bundle
    conn.set_oauth_tokens = lambda value: bundle.update(value)
    result = MagicMock()
    result.scalar_one_or_none.return_value = conn
    db.execute.return_value = result
    service = ConnectorProvisioningService(db)
    for _ in range(2):
        assert await service.refresh_slack_bot_token(
            conn.workspace_id, conn.id, "fixture-new-token", actor_user_id="admin"
        )
    (event,) = _rows(db)
    assert event.actor_user_id == "admin"
    assert event.operation == "integration.updated"
    assert event.changed_fields == ["credential"]
    assert conn.config_version == 2
    assert "fixture-new-token" not in repr(event.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runtime", "settings"])
async def test_connector_no_op_does_not_add_history(kind):
    db = _db()
    conn = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        context_id=None,
        config_version=3,
        runtime_config=None,
        channel_ids=None,
        litellm_virtual_key_id=None,
        locale=None,
        llm_config_encrypted=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = conn
    db.execute.return_value = result
    service = ConnectorProvisioningService(db)
    args = {"workspace_id": conn.workspace_id, "connector_id": conn.id, "user_id": "admin"}
    if kind == "runtime":
        await service.update_runtime_config(**args, runtime_config=None)
    else:
        await service.update_connector_settings(**args, locale=None)
    assert _rows(db) == []
    assert conn.config_version == 4  # Existing revision/optimistic-lock contract.


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", [False, True])
async def test_connector_llm_no_op_or_unreadable_bundle_repair(unreadable):
    db = _db()
    bundle = {"provider": "fixture", "model": "fixture"}
    conn = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        context_id=None,
        config_version=1,
        channel_ids=None,
        litellm_virtual_key_id=None,
        locale=None,
        llm_config_encrypted="fixture-ciphertext",
        get_llm_config=MagicMock(
            side_effect=ValueError("unreadable") if unreadable else None, return_value=bundle
        ),
        set_llm_config=MagicMock(),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = conn
    db.execute.return_value = result
    await ConnectorProvisioningService(db).update_connector_settings(
        workspace_id=conn.workspace_id,
        connector_id=conn.id,
        user_id="admin",
        llm_config=bundle,
    )
    conn.set_llm_config.assert_called_once_with(bundle)
    assert len(_rows(db)) == int(unreadable)
    if unreadable:
        assert _rows(db)[0].changed_fields == ["settings"]


@pytest.mark.asyncio
async def test_resource_auto_revoke_preserves_actor_separate_from_owner_filter():
    from auth.resource_tokens import ResourceTokenManager

    db = _db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    db.execute.return_value = result
    manager = ResourceTokenManager(db)
    manager.revoke_token = AsyncMock()
    with patch("auth.resource_tokens.resolve_resource_pk", AsyncMock(return_value=uuid4())):
        assert (
            await manager.revoke_tokens_for_resource(
                uuid4(), "resource", created_by="owner", actor_user_id="admin"
            )
            == 2
        )
    assert [c.kwargs["actor_user_id"] for c in manager.revoke_token.await_args_list] == [
        "admin",
        "admin",
    ]


@pytest.mark.asyncio
async def test_capacity_chunks_keep_context_counts_separate_and_system_attributed():
    db, workspace = _db(), uuid4()
    first, second = uuid4(), uuid4()
    results = []
    for contexts in ([first, first, second, None], []):
        result = MagicMock()
        result.scalars.return_value.all.return_value = contexts
        results.append(result)
    db.execute.side_effect = results
    committed = []
    db.commit.side_effect = lambda: committed.extend(_rows(db))
    total, aborted = await ContextService(db)._tombstone_chunked(
        [],
        actor=CAPACITY_PURGE_ACTOR,
        workspace_id=workspace,
        deleted_at=datetime.now(UTC),
    )
    assert (total, aborted) == (4, False)
    assert len(committed) == 3
    assert {row.context_id: row.count for row in committed} == {first: 2, second: 1, None: 1}
    assert all(row.surface == "system" and row.actor_user_id is None for row in committed)
    assert all(row.workspace_id == workspace for row in committed)
