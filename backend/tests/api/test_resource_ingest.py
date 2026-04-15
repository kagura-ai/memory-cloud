"""Tests for Resource Ingest API.

Issue #238: Resource-driven incremental indexing.
Issue #322: Workspace boundary enforcement on ingest (security hotfix).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from api.routes.resource_ingest import (
    _enforce_workspace_membership,
    _resolve_authoritative_context,
    ingest_event,
)
from auth.resource_tokens import ResourceTokenManager
from db.constraint_names import (
    RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE,
    RESOURCE_EVENTS_UPSERT_UNIQUE,
    integrity_error_constraint_name,
)
from models.auth import Context
from models.resource import ResourceEvent, ResourceToken
from models.schemas import ResourceEventRequest
from utils.exceptions import ConflictError


def _make_integrity_error(constraint_name: str | None) -> IntegrityError:
    """Build an IntegrityError with a psycopg-shaped ``orig.diag``.

    Mirrors the structured diagnostic surface
    ``integrity_error_constraint_name`` reads, so unit tests can pin the
    dispatch without spinning up a real PostgreSQL connection.
    """
    diag = SimpleNamespace(constraint_name=constraint_name)
    orig = SimpleNamespace(diag=diag)
    err = IntegrityError(statement="INSERT ...", params=None, orig=Exception("test"))
    err.orig = orig  # type: ignore[assignment]
    return err


class TestResourceTokenManager:
    """Test ResourceTokenManager authentication and token management."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock()
        db.execute = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.get = AsyncMock()
        return db

    @pytest.fixture
    def manager(self, mock_db):
        """Create ResourceTokenManager instance."""
        return ResourceTokenManager(mock_db)

    @pytest.mark.asyncio
    async def test_create_token_success(self, manager, mock_db):
        """Test successful token creation."""
        # Execute
        token, token_obj = await manager.create_token(
            resource_id="ec_products",
            description="Test EC integration",
            quota_events_per_hour=500,
            created_by="admin_user",
        )

        # Assert
        assert token.startswith("kagura_resource_")
        assert len(token) > 50  # Has random suffix
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verify_token_valid(self, manager, mock_db):
        """Test token verification with valid token."""
        # Mock token record
        token_record = MagicMock(spec=ResourceToken)
        token_record.id = 1
        token_record.resource_id = "ec_products"
        token_record.is_active = True
        token_record.quota_events_per_hour = 1000

        # Mock query result
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=token_record)
        mock_db.execute.return_value = result

        # Execute
        verified = await manager.verify_token("kagura_resource_test123", "ec_products")

        # Assert
        assert verified == token_record
        mock_db.flush.assert_awaited_once()  # last_used_at updated

    @pytest.mark.asyncio
    async def test_verify_token_wrong_resource(self, manager, mock_db):
        """Test token verification with mismatched resource_id."""
        # Mock: no token found for this (token, resource_id) pair
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute.return_value = result

        # Execute
        verified = await manager.verify_token("kagura_resource_test123", "wrong_resource")

        # Assert
        assert verified is None
        mock_db.flush.assert_not_awaited()


# Issue #332: ``_check_event_quota`` moved to
# ``services/resource_quota_service.check_event_quota`` (workspace-scoped key
# shared with the MCP ingest path). See ``tests/services/test_resource_quota_service.py``.


class TestIntegrityErrorConstraintNameHelper:
    """Issue #318: structured constraint_name extraction."""

    def test_returns_constraint_name_when_present(self):
        err = _make_integrity_error(RESOURCE_EVENTS_UPSERT_UNIQUE)
        assert integrity_error_constraint_name(err) == RESOURCE_EVENTS_UPSERT_UNIQUE

    def test_returns_none_when_diag_constraint_name_is_none(self):
        err = _make_integrity_error(None)
        assert integrity_error_constraint_name(err) is None

    def test_returns_none_when_orig_has_no_diag(self):
        err = IntegrityError(statement="INSERT ...", params=None, orig=Exception("plain"))
        # plain exception has no .diag attribute
        assert integrity_error_constraint_name(err) is None

    def test_returns_none_when_orig_is_none(self):
        err = IntegrityError(statement="INSERT ...", params=None, orig=Exception("x"))
        err.orig = None  # type: ignore[assignment]
        assert integrity_error_constraint_name(err) is None


