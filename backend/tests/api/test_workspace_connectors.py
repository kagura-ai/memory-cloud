"""Tests for workspace connector setup API (Issue #851, F6-b of #755)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError as PydanticValidationError

from api.routes.workspace_connectors import (
    WorkspaceConnectorCreateRequest,
    create_workspace_connector,
)
from utils.exceptions import MemoryCloudException

_HTTP = SimpleNamespace(client=SimpleNamespace(host="192.0.2.5"), headers={})


@pytest.mark.asyncio
async def test_create_workspace_connector_rolls_back_on_service_failure():
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.provision_connector = AsyncMock(
            side_effect=MemoryCloudException(
                "Connector seat limit reached.",
                status_code=403,
                error_code="CONNECTOR-SEAT-CAP",
            )
        )
        with pytest.raises(MemoryCloudException):
            await create_workspace_connector(request, _HTTP, BackgroundTasks(), admin, db)

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_team_conflict_wire_bodies_carry_reason():
    """#1753: the two team conflicts reach the wire as ``RES-002`` / 409 with a
    ``details.reason``; the "elsewhere" body carries nothing else."""
    import json

    from fastapi import Request

    from api.main import memory_cloud_exception_handler
    from utils.exceptions import (
        ConnectorTeamConnectedElsewhereError,
        ConnectorTeamConnectedHereError,
    )

    connector_id = uuid4()
    request_stub = MagicMock(spec=Request)
    request_stub.url.path = "/api/v1/workspace-connectors"

    here = await memory_cloud_exception_handler(
        request_stub,
        ConnectorTeamConnectedHereError(
            connector_type="slack", connector_id=connector_id, display_name="Acme"
        ),
    )
    assert here.status_code == 409
    assert json.loads(here.body)["error"] == "RES-002"
    assert json.loads(here.body)["details"] == {
        "reason": "connector_team_connected_here",
        "connector_id": str(connector_id),
        "display_name": "Acme",
    }

    elsewhere = await memory_cloud_exception_handler(
        request_stub, ConnectorTeamConnectedElsewhereError(connector_type="slack")
    )
    body = json.loads(elsewhere.body)
    assert elsewhere.status_code == 409
    assert set(body) == {"error", "message", "details"}
    assert body["error"] == "RES-002"
    assert body["details"] == {"reason": "connector_team_connected_elsewhere"}


@pytest.fixture
def _fernet_env(monkeypatch):
    """Set a Fernet key and reset the ``get_encryptor`` singleton around a test."""
    from cryptography.fernet import Fernet

    import utils.encryption as enc_module

    monkeypatch.setenv("API_KEY_SECRET", Fernet.generate_key().decode())
    enc_module._encryptor = None
    yield
    enc_module._encryptor = None


class _ScanRedis:
    """Async Redis stub for the cache invalidation: scan_iter / delete over a dict."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    async def scan_iter(self, match=None, count=None):
        prefix = match[:-1]
        assert match.endswith("*") and "*" not in prefix
        for key in list(self.store):
            if key.startswith(prefix):
                yield key

    async def delete(self, *keys):
        return sum(1 for key in keys if self.store.pop(key, None) is not None)


def _existing_slack_connector(workspace_id, *, bot_token="xoxb-old"):
    """A real (unpersisted) connector row, so the encrypted bundle round-trips."""
    from models.resource import WorkspaceConnector

    connector = WorkspaceConnector(
        id=uuid4(),
        resource_pk=uuid4(),
        workspace_id=workspace_id,
        connector_type="slack",
        app_key="default",
        external_team_id="T01",
        config_version=3,
    )
    connector.set_oauth_tokens({"bot_token": bot_token, "installing_admin_user_id": "U-old"})
    return connector


def _db_returning(connector):
    """AsyncSession stub whose one SELECT answers with ``connector``."""
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = connector
    db.execute = AsyncMock(return_value=result)
    return db


