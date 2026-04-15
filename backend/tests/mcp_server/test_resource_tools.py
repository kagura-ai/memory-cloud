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
        # 2) resource_id duplicate check → none
        # 3) workspace plan_name lookup → "basic"
        # (context name duplicate check is patched below, so it does not touch db.execute)
        role_result = MagicMock()
        owner = MagicMock()
        owner.role = "owner"
        role_result.scalar_one_or_none.return_value = owner

        resource_dup_result = MagicMock()
        resource_dup_result.scalar_one_or_none.return_value = None

        plan_result = MagicMock()
        plan_result.scalar_one_or_none.return_value = "basic"

        mock_db.execute.side_effect = [role_result, resource_dup_result, plan_result]

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

    @pytest.fixture(autouse=True)
    def _stub_quota(self):
        """Issue #332: ingest_events now consults a workspace-scoped quota helper.
        These tests cover validation paths only, so stub the quota layer to a no-op."""
        with (
            patch(
                "mcp_server.tools.resource.resolve_workspace_event_quota_per_hour",
                new=AsyncMock(return_value=1000),
            ),
            patch(
                "mcp_server.tools.resource.check_event_quota",
                new=AsyncMock(return_value=None),
            ),
        ):
            yield

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


# ============================================================================
# Happy-path tests (one per handler)
# ============================================================================


class TestGetResourceImpactHappyPath:
    @pytest.mark.asyncio
    async def test_returns_stats(self):
        """get_resource_impact returns token/memory/schema stats on success."""
        workspace_id = uuid4()
        mock_db = AsyncMock()

        # 1) boundary check: resource found
        boundary_result = MagicMock()
        boundary_result.scalar_one_or_none.return_value = uuid4()

        # 2) combined stats query: row with 3 counts
        stats_row = MagicMock()
        stats_row.token_count = 2
        stats_row.memory_count = 47
        stats_row.schema_version = 3
        stats_result = MagicMock()
        stats_result.one.return_value = stats_row

        mock_db.execute.side_effect = [boundary_result, stats_result]
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_resource_impact({"resource_id": "res1"}, "user", workspace_id)
        data = _json_of(result)
        assert data["status"] == "success"
        assert data["resource_id"] == "res1"
        assert data["token_count"] == 2
        assert data["memory_count"] == 47
        assert data["current_schema_version"] == 3


class TestGetResourceSchemaHappyPath:
    @pytest.mark.asyncio
    async def test_returns_latest_schema(self):
        """get_resource_schema returns the highest schema_version when unspecified."""
        from datetime import UTC, datetime

        workspace_id = uuid4()
        mock_db = AsyncMock()

        # 1) boundary check: resource found
        boundary_result = MagicMock()
        boundary_result.scalar_one_or_none.return_value = uuid4()

        # 2) schema query: schema row
        schema_row = MagicMock()
        schema_row.resource_id = "res1"
        schema_row.schema_version = 4
        schema_row.field_definitions = [{"name": "title", "type": "string"}]
        schema_row.created_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        schema_result = MagicMock()
        schema_result.scalar_one_or_none.return_value = schema_row

        mock_db.execute.side_effect = [boundary_result, schema_result]
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_resource_schema({"resource_id": "res1"}, "user", workspace_id)
        data = _json_of(result)
        assert data["status"] == "success"
        assert data["schema_version"] == 4
        assert data["field_definitions"] == [{"name": "title", "type": "string"}]


