"""Tests for MCP ``handle_list_tags`` tool.

Pins:
- Happy path response shape (status / context_id / context_name / tags / total).
- Empty-context returns ``tags=[]`` and ``total=0`` (no ``context_not_found``).
- ``NotFoundException`` from the service → uniform ``context_not_found`` surface.
- Arg validation: bad ``limit`` / ``min_count`` / oversized ``prefix`` / invalid UUID
  return ``invalid_argument`` / ``invalid_context_id_format`` without touching the DB.
- ``last_used_at`` datetime is rendered with a ``Z`` suffix.

Aggregation correctness (SQL CTE) is exercised at the integration level
(``tests/integration/test_list_tags_aggregation.py``).
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.context import handle_list_tags
from utils.exceptions import NotFoundException


@pytest.fixture
def user_id():
    return "test_user"


@pytest.fixture
def workspace_id():
    return uuid4()


@pytest.fixture
def context_id():
    return uuid4()


def _mock_db_context():
    """Return a (mock_db, async_get_db) pair that yields the mock once."""
    mock_db = AsyncMock()
    mock_db.rollback = AsyncMock()
    mock_db.commit = AsyncMock()

    async def mock_get_db():
        yield mock_db

    return mock_db, mock_get_db


class TestHandleListTagsHappyPath:
    @pytest.mark.asyncio
    async def test_returns_success_envelope(self, user_id, workspace_id, context_id):
        """Populated context returns ``status=success`` with tag array."""
        _, mock_get_db = _mock_db_context()
        last_used = datetime(2026, 5, 12, 3, 39, 39)
        mock_service = MagicMock(
            aggregate_tags=AsyncMock(
                return_value={
                    "context_name": "kagura-dev",
                    "rows": [
                        {"tag": "python", "count": 5, "last_used_at": last_used},
                        {"tag": "fastapi", "count": 3, "last_used_at": None},
                    ],
                }
            )
        )

        with (
            patch("db.base.get_db", mock_get_db),
            patch("services.context_service.ContextService", return_value=mock_service),
            patch("mcp_server.tools.context._log_tool_usage", AsyncMock()),
        ):
            result = await handle_list_tags({"context_id": str(context_id)}, user_id, workspace_id)

        payload = json.loads(result[0].text)
        assert payload["status"] == "success"
        assert payload["context_id"] == str(context_id)
        assert payload["context_name"] == "kagura-dev"
        assert payload["total"] == 2
        assert payload["tags"][0] == {
            "tag": "python",
            "count": 5,
            "last_used_at": "2026-05-12T03:39:39Z",
        }
        assert payload["tags"][1]["last_used_at"] is None

    @pytest.mark.asyncio
    async def test_empty_context_returns_empty_array(self, user_id, workspace_id, context_id):
        """Empty context returns ``tags=[]`` and ``total=0`` (no context_not_found)."""
        _, mock_get_db = _mock_db_context()
        mock_service = MagicMock(
            aggregate_tags=AsyncMock(return_value={"context_name": "empty-ctx", "rows": []})
        )

        with (
            patch("db.base.get_db", mock_get_db),
            patch("services.context_service.ContextService", return_value=mock_service),
            patch("mcp_server.tools.context._log_tool_usage", AsyncMock()),
        ):
            result = await handle_list_tags({"context_id": str(context_id)}, user_id, workspace_id)

        payload = json.loads(result[0].text)
        assert payload["status"] == "success"
        assert payload["tags"] == []
        assert payload["total"] == 0

    @pytest.mark.asyncio
    async def test_forwards_optional_args_to_service(self, user_id, workspace_id, context_id):
        """All optional args (limit/min_count/sort/prefix) reach the service."""
        _, mock_get_db = _mock_db_context()
        mock_service = MagicMock(
            aggregate_tags=AsyncMock(return_value={"context_name": "ctx", "rows": []})
        )

        with (
            patch("db.base.get_db", mock_get_db),
            patch("services.context_service.ContextService", return_value=mock_service),
            patch("mcp_server.tools.context._log_tool_usage", AsyncMock()),
        ):
            await handle_list_tags(
                {
                    "context_id": str(context_id),
                    "limit": 100,
                    "min_count": 3,
                    "sort": "recent",
                    "prefix": "auth",
                },
                user_id,
                workspace_id,
            )

        mock_service.aggregate_tags.assert_awaited_once_with(
            user_id,
            context_id,
            limit=100,
            min_count=3,
            sort="recent",
            prefix="auth",
        )


class TestHandleListTagsErrorSurface:
    @pytest.mark.asyncio
    async def test_not_found_returns_uniform_surface(self, user_id, workspace_id, context_id):
        """NotFoundException from the service → context_not_found error response."""
        _, mock_get_db = _mock_db_context()
        mock_service = MagicMock(
            aggregate_tags=AsyncMock(
                side_effect=NotFoundException(f"Context not found: {context_id}")
            )
        )

        with (
            patch("db.base.get_db", mock_get_db),
            patch("services.context_service.ContextService", return_value=mock_service),
            patch("mcp_server.tools.context._log_tool_usage", AsyncMock()),
        ):
            result = await handle_list_tags({"context_id": str(context_id)}, user_id, workspace_id)

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_not_found"
        assert payload["context_id"] == str(context_id)

    @pytest.mark.asyncio
    async def test_invalid_sort_from_service_returns_invalid_argument(
        self, user_id, workspace_id, context_id
    ):
        """Service-raised ValidationError (e.g. bad sort) → invalid_argument."""
        from utils.exceptions import ValidationError

        _, mock_get_db = _mock_db_context()
        mock_service = MagicMock(
            aggregate_tags=AsyncMock(side_effect=ValidationError("Invalid sort mode 'bogus'."))
        )

        with (
            patch("db.base.get_db", mock_get_db),
            patch("services.context_service.ContextService", return_value=mock_service),
            patch("mcp_server.tools.context._log_tool_usage", AsyncMock()),
        ):
            result = await handle_list_tags(
                {"context_id": str(context_id), "sort": "bogus"},
                user_id,
                workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_missing_context_id_returns_required(self, user_id, workspace_id):
        """Missing context_id short-circuits before touching the DB."""
        # No DB patch — if the handler touches get_db it raises.
        result = await handle_list_tags({}, user_id, workspace_id)

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_id_required"

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_format_error_without_db(self, user_id, workspace_id):
        """Bad context_id UUID short-circuits before touching the DB."""
        # No DB patch — if the handler touches get_db it raises.
        result = await handle_list_tags({"context_id": "not-a-uuid"}, user_id, workspace_id)

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "invalid_context_id_format"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_args",
        [
            {"limit": 0},
            {"limit": 501},
            {"limit": "fifty"},
            {"min_count": 0},
            {"min_count": -1},
            {"min_count": 10_001},
            {"prefix": "x" * 201},
        ],
    )
    async def test_bad_args_short_circuit_without_db(
        self, user_id, workspace_id, context_id, bad_args
    ):
        """Each handler-bounded invalid arg returns invalid_argument pre-DB.

        ``sort`` is validated by the service (single source of truth), so a
        bad ``sort`` is covered by ``test_invalid_sort_from_service_*``.
        """
        args = {"context_id": str(context_id), **bad_args}
        result = await handle_list_tags(args, user_id, workspace_id)

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "invalid_argument"