async def _create_into_same_workspace_conflict(
    *, db, workspace_id, connector, redis, request=None, install=None
):
    """Run the create route against a same-workspace team conflict (#1880)."""
    from services.connector_provisioning import ConnectorProvisioningService
    from utils.exceptions import ConnectorTeamConnectedHereError

    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = request or WorkspaceConnectorCreateRequest(
        connector_type="slack", resource_id="slack_acme", slack_install_handle="handle-1"
    )
    if install is None:
        install = {
            "workspace_id": str(workspace_id),
            "team_id": "T01",
            "bot_token": "xoxb-new",
            "installing_admin_user_id": "U-new",
            "app_key": "default",
        }
    discard = AsyncMock()
    with (
        patch.object(
            ConnectorProvisioningService,
            "provision_connector",
            AsyncMock(
                side_effect=ConnectorTeamConnectedHereError(
                    connector_type="slack", connector_id=connector.id, display_name="Acme"
                )
            ),
        ),
        patch("api.routes.connectors_slack.peek_slack_install", AsyncMock(return_value=install)),
        patch("api.routes.connectors_slack.discard_slack_install", discard),
        patch("api.routes.workspace_connectors.get_redis_client", return_value=redis),
        pytest.raises(ConnectorTeamConnectedHereError) as excinfo,
    ):
        await create_workspace_connector(request, _HTTP, BackgroundTasks(), admin, db)
    return excinfo.value, discard


@pytest.mark.asyncio
async def test_create_same_workspace_conflict_stores_the_new_install_bot_token(_fernet_env):
    """#1880: reconnecting a Slack workspace this workspace already holds is
    still a 409, but the new install's bot token replaces the stored one — an
    app that was removed and reinstalled leaves the old token dead. The cached
    channel pages and the public-only marker go with it, the worker is told to
    refetch (``config_version``), and the one-time handle is spent."""
    workspace_id = uuid4()
    connector = _existing_slack_connector(workspace_id)
    db = _db_returning(connector)
    other_id = uuid4()
    redis = _ScanRedis(
        {
            f"slack_channels:{connector.id}:": "page-1",
            f"slack_channels:{connector.id}:CUR": "page-2",
            f"slack_channels_types:{connector.id}": "public_channel",
            f"slack_channels:{other_id}:": "other-connector-page",
            f"slack_channels_types:{other_id}": "public_channel",
        }
    )

    conflict, discard = await _create_into_same_workspace_conflict(
        db=db, workspace_id=workspace_id, connector=connector, redis=redis
    )

    assert conflict.status_code == 409
    assert conflict.details == {
        "reason": "connector_team_connected_here",
        "connector_id": str(connector.id),
        "display_name": "Acme",
        "token_refreshed": True,  # confirmed, so the UI may say so
    }
    # The rest of the bundle is the existing connector's, not the install's.
    assert connector.get_oauth_tokens() == {
        "bot_token": "xoxb-new",
        "installing_admin_user_id": "U-old",
    }
    assert connector.config_version == 4
    db.rollback.assert_awaited_once()  # the failed create
    db.commit.assert_awaited_once()  # the token refresh
    # The lookup is predicated on the caller's workspace.
    where = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert workspace_id.hex in where.replace("-", "")
    discard.assert_awaited_once_with("handle-1")
    assert set(redis.store) == {
        f"slack_channels:{other_id}:",
        f"slack_channels_types:{other_id}",
    }


@pytest.mark.asyncio
async def test_create_same_workspace_conflict_same_token_keeps_config_version(_fernet_env):
    """#1880: a re-consent that returns the same bot token (widened grant)
    writes nothing, but the cached listing is still dropped so the new scopes
    show on the next dialog open."""
    workspace_id = uuid4()
    connector = _existing_slack_connector(workspace_id, bot_token="xoxb-new")
    db = _db_returning(connector)
    redis = _ScanRedis({f"slack_channels_types:{connector.id}": "public_channel"})

    conflict, discard = await _create_into_same_workspace_conflict(
        db=db, workspace_id=workspace_id, connector=connector, redis=redis
    )

    assert conflict.details["token_refreshed"] is True
    assert connector.get_oauth_tokens()["bot_token"] == "xoxb-new"
    assert connector.config_version == 3
    discard.assert_awaited_once_with("handle-1")
    assert redis.store == {}