class TestListResourceTokensHappyPath:
    @pytest.mark.asyncio
    async def test_returns_paginated_tokens(self):
        """list_resource_tokens returns total + paginated token list."""
        from datetime import UTC, datetime

        workspace_id = uuid4()
        mock_db = AsyncMock()

        # 1) role check → owner
        role_result = MagicMock()
        owner = MagicMock()
        owner.role = "owner"
        role_result.scalar_one_or_none.return_value = owner

        # 2) total count query
        total_result = MagicMock()
        total_result.scalar.return_value = 5

        # 3) paginated tokens query
        token1 = MagicMock()
        token1.id = 1
        token1.resource_id = "res1"
        token1.description = "t1"
        token1.quota_events_per_hour = 1000
        token1.is_active = True
        token1.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        token1.last_used_at = None

        token2 = MagicMock()
        token2.id = 2
        token2.resource_id = "res2"
        token2.description = None
        token2.quota_events_per_hour = 500
        token2.is_active = False
        token2.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        token2.last_used_at = datetime(2026, 1, 3, tzinfo=UTC)

        tokens_result = MagicMock()
        tokens_result.scalars.return_value.all.return_value = [token1, token2]

        mock_db.execute.side_effect = [role_result, total_result, tokens_result]
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_list_resource_tokens(
                {"limit": 2, "offset": 0}, "user", workspace_id
            )
        data = _json_of(result)
        assert data["status"] == "success"
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == 0
        assert len(data["tokens"]) == 2
        assert data["tokens"][0]["id"] == 1
        assert data["tokens"][0]["is_active"] is True
        assert data["tokens"][1]["is_active"] is False


@pytest.fixture
def _stub_quota_for_ingest():
    """Issue #332: stub the workspace-scoped quota helper for ingest happy-paths."""
    with (
        patch(
            "mcp_server.tools.resource.resolve_workspace_event_quota_per_hour",
            new=AsyncMock(return_value=1000),
        ),
        patch(
            "mcp_server.tools.resource.check_event_quota",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield


class TestIngestEventsHappyPath:
    @pytest.mark.asyncio
    async def test_creates_upsert_event(self, _stub_quota_for_ingest):
        """ingest_events persists valid upsert event and schedules indexer."""
        workspace_id = uuid4()
        mock_db = AsyncMock()

        # 1) viewer role check → member
        role_result = MagicMock()
        m = MagicMock()
        m.role = "member"
        role_result.scalar_one_or_none.return_value = m
        # 2) boundary check → found
        boundary_result = MagicMock()
        boundary_result.scalar_one_or_none.return_value = uuid4()
        mock_db.execute.side_effect = [role_result, boundary_result]

        # Capture the added event so we can assign an id before flush returns
        added_events: list = []

        def _add(obj):
            obj.id = 123
            added_events.append(obj)

        # db.add is sync (AsyncMock defaults attributes to AsyncMock; override to MagicMock)
        mock_db.add = MagicMock(side_effect=_add)
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()

        # begin_nested must return an async context manager
        nested_cm = AsyncMock()
        nested_cm.__aenter__ = AsyncMock(return_value=None)
        nested_cm.__aexit__ = AsyncMock(return_value=None)
        mock_db.begin_nested = MagicMock(return_value=nested_cm)

        async def mock_get_db():
            yield mock_db

        async def mock_schedule(db, resource_id):
            return None

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "api.routes.resource_ingest._schedule_indexer_for_resource",
                new=mock_schedule,
            ),
        ):
            result = await handle_ingest_events(
                {
                    "resource_id": "res1",
                    "events": [
                        {
                            "op": "upsert",
                            "doc_id": "d1",
                            "version": 1,
                            "payload": {"x": 1},
                            "importance": 0.7,
                        }
                    ],
                },
                "user",
                workspace_id,
            )

        data = _json_of(result)
        assert data["status"] == "success"
        assert data["created_count"] == 1
        assert data["failed_count"] == 0
        assert data["event_ids"] == [123]
        assert len(added_events) == 1
        assert added_events[0].resource_id == "res1"
        assert added_events[0].op == "upsert"
        mock_db.commit.assert_awaited()


