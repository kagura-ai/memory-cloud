"""The slot gate on credentials without a membership (#2918), without a database.

* A management grant (``current_binding``) is refused for a suspended
  workspace in the redacted form, after every binding check.
* The connector worker's config answers not-ready for a suspended workspace,
  before the conditional GET (a suspension bumps no ``config_version``).
* The public context info is refused in the redacted form, like ``/search``.

The rows-backed matrix (share keys, resource tokens, observation credentials,
the sync worker, public serving, the REST principals) is in
``tests/integration/test_workspace_slot_gate_db.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.auth import Workspace
from utils.exceptions import WorkspaceSlotSuspendedError


def _mode(mode: str):
    return patch("services.workspace_slots.workspace_slot_mode", return_value=mode)


def _suspended(*ids):
    return patch(
        "services.workspace_slot_gate.suspended_workspace_ids",
        AsyncMock(return_value=frozenset(ids)),
    )


def _result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.first.return_value = value
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "refused"), [("enforce", True), ("shadow", False)])
async def test_a_management_grant_is_refused_for_a_suspended_workspace(mode, refused) -> None:
    from auth import management_grants

    wid, resource_id, ctx_id, endpoint_id = uuid4(), uuid4(), uuid4(), uuid4()
    workspace = Workspace(id=wid, name="w", plan_name="pro", owner_user_id="o")
    member = SimpleNamespace(role="owner")
    resource = SimpleNamespace(id=resource_id, resource_id="res")
    context = SimpleNamespace(id=ctx_id, is_private=False, is_public=True, resource_id="res")
    endpoint = SimpleNamespace(
        resource_pk=resource_id, context_id=ctx_id, workspace_id=wid, resource_id="res"
    )
    schema = SimpleNamespace(schema_version=1, field_definitions=[])
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(workspace),
            _result(member),
            _result(resource),
            _result(context),
            _result(endpoint),
            _result(None),  # no connector on the resource
            _result(schema),
        ]
    )
    grant = SimpleNamespace(
        workspace_id=wid,
        created_by="o",
        resource_pk=resource_id,
        context_id=ctx_id,
        endpoint_id=endpoint_id,
        schema_version=1,
        schema_hash="h",
        approved_fields=[],
    )
    with (
        _mode(mode),
        _suspended(wid),
        patch.object(management_grants, "has_feature", return_value=True),
        patch.object(management_grants, "public_endpoints_enabled", return_value=True),
        patch.object(management_grants, "digest", return_value="h"),
        patch.object(management_grants, "public_fields", return_value=[]),
    ):
        if refused:
            with pytest.raises(WorkspaceSlotSuspendedError) as exc:
                await management_grants.current_binding(db, grant)
            assert exc.value.details == {"gate": "workspace_slot"}
        else:
            assert (await management_grants.current_binding(db, grant))[1] is workspace


@pytest.mark.asyncio
async def test_the_worker_config_is_not_ready_for_a_suspended_workspace() -> None:
    from fastapi import Response

    import api.routes.workers as workers
    from utils.exceptions import WorkerConnectorNotReadyError

    connector = SimpleNamespace(id=uuid4(), context_id=uuid4(), workspace_id=uuid4())
    provisioning = MagicMock(get_connector_for_dispatch=AsyncMock(return_value=connector))
    slot = AsyncMock(return_value=True)
    with (
        patch.object(
            workers,
            "WorkerAppIdentityService",
            return_value=MagicMock(get_identity=AsyncMock(return_value=None)),
        ),
        patch.object(workers, "ConnectorProvisioningService", return_value=provisioning),
        patch.object(workers, "_default_context_live", AsyncMock(return_value=True)),
        patch.object(workers, "_connectors_suspended_for", AsyncMock(return_value=False)),
        patch.object(workers, "slot_suspended", slot),
        pytest.raises(WorkerConnectorNotReadyError),
    ):
        await workers.get_worker_config(
            response=Response(),
            platform="slack",
            team_id="T1",
            app_key=None,
            capabilities=None,
            # The suspended answer comes before the ETag check.
            if_none_match='"cached"',
            _=None,
            db=MagicMock(),
        )
    slot.assert_awaited_once()
    assert slot.await_args.args[1] == connector.workspace_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "refused"), [("enforce", True), ("off", False)])
async def test_the_public_context_info_is_refused_in_the_redacted_form(mode, refused) -> None:
    import api.routes.public_search as public_search

    wid = uuid4()
    context = SimpleNamespace(
        id=uuid4(),
        workspace_id=wid,
        is_public=True,
        resource_id=None,
        name="n",
        display_name="n",
        description=None,
        created_at=None,
        updated_at=None,
    )
    workspace = Workspace(id=wid, name="w", owner_user_id="o", plan_name="pro")
    db = MagicMock()
    db.get = AsyncMock(side_effect=[context, workspace])
    with (
        _mode(mode),
        _suspended(wid),
        patch.object(public_search, "_resolve_public_attribution", AsyncMock()),
        patch.object(public_search, "ensure_public_serving_allowed"),
    ):
        if refused:
            with pytest.raises(WorkspaceSlotSuspendedError) as exc:
                await public_search.get_public_context_info(context.id, api_key=None, db=db)
            assert exc.value.details == {"gate": "workspace_slot"}
        else:
            body = await public_search.get_public_context_info(context.id, api_key=None, db=db)
            assert body["context_id"] == str(context.id)