@pytest.mark.asyncio
async def test_create_same_workspace_conflict_without_install_handle_stores_nothing(_fernet_env):
    """#1880: the manual bind (pasted token, no OAuth install) is refused
    without touching the existing connector."""
    workspace_id = uuid4()
    connector = _existing_slack_connector(workspace_id)
    db = _db_returning(connector)
    redis = _ScanRedis({f"slack_channels_types:{connector.id}": "public_channel"})

    conflict, discard = await _create_into_same_workspace_conflict(
        db=db,
        workspace_id=workspace_id,
        connector=connector,
        redis=redis,
        request=WorkspaceConnectorCreateRequest(
            connector_type="slack",
            resource_id="slack_acme",
            external_team_id="T01",
            oauth_tokens={"bot_token": "xoxb-pasted"},
        ),
    )

    assert "token_refreshed" not in conflict.details
    assert connector.get_oauth_tokens()["bot_token"] == "xoxb-old"
    assert connector.config_version == 3
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    discard.assert_not_awaited()
    assert redis.store == {f"slack_channels_types:{connector.id}": "public_channel"}


@pytest.mark.asyncio
async def test_create_same_workspace_conflict_refresh_failure_still_answers_409(_fernet_env):
    """#1880: the refresh is best-effort — a failure while storing the token
    keeps the 409 (not a 500) and leaves the install handle for a retry. The
    body does not claim a refresh, so the UI falls back to the neutral copy."""
    workspace_id = uuid4()
    connector = _existing_slack_connector(workspace_id)
    db = _db_returning(connector)
    db.commit = AsyncMock(side_effect=RuntimeError("db down"))
    redis = _ScanRedis({f"slack_channels_types:{connector.id}": "public_channel"})

    conflict, discard = await _create_into_same_workspace_conflict(
        db=db, workspace_id=workspace_id, connector=connector, redis=redis
    )

    assert conflict.details["reason"] == "connector_team_connected_here"
    assert "token_refreshed" not in conflict.details
    assert db.rollback.await_count == 2
    discard.assert_not_awaited()
    assert redis.store == {f"slack_channels_types:{connector.id}": "public_channel"}


@pytest.mark.asyncio
async def test_create_rejects_invalid_pii_guardrail_config_before_calling_service():
    # #866: a malformed pii_guardrail_config (typo'd key) must be rejected at the
    # provision path with a 422, before the service / DB is touched.
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
        pii_guardrail_config={"enabled": True, "detector": ["EMAIL_ADDRESS"]},  # typo: detector
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.provision_connector = AsyncMock()
        with pytest.raises(MemoryCloudException) as exc:
            await create_workspace_connector(request, _HTTP, BackgroundTasks(), admin, db)

    assert exc.value.status_code == 422
    assert exc.value.error_code == "VAL-001"  # canonical validation code, not a one-off
    service_cls.return_value.provision_connector.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_passes_normalized_pii_guardrail_config_dict_to_service():
    # Valid config is normalized (defaults materialized) and handed to the service
    # as a plain dict for JSONB storage — not a Pydantic model.
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
        pii_guardrail_config={"enabled": True, "detectors": ["EMAIL_ADDRESS"]},
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    result = MagicMock()
    result.connector.id = uuid4()
    result.connector.connector_type = "slack"
    result.connector.app_key = "default"
    result.resource_id = "slack_general"
    result.context_id = None
    result.plaintext_kmc_api_key = None
    result.token.id = 1
    result.token.public_id = "rtok_" + "1" * 22
    result.plaintext_token = "kagura_resource_x"
    result.token.quota_events_per_hour = 1000

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.provision_connector = AsyncMock(return_value=result)
        await create_workspace_connector(request, _HTTP, BackgroundTasks(), admin, db)

    kwargs = service_cls.return_value.provision_connector.await_args.kwargs
    assert kwargs["pii_guardrail_config"] == {
        "enabled": True,
        "detectors": ["EMAIL_ADDRESS"],
        "redaction": "mask",
        "locale": "en",
        "fail_closed": True,
    }


