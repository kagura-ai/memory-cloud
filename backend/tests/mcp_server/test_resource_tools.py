"""Tests for Resource management MCP tool handlers.

Issue #46: setup_resource, ingest_events, get_resource_impact,
get_resource_schema, list_resource_tokens.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.resource import (
    handle_get_resource_impact,
    handle_get_resource_schema,
    handle_ingest_events,
    handle_list_resource_tokens,
    handle_setup_resource,
)


def _json_of(result):
    return json.loads(result[0].text)


# ============================================================================
# workspace_required
# ============================================================================


class TestWorkspaceRequired:
    """All resource handlers must return workspace_required when workspace_id is None."""

    @pytest.mark.asyncio
    async def test_get_resource_impact(self):
        result = await handle_get_resource_impact({"resource_id": "res1"}, "user", None)
        data = _json_of(result)
        assert data["status"] == "error"
        assert data["error"] == "workspace_required"

    @pytest.mark.asyncio
    async def test_get_resource_schema(self):
        result = await handle_get_resource_schema({"resource_id": "res1"}, "user", None)
        data = _json_of(result)
        assert data["error"] == "workspace_required"

    @pytest.mark.asyncio
    async def test_list_resource_tokens(self):
        result = await handle_list_resource_tokens({}, "user", None)
        data = _json_of(result)
        assert data["error"] == "workspace_required"

    @pytest.mark.asyncio
    async def test_ingest_events(self):
        result = await handle_ingest_events(
            {"resource_id": "res1", "events": [{"op": "upsert", "doc_id": "d1"}]},
            "user",
            None,
        )
        data = _json_of(result)
        assert data["error"] == "workspace_required"

    @pytest.mark.asyncio
    async def test_setup_resource(self):
        result = await handle_setup_resource({"name": "r", "resource_id": "res1"}, "user", None)
        data = _json_of(result)
        assert data["error"] == "workspace_required"


# ============================================================================
# resource_id format validation
# ============================================================================


class TestResourceIdValidation:
    @pytest.mark.asyncio
    async def test_get_resource_impact_invalid_format(self):
        result = await handle_get_resource_impact({"resource_id": "Invalid ID!"}, "user", uuid4())
        data = _json_of(result)
        assert data["error"] == "validation_error"

    @pytest.mark.asyncio
    async def test_list_resource_tokens_invalid_format(self):
        """list_resource_tokens must also validate optional resource_id format."""
        # Mock role check as owner so validation gate is reached
        mock_db = AsyncMock()
        role_result = MagicMock()
        owner = MagicMock()
        owner.role = "owner"
        role_result.scalar_one_or_none.return_value = owner
        mock_db.execute.return_value = role_result

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_list_resource_tokens({"resource_id": "Invalid!"}, "user", uuid4())
        data = _json_of(result)
        # Should be validation_error, not resource_not_found
        assert data["error"] == "validation_error"

    @pytest.mark.asyncio
    async def test_list_resource_tokens_no_filter_skips_validation(self):
        """When resource_id is not provided, skip format validation path."""
        # Role check fails first (no member) — confirms we got past resource_id gate
        mock_db = AsyncMock()
        mock_role_result = MagicMock()
        mock_role_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_role_result

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_list_resource_tokens({}, "user", uuid4())
        data = _json_of(result)
        # Role check denies: not validation_error
        assert data["error"] == "permission_denied"


# ============================================================================
# list_resource_tokens: role gating (owner/admin only)
# ============================================================================


class TestListResourceTokensRoleGating:
    @pytest.mark.asyncio
    async def test_viewer_denied(self):
        workspace_id = uuid4()
        mock_db = AsyncMock()
        mock_role_result = MagicMock()
        member = MagicMock()
        member.role = "viewer"
        mock_role_result.scalar_one_or_none.return_value = member
        mock_db.execute.return_value = mock_role_result

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_list_resource_tokens({}, "user", workspace_id)
        data = _json_of(result)
        assert data["error"] == "permission_denied"

    @pytest.mark.asyncio
    async def test_member_denied(self):
        workspace_id = uuid4()
        mock_db = AsyncMock()
        mock_role_result = MagicMock()
        member = MagicMock()
        member.role = "member"
        mock_role_result.scalar_one_or_none.return_value = member
        mock_db.execute.return_value = mock_role_result

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_list_resource_tokens({}, "user", workspace_id)
        data = _json_of(result)
        assert data["error"] == "permission_denied"


# ============================================================================
# setup_resource: plan + role gating
# ============================================================================


class TestSetupResourcePlanGating:
    @pytest.mark.asyncio
    async def test_non_pro_plan_denied(self):
        """Basic/Free plan should not be able to run setup_resource."""
        workspace_id = uuid4()
        mock_db = AsyncMock()

        # db.execute order:
        # 1) role check → owner
        # 2) workspace plan_name lookup → "basic"
        # (context name duplicate check is patched below, so it does not touch db.execute)
        role_result = MagicMock()
        owner = MagicMock()
        owner.role = "owner"
        role_result.scalar_one_or_none.return_value = owner

        plan_result = MagicMock()
        plan_result.scalar_one_or_none.return_value = "basic"

        mock_db.execute.side_effect = [role_result, plan_result]

        async def mock_get_db():
            yield mock_db

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.context_service.ContextService.validate_context_name",
                return_value=None,
            ),
            patch(
                "services.context_service.ContextService.get_context_by_name_for_workspace",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await handle_setup_resource(
                {"name": "ctx", "resource_id": "res1"}, "user", workspace_id
            )

        data = _json_of(result)
        assert data["error"] == "plan_required"

    @pytest.mark.asyncio
    async def test_non_owner_denied(self):
        """Members cannot run setup_resource."""
        workspace_id = uuid4()
        mock_db = AsyncMock()
        role_result = MagicMock()
        member = MagicMock()
        member.role = "member"
        role_result.scalar_one_or_none.return_value = member
        mock_db.execute.return_value = role_result

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_setup_resource(
                {"name": "ctx", "resource_id": "res1"}, "user", workspace_id
            )

        data = _json_of(result)
        assert data["error"] == "permission_denied"


# ============================================================================
# get_resource_schema: schema_version validation
# ============================================================================


class TestSchemaVersionValidation:
    @pytest.mark.asyncio
    async def test_non_int_schema_version(self):
        """schema_version must be coerceable to int."""
        workspace_id = uuid4()
        mock_db = AsyncMock()
        # boundary check: found
        boundary_result = MagicMock()
        boundary_result.scalar_one_or_none.return_value = uuid4()
        mock_db.execute.return_value = boundary_result

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_resource_schema(
                {"resource_id": "res1", "schema_version": "abc"},
                "user",
                workspace_id,
            )
        data = _json_of(result)
        assert data["error"] == "validation_error"
        assert "integer" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_zero_schema_version_rejected(self):
        workspace_id = uuid4()
        mock_db = AsyncMock()
        boundary_result = MagicMock()
        boundary_result.scalar_one_or_none.return_value = uuid4()
        mock_db.execute.return_value = boundary_result

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_resource_schema(
                {"resource_id": "res1", "schema_version": 0},
                "user",
                workspace_id,
            )
        data = _json_of(result)
        assert data["error"] == "validation_error"


# ============================================================================
# ingest_events: per-item validation + partial success
# ============================================================================


class TestIngestEventsValidation:
    """Tests that verify per-item validation without hitting DB.

    Events that fail validation are caught BEFORE the begin_nested savepoint,
    so no DB session mocking for ResourceEvent insert is required — we only
    need enough DB mock to pass viewer + boundary checks.
    """

    def _make_db(self):
        mock_db = AsyncMock()
        # 1) viewer role check → member (write allowed)
        role_result = MagicMock()
        m = MagicMock()
        m.role = "member"
        role_result.scalar_one_or_none.return_value = m
        # 2) boundary check → found
        boundary_result = MagicMock()
        boundary_result.scalar_one_or_none.return_value = uuid4()
        mock_db.execute.side_effect = [role_result, boundary_result]
        mock_db.commit = AsyncMock()
        return mock_db

    @pytest.mark.asyncio
    async def test_non_dict_event_rejected(self):
        mock_db = self._make_db()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_ingest_events(
                {"resource_id": "res1", "events": ["not a dict"]},
                "user",
                uuid4(),
            )
        data = _json_of(result)
        assert data["status"] == "success"
        assert data["created_count"] == 0
        assert data["failed_count"] == 1
        assert data["errors"][0]["error"] == "event must be an object"

    @pytest.mark.asyncio
    async def test_invalid_op_rejected(self):
        mock_db = self._make_db()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_ingest_events(
                {"resource_id": "res1", "events": [{"op": "bogus", "doc_id": "d1"}]},
                "user",
                uuid4(),
            )
        data = _json_of(result)
        assert data["failed_count"] == 1
        assert "Invalid op" in data["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_upsert_missing_version(self):
        mock_db = self._make_db()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_ingest_events(
                {
                    "resource_id": "res1",
                    "events": [{"op": "upsert", "doc_id": "d1", "payload": {"x": 1}}],
                },
                "user",
                uuid4(),
            )
        data = _json_of(result)
        assert data["failed_count"] == 1
        assert "version" in data["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_upsert_non_int_version(self):
        mock_db = self._make_db()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_ingest_events(
                {
                    "resource_id": "res1",
                    "events": [
                        {
                            "op": "upsert",
                            "doc_id": "d1",
                            "payload": {"x": 1},
                            "version": "not_an_int",
                        }
                    ],
                },
                "user",
                uuid4(),
            )
        data = _json_of(result)
        assert data["failed_count"] == 1
        assert "integer" in data["errors"][0]["error"].lower()

    @pytest.mark.asyncio
    async def test_upsert_missing_payload(self):
        mock_db = self._make_db()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_ingest_events(
                {
                    "resource_id": "res1",
                    "events": [{"op": "upsert", "doc_id": "d1", "version": 1}],
                },
                "user",
                uuid4(),
            )
        data = _json_of(result)
        assert data["failed_count"] == 1
        assert "payload" in data["errors"][0]["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_with_payload_rejected(self):
        mock_db = self._make_db()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_ingest_events(
                {
                    "resource_id": "res1",
                    "events": [{"op": "delete", "doc_id": "d1", "payload": {"x": 1}}],
                },
                "user",
                uuid4(),
            )
        data = _json_of(result)
        assert data["failed_count"] == 1
        assert "payload must be null" in data["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_importance_out_of_range(self):
        mock_db = self._make_db()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_ingest_events(
                {
                    "resource_id": "res1",
                    "events": [
                        {
                            "op": "upsert",
                            "doc_id": "d1",
                            "version": 1,
                            "payload": {"x": 1},
                            "importance": 2.5,
                        }
                    ],
                },
                "user",
                uuid4(),
            )
        data = _json_of(result)
        assert data["failed_count"] == 1
        assert "importance" in data["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_empty_events_rejected(self):
        result = await handle_ingest_events({"resource_id": "res1", "events": []}, "user", uuid4())
        data = _json_of(result)
        assert data["error"] == "validation_error"

    @pytest.mark.asyncio
    async def test_batch_size_exceeded(self):
        events = [
            {"op": "upsert", "doc_id": f"d{i}", "version": 1, "payload": {"x": i}}
            for i in range(101)
        ]
        result = await handle_ingest_events(
            {"resource_id": "res1", "events": events}, "user", uuid4()
        )
        data = _json_of(result)
        assert data["error"] == "validation_error"
        assert "Batch size" in data["message"]