class TestResourceEventIdempotency:
    """Issue #318: ingest_event IntegrityError dispatch by constraint_name."""

    @pytest.fixture
    def mock_event_request(self):
        return ResourceEventRequest(
            op="upsert",
            doc_id="PROD-12345",
            version=3,
            payload={"product_name": "Test Product", "price": 1000},
            idempotency_key="test-key-123",
        )

    @pytest.fixture
    def mock_auth(self):
        token = MagicMock(spec=ResourceToken)
        token.id = 7
        token.created_by = "user-1"
        ctx = MagicMock(spec=Context)
        ctx.id = uuid4()
        ctx.workspace_id = uuid4()
        return (token, 1000, ctx)

    def _build_db(self, integrity_error: IntegrityError, existing_event: object | None = None):
        """Mock AsyncSession that raises ``integrity_error`` on commit and
        optionally returns ``existing_event`` on the lookup query that the
        idempotency-key path issues.
        """
        db = MagicMock()
        db.add = MagicMock()
        db.commit = AsyncMock(side_effect=integrity_error)
        db.rollback = AsyncMock()
        db.refresh = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=existing_event)
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_upsert_unique_violation_raises_conflict(self, mock_event_request, mock_auth):
        db = self._build_db(_make_integrity_error(RESOURCE_EVENTS_UPSERT_UNIQUE))

        with patch("api.routes.resource_ingest.check_event_quota", new=AsyncMock()):
            with pytest.raises(ConflictError):
                await ingest_event(
                    resource_id="ec_products",
                    request=mock_event_request,
                    auth=mock_auth,
                    db=db,
                )

        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotency_key_collision_returns_existing(self, mock_event_request, mock_auth):
        existing = MagicMock(spec=ResourceEvent)
        existing.id = 999
        db = self._build_db(
            _make_integrity_error(RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE),
            existing_event=existing,
        )

        with patch("api.routes.resource_ingest.check_event_quota", new=AsyncMock()):
            response = await ingest_event(
                resource_id="ec_products",
                request=mock_event_request,
                auth=mock_auth,
                db=db,
            )

        assert response.event_id == 999
        assert response.queued is False
        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_constraint_reraises_integrity_error(self, mock_event_request, mock_auth):
        db = self._build_db(_make_integrity_error("some_other_constraint"))

        with patch("api.routes.resource_ingest.check_event_quota", new=AsyncMock()):
            with pytest.raises(IntegrityError):
                await ingest_event(
                    resource_id="ec_products",
                    request=mock_event_request,
                    auth=mock_auth,
                    db=db,
                )

        db.rollback.assert_awaited_once()