@pytest.mark.asyncio
async def test_create_passes_normalized_runtime_config_to_service():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
        runtime={"vision_enabled": False},
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    result = MagicMock()
    result.connector.id = uuid4()
    result.connector.connector_type = "slack"
    result.connector.app_key = "default"
    result.resource_id = "slack_general"
    result.context_id = None
    result.plaintext_kmc_api_key = None
    result.token.id = 1
    result.token.public_id = "rtok_" + "1" * 22
    result.plaintext_token = "kagura_resource_x"
    result.token.quota_events_per_hour = 1000

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.provision_connector = AsyncMock(return_value=result)
        await create_workspace_connector(request, _HTTP, BackgroundTasks(), admin, db)

    runtime = service_cls.return_value.provision_connector.await_args.kwargs["runtime_config"]
    assert runtime["vision_enabled"] is False
    assert runtime["buffer"] == {"ttl_seconds": 86400, "max_len": 10_000}


def test_create_rejects_process_owned_runtime_fields_at_rest_boundary():
    with pytest.raises(PydanticValidationError):
        WorkspaceConnectorCreateRequest(
            connector_type="slack",
            resource_id="slack_general",
            runtime={"buffer": {"redis_url": "redis://tenant.invalid:6379/0"}},
        )


@pytest.mark.asyncio
async def test_update_runtime_commits_revision_and_returns_normalized_config():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from services.connector_provisioning import ConnectorRuntimeUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    workspace_id = uuid4()
    connector_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False})

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_runtime_config = AsyncMock(
            return_value=ConnectorRuntimeUpdateResult(
                runtime_config=request.runtime.model_dump(mode="json"),
                config_version=4,
            )
        )
        response = await update_workspace_connector_runtime(connector_id, request, admin, db)

    assert response.connector_id == connector_id
    assert response.runtime.vision_enabled is False
    assert response.stored is True
    assert response.config_version == 4
    db.commit.assert_awaited_once()
    service_cls.return_value.update_runtime_config.assert_awaited_once_with(
        workspace_id=workspace_id,
        connector_id=connector_id,
        runtime_config=request.runtime.model_dump(mode="json"),
        user_id="user-1",
        expected_config_version=None,
    )


@pytest.mark.asyncio
async def test_update_runtime_hides_cross_workspace_connector_as_not_found():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from utils.exceptions import NotFoundException

    db = MagicMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_runtime_config = AsyncMock(
            side_effect=NotFoundException("Connector", "hidden")
        )
        with pytest.raises(NotFoundException) as exc:
            await update_workspace_connector_runtime(
                uuid4(),
                WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False}),
                admin,
                db,
            )

    assert exc.value.status_code == 404
    assert exc.value.message == "Connector not found"
    assert "hidden" not in exc.value.message
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_runtime_requires_selected_workspace():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from utils.exceptions import BadRequestError

    with pytest.raises(BadRequestError) as exc:
        await update_workspace_connector_runtime(
            uuid4(),
            WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False}),
            {"user_id": "user-1", "current_workspace_id": None},
            MagicMock(),
        )

    assert exc.value.status_code == 400
    assert exc.value.error_code == "REQ-001"


