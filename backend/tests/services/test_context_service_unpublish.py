"""Unit tests for context unpublish resource_id clearing (#156)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.context_service import ContextService


@pytest.fixture
def service():
    """Create ContextService with mocked async DB."""
    mock_db = AsyncMock()
    return ContextService(mock_db)


@pytest.fixture
def mock_context():
    """Create a mock context that is public with a resource_id."""
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.is_public = True
    ctx.is_private = False
    ctx.resource_id = "test_resource"
    ctx.is_locked = False
    ctx.display_name = "Test Context"
    ctx.description = "Test"
    ctx.summary = ""
    ctx.usage_guide = ""
    return ctx


class TestUnpublishClearsResourceId:
    """Issue #156: resource_id should be cleared when unpublishing."""

    @pytest.mark.asyncio
    async def test_unpublish_clears_resource_id(self, service, mock_context):
        """Setting is_public=False should clear resource_id."""
        with patch.object(
            service, "get_context", new_callable=AsyncMock, return_value=mock_context
        ):
            await service.update_context(
                user_id="test_user",
                context_id=mock_context.id,
                is_public=False,
            )

        assert mock_context.resource_id is None
        assert mock_context.is_public is False

    @pytest.mark.asyncio
    async def test_unpublish_no_op_when_already_unpublished(self, service, mock_context):
        """Setting is_public=False on already-unpublished context should not error."""
        mock_context.is_public = False
        mock_context.resource_id = None

        with patch.object(
            service, "get_context", new_callable=AsyncMock, return_value=mock_context
        ):
            await service.update_context(
                user_id="test_user",
                context_id=mock_context.id,
                is_public=False,
            )

        assert mock_context.resource_id is None

    @pytest.mark.asyncio
    async def test_publish_preserves_resource_id(self, service, mock_context):
        """Setting is_public=True should NOT clear resource_id."""
        mock_context.is_public = False
        mock_context.resource_id = "existing_resource"

        with patch.object(
            service, "get_context", new_callable=AsyncMock, return_value=mock_context
        ):
            await service.update_context(
                user_id="test_user",
                context_id=mock_context.id,
                is_public=True,
            )

        assert mock_context.resource_id == "existing_resource"
        assert mock_context.is_public is True

    @pytest.mark.asyncio
    async def test_unpublish_without_resource_id(self, service, mock_context):
        """Unpublishing a context that has no resource_id should not error."""
        mock_context.resource_id = None

        with patch.object(
            service, "get_context", new_callable=AsyncMock, return_value=mock_context
        ):
            await service.update_context(
                user_id="test_user",
                context_id=mock_context.id,
                is_public=False,
            )

        assert mock_context.resource_id is None
        assert mock_context.is_public is False
