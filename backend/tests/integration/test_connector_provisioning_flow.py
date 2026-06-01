"""DB-backed connector provisioning flow tests (Issue #851, F6-b of #755)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.resource_ingest import ingest_event, verify_resource_token
from auth.workspace_roles import WorkspaceRole
from models.auth import Workspace, WorkspaceMember
from models.resource import Resource, ResourceEvent, ResourceToken, WorkspaceConnector
from models.schemas import ResourceEventRequest
from services.connector_provisioning import ConnectorProvisioningService


async def _seed_workspace(
    db: AsyncSession,
    *,
    plan_name: str = "basic",
) -> tuple[str, Workspace]:
    user_id = f"user-{uuid4().hex}"
    workspace = Workspace(
        name=f"ws-{uuid4().hex[:8]}",
        owner_user_id=user_id,
        plan_name=plan_name,
    )
    db.add(workspace)
    await db.flush()
    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user_id,
        role=WorkspaceRole.OWNER,
    )
    db.add(member)
    await db.flush()
    return user_id, workspace


class TestConnectorProvisioningDbFlow:
    @pytest.mark.asyncio
    async def test_token_failure_rolls_back_resource_and_connector_rows(
        self,
        db_session: AsyncSession,
    ):
        """AC1: mid-flow failure leaves no Resource or WorkspaceConnector orphan."""
        user_id, workspace = await _seed_workspace(db_session)
        resource_id = f"slack_{uuid4().hex[:8]}"

        with patch(
            "services.connector_provisioning.ResourceTokenManager.create_token",
            new=AsyncMock(side_effect=RuntimeError("token mint failed")),
        ):
            with pytest.raises(RuntimeError, match="token mint failed"):
                await ConnectorProvisioningService(db_session).provision_connector(
                    workspace_id=workspace.id,
                    user_id=user_id,
                    connector_type="slack",
                    resource_id=resource_id,
                )

        await db_session.rollback()

        resource_count = (
            await db_session.execute(
                select(func.count(Resource.id)).where(
                    Resource.workspace_id == workspace.id,
                    Resource.resource_id == resource_id,
                )
            )
        ).scalar_one()
        connector_count = (
            await db_session.execute(
                select(func.count(WorkspaceConnector.id)).where(
                    WorkspaceConnector.workspace_id == workspace.id
                )
            )
        ).scalar_one()

        assert resource_count == 0
        assert connector_count == 0

    @pytest.mark.asyncio
    async def test_provision_then_ingest_event_writes_resource_pk_and_workspace(
        self,
        db_session: AsyncSession,
    ):
        """AC5: provision connector, ingest via token, then assert event binding."""
        user_id, workspace = await _seed_workspace(db_session)
        resource_id = f"slack_{uuid4().hex[:8]}"

        result = await ConnectorProvisioningService(db_session).provision_connector(
            workspace_id=workspace.id,
            user_id=user_id,
            connector_type="slack",
            resource_id=resource_id,
        )
        await db_session.commit()

        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        auth = await verify_resource_token(
            resource_id,
            request,
            x_resource_api_key=result.plaintext_token,
            db=db_session,
        )
        event_request = ResourceEventRequest(
            op="upsert",
            doc_id="1717000000.000100",
            version=1,
            payload={"text": "hello from slack"},
            idempotency_key=f"{result.connector.id}:summary-1",
        )

        with (
            patch("api.routes.resource_ingest.check_event_quota", new=AsyncMock()),
            patch("api.routes.resource_ingest._schedule_indexer_for_resource", new=AsyncMock()),
            patch("utils.usage_logger.log_usage", new=AsyncMock()),
        ):
            response = await ingest_event(
                resource_id,
                event_request,
                auth=auth,
                db=db_session,
            )

        row = (
            await db_session.execute(
                select(ResourceEvent, Resource.workspace_id)
                .join(Resource, Resource.id == ResourceEvent.resource_pk)
                .where(ResourceEvent.id == response.event_id)
            )
        ).one()
        event, event_workspace_id = row

        assert event.resource_pk == result.resource_pk
        assert event.resource_id == resource_id
        assert event_workspace_id == workspace.id
        assert (
            await db_session.execute(
                select(func.count(ResourceToken.id)).where(
                    ResourceToken.resource_pk == result.resource_pk
                )
            )
        ).scalar_one() == 1

