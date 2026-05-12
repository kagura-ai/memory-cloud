"""Tests for GET /contexts/{context_id}/tags.

Locks down the REST surface for ``list_context_tags``: response envelope shape,
empty-context behavior (200 + ``tags=[]``), and ``NotFoundException`` → 404
contract. The aggregation correctness itself is exercised at the integration
level — these tests pin the route wiring around a mocked service.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.contexts import list_context_tags
from utils.exceptions import NotFoundException, ValidationError

MOCK_USER = {"user_id": "test_user"}


class TestListContextTagsRoute:
    """REST route surface for list_context_tags."""

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.fixture
    def mock_service(self):
        return MagicMock(aggregate_tags=AsyncMock())

    @pytest.mark.asyncio
    async def test_returns_envelope_with_tags(self, mock_service, context_id):
        """Populated context returns envelope with RelatedTagItem entries."""
        last_used = datetime(2026, 5, 12, 3, 39, 39)
        mock_service.aggregate_tags.return_value = {
            "context_name": "kagura-dev",
            "rows": [
                {"tag": "python", "count": 5, "last_used_at": last_used},
                {"tag": "fastapi", "count": 3, "last_used_at": None},
            ],
        }

        response = await list_context_tags(
            context_id=context_id,
            user=MOCK_USER,
            service=mock_service,
            limit=50,
            min_count=1,
            sort="count",
            prefix="",
        )

        assert response.context_id == context_id
        assert response.total == 2
        assert [t.tag for t in response.tags] == ["python", "fastapi"]
        assert response.tags[0].count == 5
        assert response.tags[0].last_used_at == last_used
        assert response.tags[1].last_used_at is None

    @pytest.mark.asyncio
    async def test_empty_context_returns_200_with_empty_list(self, mock_service, context_id):
        """Empty / untagged context returns 200 + tags=[], NOT 404 (DX contract)."""
        mock_service.aggregate_tags.return_value = {
            "context_name": "empty-ctx",
            "rows": [],
        }

        response = await list_context_tags(
            context_id=context_id,
            user=MOCK_USER,
            service=mock_service,
            limit=50,
            min_count=1,
            sort="count",
            prefix="",
        )

        assert response.total == 0
        assert response.tags == []
        assert response.context_id == context_id

    @pytest.mark.asyncio
    async def test_not_found_maps_to_404(self, mock_service, context_id):
        """NotFoundException → HTTP 404 (uniform disclosure for not-found/no-access)."""
        mock_service.aggregate_tags.side_effect = NotFoundException(
            f"Context {context_id} not found"
        )

        with pytest.raises(HTTPException) as exc:
            await list_context_tags(
                context_id=context_id,
                user=MOCK_USER,
                service=mock_service,
                limit=50,
                min_count=1,
                sort="count",
                prefix="",
            )

        assert exc.value.status_code == 404
        assert "not found" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    async def test_validation_error_maps_to_422(self, mock_service, context_id):
        """ValidationError from service (bad sort etc.) → HTTP 422."""
        mock_service.aggregate_tags.side_effect = ValidationError("Invalid sort mode 'bogus'.")

        with pytest.raises(HTTPException) as exc:
            await list_context_tags(
                context_id=context_id,
                user=MOCK_USER,
                service=mock_service,
                limit=50,
                min_count=1,
                sort="count",  # FastAPI pattern would already reject 'bogus' upstream
                prefix="",
            )

        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_passes_through_query_params(self, mock_service, context_id):
        """Route forwards query params verbatim to the service layer."""
        mock_service.aggregate_tags.return_value = {
            "context_name": "ctx",
            "rows": [],
        }

        await list_context_tags(
            context_id=context_id,
            user=MOCK_USER,
            service=mock_service,
            limit=100,
            min_count=3,
            sort="recent",
            prefix="auth",
        )

        mock_service.aggregate_tags.assert_awaited_once_with(
            "test_user",
            context_id,
            limit=100,
            min_count=3,
            sort="recent",
            prefix="auth",
        )
