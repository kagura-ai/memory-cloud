"""Tests for Resource Ingest API.

Issue #238: Resource-driven incremental indexing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes.resource_ingest import _check_event_quota
from auth.resource_tokens import ResourceTokenManager
from models.resource import ResourceToken
from models.schemas import ResourceEventRequest
from utils.exceptions import RateLimitError


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


class TestResourceEventQuotaCheck:
    """Test event quota checking."""

    @pytest.mark.asyncio
    async def test_quota_within_limit(self):
        """Test quota check when within limit."""
        with patch("db.redis.get_cache", new_callable=AsyncMock) as mock_cache:
            mock_cache.return_value = "50"  # 50/1000 events used

            # Should not raise
            await _check_event_quota("ec_products", token_id=1, quota_per_hour=1000)

            mock_cache.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_quota_exceeded(self):
        """Test quota check when exceeded."""
        with patch("db.redis.get_cache", new_callable=AsyncMock) as mock_cache:
            mock_cache.return_value = "1001"  # 1001/1000 events - exceeded!

            # Should raise RateLimitError
            with pytest.raises(RateLimitError) as exc_info:
                await _check_event_quota("ec_products", token_id=1, quota_per_hour=1000)

            assert "Event quota exceeded" in str(exc_info.value.message)
            assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_quota_batch_check(self):
        """Test quota check for batch (multiple events at once)."""
        with patch("db.redis.get_cache", new_callable=AsyncMock) as mock_cache:
            mock_cache.return_value = "990"  # Current: 990, trying to add 20 more

            # Should raise RateLimitError (990 + 20 > 1000)
            with pytest.raises(RateLimitError):
                await _check_event_quota("ec_products", token_id=1, quota_per_hour=1000, count=20)


class TestResourceEventIdempotency:
    """Test idempotency handling."""

    @pytest.fixture
    def mock_event_request(self):
        """Create mock ResourceEventRequest."""
        return ResourceEventRequest(
            op="upsert",
            doc_id="PROD-12345",
            version=3,
            payload={"product_name": "Test Product", "price": 1000},
            idempotency_key="test-key-123",
        )

    @pytest.mark.asyncio
    async def test_duplicate_version_returns_conflict(self, mock_event_request):
        """Test that duplicate version returns ConflictError."""
        # This would be an integration test - testing full endpoint
        # Mocking IntegrityError with unique_resource_doc_version constraint
        pass  # TODO: Implement after integration test setup

    @pytest.mark.asyncio
    async def test_duplicate_idempotency_key_returns_existing(self, mock_event_request):
        """Test that duplicate idempotency_key returns existing event (idempotent)."""
        # This would be an integration test
        pass  # TODO: Implement after integration test setup
