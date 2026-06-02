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


@pytest.mark.asyncio
async def test_registration_path_creates_context_and_mints_kmc_key(
    db_session: AsyncSession,
):
    """Spec 2026-06-02: auto_create_context_name → context bound + KMC key minted.

    The connector gets a write-target context, a workspace-scoped API key is
    minted and stored Fernet-encrypted (retrievable via get_kmc_api_key), and
    the plaintext is surfaced once in the result.
    """
    from models.auth import APIKey, Context

    user_id, workspace = await _seed_workspace(db_session, plan_name="basic")
    resource_id = f"slack_{uuid4().hex[:8]}"
    ctx_name = f"slack-{uuid4().hex[:8]}"

    result = await ConnectorProvisioningService(db_session).provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=resource_id,
        auto_create_context_name=ctx_name,
        llm_config={"provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-test"},
        channel_ids=["C01EXAMPLE"],
        locale="ja",
    )
    await db_session.flush()

    # Context created and bound.
    assert result.context_id is not None
    ctx = (
        await db_session.execute(select(Context).where(Context.id == result.context_id))
    ).scalar_one()
    assert ctx.name == ctx_name
    assert ctx.workspace_id == workspace.id

    # Connector carries the new config + encrypted secrets round-trip.
    assert result.connector.context_id == result.context_id
    assert result.connector.locale == "ja"
    assert result.connector.channel_ids == ["C01EXAMPLE"]
    assert result.connector.get_llm_config()["api_key"] == "sk-test"

    # KMC write key minted, surfaced once, stored encrypted (retrievable).
    assert result.plaintext_kmc_api_key
    assert result.connector.get_kmc_api_key() == result.plaintext_kmc_api_key
    key_count = (
        await db_session.execute(
            select(func.count(APIKey.id)).where(APIKey.workspace_id == workspace.id)
        )
    ).scalar_one()
    assert key_count == 1


@pytest.mark.asyncio
async def test_delete_connector_revokes_kmc_key_and_removes_connector(
    db_session: AsyncSession,
):
    """Spec 2026-06-02: delete revokes the KMC key + drops the connector row.

    The write-target context is preserved (user data); the KMC API key is
    revoked (revoked_at set) so the worker fails closed on next config fetch.
    """
    from models.auth import APIKey, Context

    user_id, workspace = await _seed_workspace(db_session, plan_name="basic")
    result = await ConnectorProvisioningService(db_session).provision_connector(
        workspace_id=workspace.id,
        user_id=user_id,
        connector_type="slack",
        resource_id=f"slack_{uuid4().hex[:8]}",
        auto_create_context_name=f"slack-{uuid4().hex[:8]}",
    )
    await db_session.flush()
    connector_id = result.connector.id
    context_id = result.context_id

    deleted = await ConnectorProvisioningService(db_session).delete_connector(
        workspace.id, connector_id
    )
    await db_session.flush()
    assert deleted is True

    # Connector row gone.
    remaining = (
        await db_session.execute(
            select(func.count(WorkspaceConnector.id)).where(
                WorkspaceConnector.id == connector_id
            )
        )
    ).scalar_one()
    assert remaining == 0

    # KMC key revoked (not hard-deleted).
    key = (
        await db_session.execute(
            select(APIKey).where(
                APIKey.workspace_id == workspace.id,
                APIKey.name == f"connector:{connector_id}",
            )
        )
    ).scalar_one()
    assert key.revoked_at is not None

    # Context preserved.
    ctx_count = (
        await db_session.execute(select(func.count(Context.id)).where(Context.id == context_id))
    ).scalar_one()
    assert ctx_count == 1