class TestSetupResourceHappyPath:
    @pytest.mark.asyncio
    async def test_creates_context_search_config_and_token(self):
        """setup_resource creates context + search config + token on Pro plan."""
        workspace_id = uuid4()
        context_uuid = uuid4()
        mock_db = AsyncMock()

        # db.execute sequence:
        # 1) role check → owner
        # 2) resource_id duplicate check → none
        # 3) workspace plan_name lookup → "pro"
        # 4) active token count → 0
        role_result = MagicMock()
        owner = MagicMock()
        owner.role = "owner"
        role_result.scalar_one_or_none.return_value = owner

        resource_dup_result = MagicMock()
        resource_dup_result.scalar_one_or_none.return_value = None

        plan_result = MagicMock()
        plan_result.scalar_one_or_none.return_value = "pro"

        token_count_result = MagicMock()
        token_count_result.scalar.return_value = 0

        mock_db.execute.side_effect = [
            role_result,
            resource_dup_result,
            plan_result,
            token_count_result,
        ]

        # Capture added objects
        added: list = []

        def _add(obj):
            # Assign ids so that db.refresh works
            cls_name = type(obj).__name__
            if cls_name == "Context":
                obj.id = context_uuid
            added.append(obj)

        # db.add is sync
        mock_db.add = MagicMock(side_effect=_add)
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()
        mock_db.commit = AsyncMock()

        # Mock services/managers
        mock_token_record = MagicMock()
        mock_token_record.id = 42

        async def mock_create_token(self, **kwargs):
            return ("plaintext-token-xyz", mock_token_record)

        async def mock_can_create(self, wsid):
            return (True, None)

        async def mock_get_ctx_by_name(self, wsid, name):
            return None

        def mock_validate_name(name):
            return None

        async def mock_get_db():
            yield mock_db

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.context_service.ContextService.validate_context_name",
                new=mock_validate_name,
            ),
            patch(
                "services.context_service.ContextService.get_context_by_name_for_workspace",
                new=mock_get_ctx_by_name,
            ),
            patch(
                "services.quota_service.QuotaService.check_context_creation_allowed",
                new=mock_can_create,
            ),
            patch(
                "auth.resource_tokens.ResourceTokenManager.create_token",
                new=mock_create_token,
            ),
        ):
            result = await handle_setup_resource(
                {
                    "name": "ctx-res1",
                    "resource_id": "res1",
                    "description": "smoke",
                },
                "user",
                workspace_id,
            )

        data = _json_of(result)
        assert data["status"] == "success"
        assert data["resource_id"] == "res1"
        assert data["token"] == "plaintext-token-xyz"
        assert data["token_id"] == 42
        assert data["context_id"] == str(context_uuid)
        # Verify Context + ContextSearchConfig were added
        type_names = [type(o).__name__ for o in added]
        assert "Context" in type_names
        assert "ContextSearchConfig" in type_names
        mock_db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_resource_id_conflict_pre_insert(self):
        """setup_resource rejects duplicate resource_id with resource_id_conflict."""
        workspace_id = uuid4()
        mock_db = AsyncMock()

        role_result = MagicMock()
        owner = MagicMock()
        owner.role = "owner"
        role_result.scalar_one_or_none.return_value = owner

        # resource_id duplicate check → existing context found
        resource_dup_result = MagicMock()
        resource_dup_result.scalar_one_or_none.return_value = uuid4()

        mock_db.execute.side_effect = [role_result, resource_dup_result]

        async def mock_get_db():
            yield mock_db

        async def mock_get_ctx_by_name(self, wsid, name):
            return None

        def mock_validate_name(name):
            return None

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.context_service.ContextService.validate_context_name",
                new=mock_validate_name,
            ),
            patch(
                "services.context_service.ContextService.get_context_by_name_for_workspace",
                new=mock_get_ctx_by_name,
            ),
        ):
            result = await handle_setup_resource(
                {"name": "ctx-res1", "resource_id": "res1"},
                "user",
                workspace_id,
            )
        data = _json_of(result)
        assert data["error"] == "resource_id_conflict"