@pytest.mark.asyncio
async def test_update_runtime_maps_unexpected_failure_to_canonical_internal_error():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from utils.exceptions import InternalError

    db = MagicMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls,
        patch("api.routes.workspace_connectors.logger.error") as log_error,
    ):
        service_cls.return_value.update_runtime_config = AsyncMock(
            side_effect=RuntimeError("secret-bearing internal detail")
        )
        with pytest.raises(InternalError) as exc:
            await update_workspace_connector_runtime(
                uuid4(),
                WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False}),
                admin,
                db,
            )

    assert exc.value.status_code == 500
    assert exc.value.error_code == "INT-001"
    assert exc.value.message == "Failed to update connector runtime"
    assert "secret-bearing" not in str(log_error.call_args)
    assert log_error.call_args.kwargs["error_type"] == "RuntimeError"
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_workspace_connectors_returns_summaries():
    from types import SimpleNamespace

    from api.routes.workspace_connectors import list_workspace_connectors

    db = MagicMock()
    ws_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": ws_id}

    c = MagicMock()
    c.id = uuid4()
    c.connector_type = "slack"
    c.app_key = "default"
    c.context_id = uuid4()
    c.config_version = 1
    c.runtime_config = None
    c.created_at = datetime(2026, 6, 2, 0, 0, 0)
    c.created_by = "user-1"
    # #1376: settings surfaced for the admin-card presence indicators.
    c.channel_ids = ["C01"]
    c.locale = "ja"
    c.litellm_virtual_key_id = None
    c.llm_config_encrypted = "ENC:x"
    c.external_team_id = "T0123ABC"

    # list_connectors now returns ConnectorListItem(connector, resource_id) so the
    # summary exposes the public slug, not the internal resource_pk DB key (#991).
    # #1389: the human-readable identity (resource label + context name) rides
    # the same list item.
    item = SimpleNamespace(
        connector=c,
        resource_id="my-resource-slug",
        display_name="Sales Slack / T0123ABC",
        context_name="slack-sales",
        # #1449: ingest outcome for the row.
        last_memory_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        memories_last_7d=0,
        ingest_context_shared=False,
    )

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.list_connectors = AsyncMock(return_value=[item])
        result = await list_workspace_connectors(admin, db)

    assert len(result) == 1
    assert result[0].connector_id == c.id
    assert result[0].connector_type == "slack"
    assert result[0].app_key == "default"
    assert result[0].resource_id == "my-resource-slug"
    assert not hasattr(result[0], "resource_pk")
    # #1376: presence indicators for the admin card; the LLM bundle itself is
    # write-only and must never be listed.
    assert result[0].channel_ids == ["C01"]
    assert result[0].locale == "ja"
    assert result[0].llm_config_present is True
    assert result[0].litellm_virtual_key_id is None
    assert "ENC:" not in result[0].model_dump_json()
    # #1389: human-readable row identity is exposed additively.
    assert result[0].display_name == "Sales Slack / T0123ABC"
    assert result[0].external_team_id == "T0123ABC"
    assert result[0].context_name == "slack-sales"
    # #1449: the outage that motivated this looked exactly like the row below —
    # a connector listing as normal whose context had not been written to in
    # days. The fact is carried on the row; nothing here grades it.
    assert result[0].last_memory_at == datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    assert result[0].memories_last_7d == 0
    # TZAwareBaseModel: the wire form must carry the UTC marker or a JST client
    # renders the staleness 9 hours off.
    assert "2026-07-19T12:00:00Z" in result[0].model_dump_json()
    service_cls.return_value.list_connectors.assert_awaited_once_with(ws_id)


@pytest.mark.asyncio
async def test_list_workspace_connectors_400_without_workspace():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import list_workspace_connectors

    db = MagicMock()
    admin = {"user_id": "user-1", "current_workspace_id": None}

    with pytest.raises(HTTPException) as exc:
        await list_workspace_connectors(admin, db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_list_available_worker_apps_returns_active_non_secret_metadata():
    from api.routes.workspace_connectors import list_available_worker_apps

    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}
    identity = MagicMock()
    identity.platform = "slack"
    identity.app_key = "sales"
    identity.display_name = "Sales Slack App"

    with patch("services.worker_app_identity.WorkerAppIdentityService") as service_cls:
        service_cls.return_value.list_identities = AsyncMock(return_value=[identity])
        result = await list_available_worker_apps(admin, MagicMock())

    assert [item.model_dump() for item in result] == [
        {
            "platform": "slack",
            "app_key": "sales",
            "display_name": "Sales Slack App",
        }
    ]
    assert "signing_secret" not in result[0].model_dump()
    service_cls.return_value.list_identities.assert_awaited_once_with(active_only=True)


