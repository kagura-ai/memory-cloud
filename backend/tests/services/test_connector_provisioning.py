"""Tests for connector provisioning service (Issue #851, F6-b of #755)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.connector_provisioning import (
    ConnectorProvisioningService,
    validate_connector_idempotency_key,
)
from utils.exceptions import ConflictError, MemoryCloudException, ValidationError


def _result(*, one=None, scalar=None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalar.return_value = scalar
    return result


class TestConnectorProvisioningService:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_free_plan_zero_connector_cap_blocks_before_writes(self, mock_db):
        workspace_id = uuid4()
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=0)),
            _result(scalar=0),
        ]

        with patch("services.connector_provisioning.upsert_resource", new=AsyncMock()) as upsert:
            with pytest.raises(MemoryCloudException) as exc_info:
                await ConnectorProvisioningService(mock_db).provision_connector(
                    workspace_id=workspace_id,
                    user_id="user-1",
                    connector_type="slack",
                    resource_id="slack_general",
                )

        assert exc_info.value.status_code == 403
        assert exc_info.value.error_code == "CONNECTOR-001"
        upsert.assert_not_awaited()
        mock_db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_basic_plan_at_cap_blocks_paid_boundary(self, mock_db):
        """AC2: cap=1 and active=1 must reject; >= cannot regress to >."""
        workspace_id = uuid4()
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=1)),
            _result(scalar=1),
        ]

        with patch("services.connector_provisioning.upsert_resource", new=AsyncMock()) as upsert:
            with pytest.raises(MemoryCloudException) as exc_info:
                await ConnectorProvisioningService(mock_db).provision_connector(
                    workspace_id=workspace_id,
                    user_id="user-1",
                    connector_type="slack",
                    resource_id="slack_general",
                )

        assert exc_info.value.status_code == 403
        assert exc_info.value.details["max_connectors"] == 1
        assert exc_info.value.details["active_connectors"] == 1
        upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_basic_plan_below_cap_allows_provision(self, mock_db):
        """AC2: positive cap with active_count below boundary must proceed."""
        workspace_id = uuid4()
        resource_pk = uuid4()
        token = SimpleNamespace(id=123, quota_events_per_hour=1000)
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=1)),
            _result(scalar=0),
            _result(one=None),
        ]

        with (
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
            result = await ConnectorProvisioningService(mock_db).provision_connector(
                workspace_id=workspace_id,
                user_id="user-1",
                connector_type="slack",
                resource_id="slack_general",
            )

        assert result.resource_pk == resource_pk
        assert result.token is token

    @pytest.mark.asyncio
    async def test_basic_connector_token_bypasses_resource_token_gate(self, mock_db):
        """Regression: connector tokens are seat-gated, not max_resource_tokens-gated."""
        workspace_id = uuid4()
        resource_pk = uuid4()
        token = SimpleNamespace(id=123, quota_events_per_hour=1000)
        mock_db.execute.side_effect = [
            _result(
                one=SimpleNamespace(effective_max_connectors=1, effective_max_resource_tokens=0)
            ),
            _result(scalar=0),
            _result(one=None),
        ]

        with (
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
            ) as create_token,
        ):
            result = await ConnectorProvisioningService(mock_db).provision_connector(
                workspace_id=workspace_id,
                user_id="user-1",
                connector_type="slack",
                resource_id="slack_general",
            )

        assert result.plaintext_token == "kagura_resource_plain"
        assert result.token is token
        create_token.assert_awaited_once()
        assert mock_db.add.call_args.args[0].resource_pk == resource_pk

    @pytest.mark.asyncio
    async def test_existing_connector_for_resource_rejects_duplicate(self, mock_db):
        workspace_id = uuid4()
        resource_pk = uuid4()
        connector_id = uuid4()
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=5)),
            _result(scalar=0),
            _result(one=SimpleNamespace(id=connector_id)),
        ]

        with (
            patch(
                "services.connector_provisioning.resolve_resource_pk",
                new=AsyncMock(return_value=resource_pk),
            ),
            patch(
                "services.connector_provisioning.upsert_resource",
                new=AsyncMock(return_value=resource_pk),
            ),
            patch(
                "services.connector_provisioning.ResourceTokenManager.create_token"
            ) as create_token,
        ):
            with pytest.raises(ConflictError):
                await ConnectorProvisioningService(mock_db).provision_connector(
                    workspace_id=workspace_id,
                    user_id="user-1",
                    connector_type="slack",
                    resource_id="slack_general",
                )

        create_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_non_connector_resource_slug_is_rejected(self, mock_db):
        """Small guard: a regular resource slug must not become connector-owned."""
        workspace_id = uuid4()
        resource_pk = uuid4()
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=5)),
            _result(scalar=0),
            _result(one=None),
        ]

        with (
            patch(
                "services.connector_provisioning.resolve_resource_pk",
                new=AsyncMock(return_value=resource_pk),
            ),
            patch(
                "services.connector_provisioning.upsert_resource",
                new=AsyncMock(return_value=resource_pk),
            ),
            patch(
                "services.connector_provisioning.ResourceTokenManager.create_token"
            ) as create_token,
        ):
            with pytest.raises(ConflictError) as exc_info:
                await ConnectorProvisioningService(mock_db).provision_connector(
                    workspace_id=workspace_id,
                    user_id="user-1",
                    connector_type="slack",
                    resource_id="existing_resource",
                )

        assert "not connector-owned" in exc_info.value.message
        create_token.assert_not_called()


class TestConnectorIdempotencyKey:
    def test_non_connector_resources_are_unchanged(self):
        validate_connector_idempotency_key(connector_id=None, idempotency_key=None)

    def test_accepts_connector_prefix(self):
        connector_id = uuid4()
        validate_connector_idempotency_key(
            connector_id=connector_id,
            idempotency_key=f"{connector_id}:summary-123",
        )

    def test_rejects_missing_or_wrong_prefix(self):
        connector_id = uuid4()
        with pytest.raises(ValidationError):
            validate_connector_idempotency_key(connector_id=connector_id, idempotency_key=None)
        with pytest.raises(ValidationError):
            validate_connector_idempotency_key(
                connector_id=connector_id,
                idempotency_key=f"{uuid4()}:summary-123",
            )
