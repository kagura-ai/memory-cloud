"""Tests for per-context sleep_mode toggle (Issue #504).

Covers:
1. Enum validation on ContextUpdate Pydantic schema
2. ContextService.update_context applies sleep_mode correctly
3. ContextResponse serialization includes sleep_mode
4. owner_only_fields list contains sleep_mode (permission enforcement)
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.contexts import ContextResponse, ContextUpdate
from services.context_service import ContextService

# ============================================================================
# 1. Enum Validation
# ============================================================================


class TestContextUpdateSleepModeValidation:
    """Pydantic schema rejects unknown sleep_mode values."""

    @pytest.mark.parametrize("mode", ["full", "edges_only", "skip"])
    def test_valid_sleep_modes_accepted(self, mode: str):
        update = ContextUpdate(sleep_mode=mode)  # type: ignore[literal-required]
        assert update.sleep_mode == mode

    def test_null_sleep_mode_accepted(self):
        update = ContextUpdate()
        assert update.sleep_mode is None

    def test_invalid_sleep_mode_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ContextUpdate(sleep_mode="invalid")  # type: ignore[literal-required]


# ============================================================================
# 2. Service Layer
# ============================================================================


class TestContextServiceUpdateSleepMode:
    """ContextService.update_context applies sleep_mode when provided."""

    @pytest.fixture
    def service(self):
        mock_db = AsyncMock()
        return ContextService(mock_db)

    @pytest.fixture
    def mock_context(self):
        ctx = MagicMock()
        ctx.id = uuid4()
        ctx.name = "test-ctx"
        ctx.display_name = None
        ctx.description = None
        ctx.summary = None
        ctx.usage_guide = None
        ctx.is_private = True
        ctx.is_public = False
        ctx.resource_id = None
        ctx.is_locked = False
        ctx.sleep_mode = "full"
        return ctx

    @pytest.mark.asyncio
    async def test_update_sleep_mode_to_skip(self, service, mock_context):
        with patch.object(
            service, "get_context", new_callable=AsyncMock, return_value=mock_context
        ):
            await service.update_context(
                user_id="test_user",
                context_id=mock_context.id,
                sleep_mode="skip",
            )

        assert mock_context.sleep_mode == "skip"
        service.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_sleep_mode_to_edges_only(self, service, mock_context):
        with patch.object(
            service, "get_context", new_callable=AsyncMock, return_value=mock_context
        ):
            await service.update_context(
                user_id="test_user",
                context_id=mock_context.id,
                sleep_mode="edges_only",
            )

        assert mock_context.sleep_mode == "edges_only"
        service.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sleep_mode_none_is_noop(self, service, mock_context):
        """Passing sleep_mode=None should not change the existing value."""
        with patch.object(
            service, "get_context", new_callable=AsyncMock, return_value=mock_context
        ):
            await service.update_context(
                user_id="test_user",
                context_id=mock_context.id,
                sleep_mode=None,
            )

        assert mock_context.sleep_mode == "full"
        service.db.commit.assert_awaited_once()


# ============================================================================
# 3. Response Serialization
# ============================================================================


class TestContextResponseSerialization:
    """ContextResponse includes sleep_mode in serialized output."""

    def test_sleep_mode_defaults_to_full(self):
        response = ContextResponse(
            id=uuid4(),
            name="test",
            is_default=False,
            created_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        )
        body = response.model_dump()
        assert body["sleep_mode"] == "full"

    def test_sleep_mode_edges_only(self):
        response = ContextResponse(
            id=uuid4(),
            name="test",
            is_default=False,
            sleep_mode="edges_only",
            created_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        )
        body = response.model_dump()
        assert body["sleep_mode"] == "edges_only"

    def test_sleep_mode_skip(self):
        response = ContextResponse(
            id=uuid4(),
            name="test",
            is_default=False,
            sleep_mode="skip",
            created_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        )
        body = response.model_dump()
        assert body["sleep_mode"] == "skip"


# ============================================================================
# 4. Permission Enforcement (owner_only_fields)
# ============================================================================


class TestContextSleepModePermission:
    """sleep_mode must be present in owner_only_fields list."""

    def test_sleep_mode_in_owner_only_fields(self):
        """Verify the route's owner_only_fields list contains sleep_mode.

        This is a contract test: if someone removes sleep_mode from the list,
        this test fails, reminding them that sleep_mode is owner-only by design.
        """
        # Extract owner_only_fields from the route source by inspecting the
        # function's local variables (they're defined at module load time).
        import inspect

        from api.routes.contexts import update_context

        source = inspect.getsource(update_context)
        assert "owner_only_fields" in source
        assert '"sleep_mode"' in source or "'sleep_mode'" in source