@pytest.mark.asyncio
async def test_delete_workspace_connector_204_on_success():
    from api.routes.workspace_connectors import delete_workspace_connector

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    ws_id = uuid4()
    conn_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": ws_id}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.delete_connector = AsyncMock(return_value=True)
        resp = await delete_workspace_connector(conn_id, admin, db)

    assert resp.status_code == 204
    db.commit.assert_awaited_once()
    service_cls.return_value.delete_connector.assert_awaited_once_with(ws_id, conn_id)


@pytest.mark.asyncio
async def test_delete_workspace_connector_404_when_missing():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import delete_workspace_connector

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.delete_connector = AsyncMock(return_value=False)
        with pytest.raises(HTTPException) as exc:
            await delete_workspace_connector(uuid4(), admin, db)

    assert exc.value.status_code == 404
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


# --- rotate-kmc-key (#892) ---


@pytest.mark.asyncio
async def test_rotate_kmc_key_returns_new_key_on_success():

    from api.routes.workspace_connectors import rotate_connector_kmc_key
    from services.connector_provisioning import KmcKeyRotationResult

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    ws_id = uuid4()
    conn_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": ws_id}
    expires = datetime(2099, 1, 1)

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.rotate_kmc_key = AsyncMock(
            return_value=KmcKeyRotationResult(
                plaintext_kmc_api_key="kmc-new-plaintext",
                expires_at=expires,
                config_version=3,
            )
        )
        resp = await rotate_connector_kmc_key(conn_id, _HTTP, BackgroundTasks(), admin, db)

    assert resp.connector_id == conn_id
    assert resp.kmc_api_key == "kmc-new-plaintext"
    assert resp.kmc_api_key_expires_at == expires
    assert resp.config_version == 3
    db.commit.assert_awaited_once()
    service_cls.return_value.rotate_kmc_key.assert_awaited_once_with(
        workspace_id=ws_id, connector_id=conn_id, user_id="user-1"
    )


@pytest.mark.asyncio
async def test_rotate_kmc_key_404_when_connector_missing():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import rotate_connector_kmc_key
    from utils.exceptions import NotFoundException

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.rotate_kmc_key = AsyncMock(
            side_effect=NotFoundException("Connector", str(uuid4()))
        )
        with pytest.raises(HTTPException) as exc:
            await rotate_connector_kmc_key(uuid4(), _HTTP, BackgroundTasks(), admin, db)

    assert exc.value.status_code == 404
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotate_kmc_key_422_when_no_kmc_key():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import rotate_connector_kmc_key
    from utils.exceptions import ValidationError

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.rotate_kmc_key = AsyncMock(
            side_effect=ValidationError("Connector has no KMC write key")
        )
        with pytest.raises(HTTPException) as exc:
            await rotate_connector_kmc_key(uuid4(), _HTTP, BackgroundTasks(), admin, db)

    assert exc.value.status_code == 422
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_settings_route_passes_only_provided_fields():
    """#1376: absent request fields never reach the service (PATCH semantics)."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from services.connector_provisioning import ConnectorSettingsUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    workspace_id = uuid4()
    connector_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate(
        {"channel_ids": ["C01"], "expected_config_version": 3}
    )

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            return_value=ConnectorSettingsUpdateResult(
                channel_ids=["C01"],
                litellm_virtual_key_id=None,
                llm_config_present=False,
                locale=None,
                config_version=4,
                context_id=None,
            )
        )
        response = await update_workspace_connector_settings(connector_id, request, admin, db)

    assert response.connector_id == connector_id
    assert response.channel_ids == ["C01"]
    assert response.config_version == 4
    db.commit.assert_awaited_once()
    service_cls.return_value.update_connector_settings.assert_awaited_once_with(
        workspace_id=workspace_id,
        connector_id=connector_id,
        user_id="user-1",
        expected_config_version=3,
        channel_ids=["C01"],
    )


@pytest.mark.asyncio
async def test_update_settings_route_distinguishes_explicit_null_from_absent():
    """#1376: explicit null (clear) is forwarded; untouched fields are not."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from services.connector_provisioning import ConnectorSettingsUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    workspace_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate(
        {"llm_config": None, "locale": None}
    )

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            return_value=ConnectorSettingsUpdateResult(
                channel_ids=["C-kept"],
                litellm_virtual_key_id="vk-kept",
                llm_config_present=False,
                locale=None,
                config_version=9,
                context_id=None,
            )
        )
        await update_workspace_connector_settings(uuid4(), request, admin, db)

    kwargs = service_cls.return_value.update_connector_settings.await_args.kwargs
    assert kwargs["llm_config"] is None
    assert kwargs["locale"] is None
    assert "channel_ids" not in kwargs
    assert "litellm_virtual_key_id" not in kwargs


