"""Tests for MCP edge CRUD tool handlers.

Issue #458: handle_update_edge response payload must reflect post-update DB
state (weight, last_updated, edge_type) rather than the cached pre-update
snapshot loaded by the prior get_edge call. The fix lives at the repository
layer (NeuralEdgeRepository.create_or_update_edge refreshes the returned
ORM after RETURNING), so this handler-level test pins only the contract
the MCP tool exposes: whatever ORM the repo returns is what the response
serializes.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.edge import handle_update_edge


def _mock_edge(src_id, dst_id, *, edge_type="neural_association", weight=0.5):
    """Build a minimal NeuralMemoryEdge-shaped mock for response serialization."""
    e = MagicMock()
    e.src_id = src_id
    e.dst_id = dst_id
    e.edge_type = edge_type
    e.weight = weight
    e.confidence = 1.0
    e.created_at = datetime(2026, 4, 26, 9, 0, 0, tzinfo=UTC)
    e.last_updated = datetime(2026, 4, 26, 9, 0, 0, tzinfo=UTC)
    return e


class TestUpdateEdgeResponsePayload:
    """Issue #458 regression: response must serialize whatever the repo returns."""

    @pytest.fixture
    def user_id(self):
        return "test_user_458"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_response_reflects_repo_returned_edge(self, user_id, workspace_id, context_id):
        """The response payload mirrors the post-update edge returned by the repo.

        The fix lives in NeuralEdgeRepository.create_or_update_edge (refreshes
        the ORM after RETURNING). Here we pin the handler contract: response
        weight/last_updated equal whatever the repo returned, with no
        intervening transformation that could re-stale the value.
        """
        src_id = uuid4()
        dst_id = uuid4()
        existing = _mock_edge(src_id, dst_id, edge_type="neural_association", weight=0.5)
        post_update = _mock_edge(src_id, dst_id, edge_type="neural_association", weight=0.8)
        post_update.last_updated = datetime(2026, 4, 26, 9, 19, 11, tzinfo=UTC)

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=existing)
        mock_repo.create_or_update_edge = AsyncMock(return_value=post_update)

        mock_ctx = MagicMock()
        mock_ctx.id = context_id
        mock_ctx.workspace_id = workspace_id

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "mcp_server.tools.edge._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch(
                "mcp_server.tools.edge._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "mcp_server.tools.edge._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_update_edge(
                {
                    "source_id": str(src_id),
                    "target_id": str(dst_id),
                    "weight": 0.8,
                    "context_id": str(context_id),
                },
                user_id,
                workspace_id,
            )

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["edge"]["weight"] == 0.8
        assert data["edge"]["last_updated"] == "2026-04-26T09:19:11+00:00"
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_error_when_edge_missing(self, user_id, workspace_id, context_id):
        """update_edge for a nonexistent edge returns edge_not_found without upsert."""
        src_id = uuid4()
        dst_id = uuid4()

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_repo = MagicMock()
        mock_repo.get_edge = AsyncMock(return_value=None)
        mock_repo.create_or_update_edge = AsyncMock()

        mock_ctx = MagicMock()
        mock_ctx.id = context_id
        mock_ctx.workspace_id = workspace_id

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "mcp_server.tools.edge._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch(
                "mcp_server.tools.edge._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "mcp_server.tools.edge._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_update_edge(
                {
                    "source_id": str(src_id),
                    "target_id": str(dst_id),
                    "weight": 0.8,
                    "context_id": str(context_id),
                },
                user_id,
                workspace_id,
            )

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "edge_not_found"
        mock_repo.create_or_update_edge.assert_not_awaited()
