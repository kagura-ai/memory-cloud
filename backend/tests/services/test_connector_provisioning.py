"""Tests for connector provisioning service (Issue #851, F6-b of #755)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

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


def _lock_results():
    """Mock execute() results for the #857 advisory-lock acquire sequence.

    ``_acquire_connector_seat_lock`` issues three statements before the
    seat-count SELECT — ``SET LOCAL lock_timeout='5s'``, the
    ``pg_advisory_xact_lock`` SELECT, then ``SET LOCAL lock_timeout='0'`` — and
    ignores all three return values. Splice these into ``execute.side_effect``
    right after the workspace lookup so the count read still lands on the
    intended mock result.
    """
    return [_result(), _result(), _result()]


class TestConnectorProvisioningService:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.rollback = AsyncMock()
        return db

    @pytest.fixture(autouse=True)
    def _absent_worker_app_identity(self):
        """provision_connector always resolves the app identity now (an
        explicitly disabled default fails closed — #1337 review), which
        would consume one extra db.execute from the positional side_effect
        sequences below. Stub the identity lookup as absent (allowed
        migration-window state) so the plan-cap sequences stay aligned."""
        with patch("services.worker_app_identity.WorkerAppIdentityService") as svc:
            svc.return_value.get_identity = AsyncMock(return_value=None)
            yield

    @pytest.mark.asyncio
    async def test_free_plan_zero_connector_cap_blocks_before_writes(self, mock_db):
        workspace_id = uuid4()
        # #857/PR #860: a zero-cap plan short-circuits to 403 BEFORE the advisory
        # lock and count — so only the workspace lookup runs (no lock results).
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=0)),
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
        # Only the workspace lookup ran — the zero-cap path never reached the
        # advisory-lock SET LOCAL / pg_advisory_xact_lock or the count query.
        assert mock_db.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_basic_plan_at_cap_blocks_paid_boundary(self, mock_db):
        """AC2: cap=1 and active=1 must reject; >= cannot regress to >."""
        workspace_id = uuid4()
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=1)),
            *_lock_results(),
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
            *_lock_results(),
            _result(scalar=0),
            _result(one=None),
            _result(one=None),  # #910: canonical chat schema existence check → none → provision
            _result(),  # #910: ON CONFLICT DO NOTHING insert (return not consumed)
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
            *_lock_results(),
            _result(scalar=0),
            _result(one=None),
            _result(one=None),  # #910: canonical chat schema existence check → none → provision
            _result(),  # #910: ON CONFLICT DO NOTHING insert (return not consumed)
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
    async def test_seat_lock_timeout_fails_closed_503(self, mock_db):
        """#857: lock-acquire 55P03 → rollback + retriable 503 (fail-closed)."""
        workspace_id = uuid4()

        class _Orig(Exception):
            sqlstate = "55P03"

        lock_timeout = DBAPIError("SELECT pg_advisory_xact_lock(...)", {}, _Orig())
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=1)),
            _result(),  # SET LOCAL lock_timeout = '5s'
            lock_timeout,  # pg_advisory_xact_lock raises
        ]

        with patch("services.connector_provisioning.upsert_resource", new=AsyncMock()) as upsert:
            with pytest.raises(MemoryCloudException) as exc_info:
                await ConnectorProvisioningService(mock_db).provision_connector(
                    workspace_id=workspace_id,
                    user_id="user-1",
                    connector_type="slack",
                    resource_id="slack_general",
                )

        assert exc_info.value.status_code == 503
        assert exc_info.value.error_code == "CONNECTOR-002"
        mock_db.rollback.assert_awaited_once()
        upsert.assert_not_awaited()
        mock_db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_connector_for_resource_rejects_duplicate(self, mock_db):
        workspace_id = uuid4()
        resource_pk = uuid4()
        connector_id = uuid4()
        mock_db.execute.side_effect = [
            _result(one=SimpleNamespace(effective_max_connectors=5)),
            *_lock_results(),
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
            *_lock_results(),
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


@pytest.mark.asyncio
async def test_list_connectors_returns_workspace_scoped_rows_newest_first():
    from services.connector_provisioning import ConnectorListItem

    ws_id = uuid4()
    conn_a, conn_b = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    # The JOIN yields (WorkspaceConnector, resource_id-slug) tuples (#991); the
    # service maps each to a ConnectorListItem so the surface exposes the slug.
    rows = [(conn_a, "slug-a"), (conn_b, "slug-b")]

    db = MagicMock()
    exec_result = MagicMock()
    exec_result.all.return_value = rows
    db.execute = AsyncMock(return_value=exec_result)

    service = ConnectorProvisioningService(db)
    result = await service.list_connectors(ws_id)

    assert result == [
        ConnectorListItem(connector=conn_a, resource_id="slug-a"),
        ConnectorListItem(connector=conn_b, resource_id="slug-b"),
    ]
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_runtime_config_is_workspace_scoped_and_bumps_revision():
    connector_id = uuid4()
    workspace_id = uuid4()
    connector = SimpleNamespace(
        id=connector_id,
        workspace_id=workspace_id,
        runtime_config={"vision_enabled": True},
        config_version=7,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(one=connector))
    db.flush = AsyncMock()

    with patch("services.connector_provisioning.logger.info") as log_info:
        result = await ConnectorProvisioningService(db).update_runtime_config(
            workspace_id=workspace_id,
            connector_id=connector_id,
            runtime_config={"vision_enabled": False},
            user_id="admin-1",
        )

    assert connector.runtime_config["vision_enabled"] is False
    assert connector.runtime_config["buffer"] == {
        "ttl_seconds": 86400,
        "max_len": 10_000,
    }
    assert connector.config_version == 8
    assert result.config_version == 8
    db.flush.assert_awaited_once()
    statement = db.execute.await_args.args[0]
    assert "workspace_connectors.workspace_id" in str(statement)
    assert "FOR UPDATE" in str(statement)
    log_info.assert_called_once_with(
        "workspace_connector_runtime_updated",
        connector_id=str(connector_id),
        workspace_id=str(workspace_id),
        updated_by="admin-1",
        changed_fields=["vision_enabled"],
        cleared=False,
        config_version=8,
    )


@pytest.mark.asyncio
async def test_update_runtime_config_null_clears_back_to_worker_defaults():
    """#1350 review: runtime_config=None returns the row to NULL (the worker
    built-in defaults state) — a tuned connector is not a one-way door."""
    connector_id = uuid4()
    workspace_id = uuid4()
    connector = SimpleNamespace(
        id=connector_id,
        workspace_id=workspace_id,
        runtime_config={"vision_enabled": False},
        config_version=3,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(one=connector))
    db.flush = AsyncMock()

    with patch("services.connector_provisioning.logger.info") as log_info:
        result = await ConnectorProvisioningService(db).update_runtime_config(
            workspace_id=workspace_id,
            connector_id=connector_id,
            runtime_config=None,
            user_id="admin-1",
        )

    assert connector.runtime_config is None
    assert result.runtime_config is None
    assert connector.config_version == 4
    kwargs = log_info.call_args.kwargs
    assert kwargs["cleared"] is True
    # Diff shows the field returning to its default.
    assert kwargs["changed_fields"] == ["vision_enabled"]


@pytest.mark.asyncio
async def test_update_runtime_config_stale_expected_version_conflicts():
    """#1350 review: the full-document replacement gets an optimistic guard —
    a stale snapshot 409s instead of silently reverting concurrent changes."""
    from utils.exceptions import ConflictError

    connector = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        runtime_config=None,
        config_version=5,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(one=connector))
    db.flush = AsyncMock()

    with pytest.raises(ConflictError):
        await ConnectorProvisioningService(db).update_runtime_config(
            workspace_id=connector.workspace_id,
            connector_id=connector.id,
            runtime_config={"vision_enabled": False},
            user_id="admin-1",
            expected_config_version=4,
        )
    assert connector.config_version == 5  # untouched
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_runtime_config_survives_drifted_stored_document():
    """#1350 review: a stored doc the current schema rejects must not make
    the repair PATCH itself fail — previous falls back to defaults."""
    connector = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        runtime_config={"future_field": True, "buffer": {"max_len": 10**15}},
        config_version=1,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(one=connector))
    db.flush = AsyncMock()

    result = await ConnectorProvisioningService(db).update_runtime_config(
        workspace_id=connector.workspace_id,
        connector_id=connector.id,
        runtime_config={"vision_enabled": False},
        user_id="admin-1",
    )
    assert result.config_version == 2
    assert connector.runtime_config["vision_enabled"] is False