@pytest.mark.asyncio
async def test_update_settings_response_never_echoes_llm_config():
    """#1376: the LLM bundle is write-only — only a presence flag comes back."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from services.connector_provisioning import ConnectorSettingsUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate(
        {"llm_config": {"provider": "openai", "model": "gpt", "api_key": "sk-secret"}}
    )

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            return_value=ConnectorSettingsUpdateResult(
                channel_ids=None,
                litellm_virtual_key_id=None,
                llm_config_present=True,
                locale=None,
                config_version=2,
                context_id=None,
            )
        )
        response = await update_workspace_connector_settings(uuid4(), request, admin, db)

    assert response.llm_config_present is True
    assert "sk-secret" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_update_settings_route_forwards_context_id_repoint():
    """#1428: a context_id in the PATCH body reaches the service and the new
    binding comes back in the response."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from services.connector_provisioning import ConnectorSettingsUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    workspace_id = uuid4()
    new_ctx = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate({"context_id": str(new_ctx)})

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            return_value=ConnectorSettingsUpdateResult(
                channel_ids=["C-kept"],
                litellm_virtual_key_id=None,
                llm_config_present=False,
                locale=None,
                config_version=5,
                context_id=new_ctx,
            )
        )
        response = await update_workspace_connector_settings(uuid4(), request, admin, db)

    kwargs = service_cls.return_value.update_connector_settings.await_args.kwargs
    assert kwargs["context_id"] == new_ctx
    assert response.context_id == new_ctx
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_settings_route_rolls_back_on_conflict():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from utils.exceptions import ConflictError

    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate({"channel_ids": ["C01"]})

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            side_effect=ConflictError("stale")
        )
        with pytest.raises(ConflictError):
            await update_workspace_connector_settings(uuid4(), request, admin, db)

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_settings_route_context_not_found_not_masked_as_connector():
    """#1428: a bad context_id surfaces as "Context" NotFound, not masked as
    "Connector" by the route's catch-all (regression the re-point path exposes)."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from utils.exceptions import NotFoundException

    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    bad_ctx = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate({"context_id": str(bad_ctx)})

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            side_effect=NotFoundException("Context", str(bad_ctx))
        )
        with pytest.raises(NotFoundException) as exc:
            await update_workspace_connector_settings(uuid4(), request, admin, db)

    assert "Context" in str(exc.value)
    assert "Connector" not in str(exc.value)
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_update_settings_request_forbids_unknown_fields():
    """#1376 review: a silently-dropped typo'd field name would read as a
    successful partial update — unknown keys must 422 at the request layer."""
    from api.routes.workspace_connectors import WorkspaceConnectorSettingsUpdateRequest

    with pytest.raises(PydanticValidationError):
        WorkspaceConnectorSettingsUpdateRequest.model_validate({"locail": "ja"})
