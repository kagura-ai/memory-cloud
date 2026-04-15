"""Integration tests for MCP ingest_events quota enforcement.

Issue #332: handle_ingest_events must call services.resource_quota_service.check_event_quota
after the viewer + workspace boundary checks, and the call MUST be workspace-scoped so
HTTP and MCP traffic share one Redis counter.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.resource import handle_ingest_events
from utils.exceptions import RateLimitError


def _json_of(result):
    """Decode the JSON body of an MCP TextContent response list."""
    return json.loads(result[0].text)


def _build_db_mock(*, role: str | None, boundary_ok: bool):
    """Build a mock DB whose db.execute returns role then boundary lookup.

    The MCP handler queries:
      1. _get_workspace_member_role → SELECT WorkspaceMember JOIN Workspace.
         .scalar_one_or_none returns a WorkspaceMember-like object whose
         .role attribute is the role string, or None when no member row
         matches (includes soft-deleted workspaces).
      2. _check_resource_workspace_boundary → SELECT Context.id, .scalar_one_or_none
    """
    role_result = MagicMock()
    if role is None:
        role_result.scalar_one_or_none.return_value = None
    else:
        member = MagicMock()
        member.role = role
        role_result.scalar_one_or_none.return_value = member

    boundary_result = MagicMock()
    boundary_result.scalar_one_or_none.return_value = uuid4() if boundary_ok else None

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[role_result, boundary_result])

    @asynccontextmanager
    async def _begin_nested():
        yield None

    mock_db.begin_nested = _begin_nested
    return mock_db


@pytest.fixture
def workspace_id():
    return uuid4()


def _patch_get_db(mock_db):
    async def _get_db():
        yield mock_db

    return patch("db.base.get_db", new=_get_db)


def _patch_log_tool_usage():
    """Skip the usage-log DB write — out of scope for these tests."""
    return patch(
        "mcp_server.tools.resource._log_tool_usage",
        new=AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_quota_check_invoked_after_permission_with_workspace_key(workspace_id):
    """Happy-path wiring: quota helper is called with (resource_id, workspace_id, count)."""
    mock_db = _build_db_mock(role="member", boundary_ok=True)

    with (
        _patch_get_db(mock_db),
        _patch_log_tool_usage(),
        patch(
            "mcp_server.tools.resource.resolve_workspace_event_quota_per_hour",
            new=AsyncMock(return_value=1000),
        ) as mock_resolve,
        patch(
            "mcp_server.tools.resource.check_event_quota",
            new=AsyncMock(return_value=None),
        ) as mock_quota,
    ):
        events = [
            {"op": "upsert", "doc_id": f"D-{i}", "version": 1, "payload": {"x": i}}
            for i in range(3)
        ]
        await handle_ingest_events(
            {"resource_id": "ec_products", "events": events},
            "user-1",
            workspace_id,
        )

        mock_resolve.assert_awaited_once()
        mock_quota.assert_awaited_once_with("ec_products", workspace_id, 1000, count=3)


@pytest.mark.asyncio
async def test_quota_exceeded_returns_error_and_does_not_ingest(workspace_id):
    """When check_event_quota raises RateLimitError, handler returns quota_exceeded
    and never reaches the ingest loop."""
    mock_db = _build_db_mock(role="member", boundary_ok=True)
    mock_db.add = MagicMock()  # would be called inside the ingest loop if reached

    with (
        _patch_get_db(mock_db),
        _patch_log_tool_usage(),
        patch(
            "mcp_server.tools.resource.resolve_workspace_event_quota_per_hour",
            new=AsyncMock(return_value=10),
        ),
        patch(
            "mcp_server.tools.resource.check_event_quota",
            new=AsyncMock(
                side_effect=RateLimitError(
                    message="Event quota exceeded: 9/10 events per hour",
                    retry_after=3600,
                )
            ),
        ),
    ):
        result = await handle_ingest_events(
            {
                "resource_id": "ec_products",
                "events": [{"op": "upsert", "doc_id": "D-1", "version": 1, "payload": {}}],
            },
            "user-1",
            workspace_id,
        )

        data = _json_of(result)
        assert data["error"] == "quota_exceeded"
        assert "Event quota exceeded" in data["message"]
        assert data["retry_after_seconds"] == 3600
        mock_db.add.assert_not_called()


@pytest.mark.asyncio
async def test_quota_skipped_when_viewer_rejected(workspace_id):
    """Viewer permission denial happens BEFORE quota check — no quota call, no resolve."""
    mock_db = _build_db_mock(role="viewer", boundary_ok=True)

    with (
        _patch_get_db(mock_db),
        _patch_log_tool_usage(),
        patch(
            "mcp_server.tools.resource.resolve_workspace_event_quota_per_hour",
            new=AsyncMock(return_value=1000),
        ) as mock_resolve,
        patch(
            "mcp_server.tools.resource.check_event_quota",
            new=AsyncMock(return_value=None),
        ) as mock_quota,
    ):
        result = await handle_ingest_events(
            {
                "resource_id": "ec_products",
                "events": [{"op": "upsert", "doc_id": "D-1", "version": 1, "payload": {}}],
            },
            "user-1",
            workspace_id,
        )

        data = _json_of(result)
        assert data["error"] == "permission_denied"
        mock_resolve.assert_not_awaited()
        mock_quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_quota_skipped_when_workspace_boundary_fails(workspace_id):
    """Workspace boundary fails BEFORE quota check — no quota call."""
    mock_db = _build_db_mock(role="member", boundary_ok=False)

    with (
        _patch_get_db(mock_db),
        _patch_log_tool_usage(),
        patch(
            "mcp_server.tools.resource.resolve_workspace_event_quota_per_hour",
            new=AsyncMock(return_value=1000),
        ) as mock_resolve,
        patch(
            "mcp_server.tools.resource.check_event_quota",
            new=AsyncMock(return_value=None),
        ) as mock_quota,
    ):
        result = await handle_ingest_events(
            {
                "resource_id": "ec_products",
                "events": [{"op": "upsert", "doc_id": "D-1", "version": 1, "payload": {}}],
            },
            "user-1",
            workspace_id,
        )

        data = _json_of(result)
        assert data["error"] == "resource_not_found"
        mock_resolve.assert_not_awaited()
        mock_quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_active_workspace_returns_early_without_quota(workspace_id):
    """workspace_id=None should return ``workspace_required`` before any DB / quota work."""
    with (
        patch(
            "mcp_server.tools.resource.resolve_workspace_event_quota_per_hour",
            new=AsyncMock(return_value=1000),
        ) as mock_resolve,
        patch(
            "mcp_server.tools.resource.check_event_quota",
            new=AsyncMock(return_value=None),
        ) as mock_quota,
    ):
        result = await handle_ingest_events(
            {
                "resource_id": "ec_products",
                "events": [{"op": "upsert", "doc_id": "D-1", "version": 1, "payload": {}}],
            },
            "user-1",
            None,
        )

        data = _json_of(result)
        assert data["error"] == "workspace_required"
        mock_resolve.assert_not_awaited()
        mock_quota.assert_not_awaited()