def _mock_db_scalars(values):
    """Build a mock async DB session whose single execute() returns `values`
    from `.scalars().all()`. Accepts an empty list, one item, or many.
    """
    scalars_obj = MagicMock()
    scalars_obj.all = MagicMock(return_value=list(values))
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars_obj)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestResourceIngestWorkspaceBoundary:
    """Workspace boundary enforcement in verify_resource_token (security hotfix)."""

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def mock_context(self, workspace_id):
        ctx = MagicMock(spec=Context)
        ctx.id = uuid4()
        ctx.resource_id = "acme"
        ctx.workspace_id = workspace_id
        ctx.deleted_at = None
        return ctx

    @pytest.fixture
    def mock_token(self):
        token = MagicMock(spec=ResourceToken)
        token.id = 42
        token.resource_id = "acme"
        token.created_by = "user_owner"
        token.is_active = True
        return token

    @pytest.fixture
    def mock_request(self):
        request = MagicMock()
        request.client.host = "203.0.113.7"
        return request

    # --- _resolve_authoritative_context ----------------------------------

    @pytest.mark.asyncio
    async def test_resolve_context_returns_single_active_match(self, mock_context):
        db = _mock_db_scalars([mock_context])
        got = await _resolve_authoritative_context(db, "acme")
        assert got is mock_context

    @pytest.mark.asyncio
    async def test_resolve_context_returns_none_when_unbound(self):
        """Helper returns None for unbound resource_id; caller (verify_resource_token)
        owns the 404 + warning policy."""
        db = _mock_db_scalars([])
        got = await _resolve_authoritative_context(db, "orphan_id")
        assert got is None

    @pytest.mark.asyncio
    async def test_resolve_context_raises_409_on_cross_workspace_collision(self, mock_context):
        """Defensive: pre-migration cross-workspace collisions must fail closed
        (409) rather than bubble MultipleResultsFound as a 500."""
        other_ctx = MagicMock(spec=Context)
        other_ctx.id = uuid4()
        other_ctx.resource_id = "acme"
        other_ctx.workspace_id = uuid4()
        other_ctx.deleted_at = None
        db = _mock_db_scalars([mock_context, other_ctx])

        with patch("api.routes.resource_ingest.logger") as mock_logger:
            with pytest.raises(HTTPException) as exc:
                await _resolve_authoritative_context(db, "acme")

            assert exc.value.status_code == 409
            mock_logger.warning.assert_called_once_with(
                "resource_id_ambiguous_on_ingest",
                resource_id="acme",
                match_count=2,
            )

    # --- _enforce_workspace_membership -----------------------------------

    @pytest.mark.asyncio
    async def test_membership_allowed_for_workspace_member(
        self, mock_request, mock_token, mock_context
    ):
        db = MagicMock()
        with patch(
            "services.permission_service.PermissionService.check_workspace_access",
            new=AsyncMock(return_value=MagicMock()),
        ):
            # Should not raise.
            await _enforce_workspace_membership(db, mock_request, mock_token, mock_context)

    @pytest.mark.asyncio
    async def test_membership_denied_logs_cross_tenant_attempt(
        self, mock_request, mock_token, mock_context
    ):
        """Attacker token (non-member) must be rejected with audit warning.
        The log's `reason` kwarg carries the underlying auth failure detail
        for forensics."""
        db = MagicMock()
        auth_error = HTTPException(
            status_code=403,
            detail="Not a member of workspace " + str(mock_context.workspace_id),
        )
        with (
            patch(
                "services.permission_service.PermissionService.check_workspace_access",
                new=AsyncMock(side_effect=auth_error),
            ),
            patch("api.routes.resource_ingest.logger") as mock_logger,
        ):
            with pytest.raises(HTTPException) as exc:
                await _enforce_workspace_membership(db, mock_request, mock_token, mock_context)

            assert exc.value.status_code == 403
            mock_logger.warning.assert_called_once_with(
                "cross_tenant_ingest_attempt",
                resource_id=mock_context.resource_id,
                token_id=mock_token.id,
                target_workspace_id=str(mock_context.workspace_id),
                token_creator=mock_token.created_by,
                client_ip="203.0.113.7",
                reason=auth_error.detail,
            )

    @pytest.mark.asyncio
    async def test_membership_denied_when_workspace_soft_deleted(
        self, mock_request, mock_token, mock_context
    ):
        """Soft-deleted workspace must deny ingest — the prior is_workspace_member
        check silently allowed this. `check_workspace_access` now catches it."""
        db = MagicMock()
        soft_deleted_error = HTTPException(
            status_code=403,
            detail=f"Workspace {mock_context.workspace_id} not found or has been deleted",
        )
        with (
            patch(
                "services.permission_service.PermissionService.check_workspace_access",
                new=AsyncMock(side_effect=soft_deleted_error),
            ),
            patch("api.routes.resource_ingest.logger") as mock_logger,
        ):
            with pytest.raises(HTTPException) as exc:
                await _enforce_workspace_membership(db, mock_request, mock_token, mock_context)

            assert exc.value.status_code == 403
            call_kwargs = mock_logger.warning.call_args.kwargs
            assert "deleted" in call_kwargs["reason"]

    @pytest.mark.asyncio
    async def test_membership_denied_without_request_client(self, mock_token, mock_context):
        """client_ip is None when request.client is None (FastAPI edge case)."""
        req = MagicMock()
        req.client = None
        db = MagicMock()
        auth_error = HTTPException(status_code=403, detail="Not a member")
        with (
            patch(
                "services.permission_service.PermissionService.check_workspace_access",
                new=AsyncMock(side_effect=auth_error),
            ),
            patch("api.routes.resource_ingest.logger") as mock_logger,
        ):
            with pytest.raises(HTTPException):
                await _enforce_workspace_membership(db, req, mock_token, mock_context)

            call_kwargs = mock_logger.warning.call_args.kwargs
            assert call_kwargs["client_ip"] is None

    @pytest.mark.asyncio
    async def test_membership_denied_when_token_has_no_creator(self, mock_request, mock_context):
        """Legacy tokens without created_by cannot be authorized, without hitting the DB."""
        token = MagicMock(spec=ResourceToken)
        token.id = 99
        token.created_by = None
        db = MagicMock()
        db.execute = AsyncMock()

        with patch("api.routes.resource_ingest.logger") as mock_logger:
            with pytest.raises(HTTPException) as exc:
                await _enforce_workspace_membership(db, mock_request, token, mock_context)

            assert exc.value.status_code == 403
            mock_logger.warning.assert_called_once_with(
                "resource_ingest_missing_token_creator",
                resource_id=mock_context.resource_id,
                token_id=99,
            )
        # No permission check (and therefore no DB query) for unattributed tokens.
        db.execute.assert_not_awaited()