class TestTeamUniquenessGuard:
    """#1360: the #1315 app-qualified uniqueness must not re-open
    cross-tenant pre-binding of an already-bound platform team."""

    @pytest.mark.asyncio
    async def test_cross_tenant_prebind_rejected_even_under_different_app_key(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value=_result(one=uuid4()))  # other-tenant hit
        svc = ConnectorProvisioningService(db)
        svc.get_connector_for_dispatch = AsyncMock(return_value=None)  # app-qualified miss

        with pytest.raises(ConflictError):
            await svc._assert_team_unclaimed(
                workspace_id=uuid4(),
                connector_type="slack",
                app_key="second-app",
                external_team_id="T123",
            )

    @pytest.mark.asyncio
    async def test_same_workspace_multi_app_allowed(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value=_result(one=None))  # no OTHER-workspace row
        svc = ConnectorProvisioningService(db)
        svc.get_connector_for_dispatch = AsyncMock(return_value=None)

        await svc._assert_team_unclaimed(
            workspace_id=uuid4(),
            connector_type="slack",
            app_key="second-app",
            external_team_id="T123",
        )

        # The cross-tenant probe excludes the caller's own workspace.
        sql = str(db.execute.await_args.args[0])
        assert "workspace_id !=" in sql

    @pytest.mark.asyncio
    async def test_exact_app_qualified_duplicate_rejected_first(self):
        db = MagicMock()
        db.execute = AsyncMock()
        svc = ConnectorProvisioningService(db)
        svc.get_connector_for_dispatch = AsyncMock(return_value=SimpleNamespace(id=uuid4()))

        with pytest.raises(ConflictError):
            await svc._assert_team_unclaimed(
                workspace_id=uuid4(),
                connector_type="slack",
                app_key="default",
                external_team_id="T123",
            )
        db.execute.assert_not_awaited()
