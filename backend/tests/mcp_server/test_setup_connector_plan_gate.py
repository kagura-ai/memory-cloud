"""MCP ``setup_connector`` plan gate (#1551) — mirror of TestSetupResourcePlanGating.

Connectors are XL-only to CREATE. The refusal raised inside
``ConnectorProvisioningService`` must surface through the MCP handler under
the same ``plan_required`` envelope ``setup_resource`` / ``update_context``
use — code, registry-derived message and ``required_plan`` — with nothing
staged or committed. On promax the gate passes and the real service proceeds.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.resource import handle_setup_connector


def _result(*, one=None, scalar=None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalar.return_value = scalar
    return result


def _db(execute_results: list) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute_results)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _get_db_for(db: MagicMock):
    async def _gen():
        yield db

    return lambda: _gen()


_ARGS = {"connector_type": "slack", "resource_id": "slack_general"}


@pytest.fixture(autouse=True)
def _quiet_handler_side_effects():
    with (
        patch(
            "mcp_server.tools.resource._check_owner_admin_role", new=AsyncMock(return_value=None)
        ),
        patch("mcp_server.tools.resource._log_tool_usage", new=AsyncMock()),
        patch("services.worker_app_identity.WorkerAppIdentityService") as identity,
    ):
        identity.return_value.get_identity = AsyncMock(return_value=None)
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_name", ["free", "basic", "pro"])
async def test_non_xl_plan_denied_with_plan_required_envelope(plan_name: str) -> None:
    # M/L keep positive seat caps for existing connectors; the feature gate
    # refuses before the cap is even read.
    db = _db([_result(one=SimpleNamespace(plan_name=plan_name, effective_max_connectors=10))])

    with patch("db.base.get_db", side_effect=_get_db_for(db)):
        result = await handle_setup_connector(_ARGS, "user-1", uuid4())

    payload = json.loads(result[0].text)
    assert payload["error"] == "plan_required"
    assert payload["required_plan"] == "promax"
    assert payload["feature"] == "connectors"
    assert "XL" in payload["message"]
    assert "PRO plan" not in payload["message"]
    # Refused at the gate: only the workspace lookup ran, nothing staged.
    assert db.execute.await_count == 1
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_promax_passes_the_gate_and_provisions() -> None:
    resource_pk = uuid4()
    token = SimpleNamespace(id=123, quota_events_per_hour=1000)
    # Same execute sequence as the service-level happy path
    # (tests/services/test_connector_provisioning.py): workspace, the three
    # advisory-lock statements, seat count, existing-connector probe, canonical
    # chat schema probe, ON CONFLICT insert.
    db = _db(
        [
            _result(one=SimpleNamespace(plan_name="promax", effective_max_connectors=50)),
            _result(),
            _result(),
            _result(),
            _result(scalar=0),
            _result(one=None),
            _result(one=None),
            _result(),
        ]
    )

    with (
        patch("db.base.get_db", side_effect=_get_db_for(db)),
        patch(
            "services.connector_provisioning.resolve_resource_pk",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "services.connector_provisioning.upsert_resource",
            new=AsyncMock(return_value=resource_pk),
        ),
        patch(
            "services.connector_provisioning.ResourceTokenManager.create_token",
            new=AsyncMock(return_value=("kagura_resource_plain", token)),
        ),
    ):
        result = await handle_setup_connector(_ARGS, "user-1", uuid4())

    payload = json.loads(result[0].text)
    assert payload["status"] == "success", payload
    assert payload["token"] == "kagura_resource_plain"
    assert payload["resource_pk"] == str(resource_pk)
    db.commit.assert_awaited_once()
